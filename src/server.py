"""
server.py — Universal CI/CD Intelligence Agent
Single /webhook endpoint handles GitHub, GitLab, Azure DevOps, and Jenkins.

Credentials are loaded from config.json (saved by the dashboard UI) —
you do NOT need to put platform tokens in your .env file.
LLM settings (LLM_MODEL + OPENAI_API_KEY, ANTHROPIC_API_KEY, or GEMINI_API_KEY)
and PORT go in .env — or configure via the dashboard.
"""

import os
import json
import hmac
import hashlib
import logging
import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
from diagnose import diagnose_failure
from universal_ci_client import detect_platform, is_failure, fetch_context, post_comment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config store (written by the dashboard /api/config endpoint) ─────────────
_THIS_FILE   = Path(__file__).resolve()
CONFIG_FILE  = _THIS_FILE.parent.parent / "config.json"

load_dotenv(_THIS_FILE.parent.parent / ".env")

def load_config() -> dict:
    """Load platform credentials from config.json, fall back to env vars."""
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            log.debug(f"Loaded config from {CONFIG_FILE}")
        except Exception as e:
            log.warning(f"Could not read config.json: {e}")
    return cfg

def get_cfg(key: str, default: str = "") -> str:
    """Read a credential: config.json first, then environment variable."""
    cfg = load_config()
    return cfg.get(key) or os.environ.get(key, default)

def save_config(data: dict):
    """Merge new credentials into config.json."""
    existing = {}
    if CONFIG_FILE.exists():
        try:
            existing = json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    existing.update({k: v for k, v in data.items() if v})  # skip empty strings
    CONFIG_FILE.write_text(json.dumps(existing, indent=2))
    log.info(f"Config saved to {CONFIG_FILE}")

# Resolve dashboard folder — tries multiple locations so it works
# whether you run from the project root OR from inside src/
def _find_dashboard() -> Path:
    candidates = [
        _THIS_FILE.parent.parent / "dashboard",   # running from project root: src/../dashboard
        _THIS_FILE.parent / "dashboard",          # running from src/: src/dashboard
        Path.cwd() / "dashboard",                 # fallback: CWD/dashboard
        Path.cwd().parent / "dashboard",          # fallback: parent of CWD/dashboard
    ]
    for c in candidates:
        if c.exists() and (c / "index.html").exists():
            return c
    # None found — return the most likely path and let Flask give a clear error
    return candidates[0]

DASHBOARD_DIR = _find_dashboard()

app = Flask(__name__, static_folder=str(DASHBOARD_DIR), static_url_path="/static")

HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", "diagnoses.json"))


# ── History store (append-only JSON file) ───────────────────────────────────

def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            return []
    return []

def save_history(history: list):
    HISTORY_FILE.write_text(json.dumps(history, indent=2))

def append_diagnosis(platform: str, context: dict, diagnosis: dict, comment_url: str):
    history = load_history()
    history.insert(0, {
        "id": len(history) + 1,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "platform": platform,
        "pipeline_name": context.get("pipeline_name", ""),
        "branch": context.get("branch", ""),
        "repo": context.get("repo") or context.get("project") or context.get("job_name", ""),
        "root_cause": diagnosis.get("root_cause", ""),
        "technical_detail": diagnosis.get("technical_detail", ""),
        "affected_files": diagnosis.get("affected_files", []),
        "suggested_fix": diagnosis.get("suggested_fix", ""),
        "suggested_fix_code": diagnosis.get("suggested_fix_code", ""),
        "confidence": diagnosis.get("confidence", "low"),
        "category": diagnosis.get("category", "other"),
        "related_commit": diagnosis.get("related_commit", ""),
        "comment_url": comment_url,
    })
    save_history(history[:200])  # keep last 200 entries


# ── Webhook signature verification ──────────────────────────────────────────

def _verify(secret_env: str, payload: bytes, header: str, prefix: str = "sha256=") -> bool:
    secret = os.environ.get(secret_env, "")
    if not secret:
        return True
    sig = request.headers.get(header, "")
    expected = prefix + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/history")
def api_history():
    """Dashboard polls this endpoint for diagnosis history."""
    history = load_history()
    limit = int(request.args.get("limit", 50))
    platform = request.args.get("platform", "")
    if platform:
        history = [h for h in history if h["platform"].lower() == platform.lower()]
    return jsonify(history[:limit])


@app.route("/api/stats")
def api_stats():
    history = load_history()
    platforms = {}
    categories = {}
    confidences = {"high": 0, "medium": 0, "low": 0}
    for h in history:
        p = h.get("platform", "unknown")
        platforms[p] = platforms.get(p, 0) + 1
        c = h.get("category", "other")
        categories[c] = categories.get(c, 0) + 1
        conf = h.get("confidence", "low")
        confidences[conf] = confidences.get(conf, 0) + 1
    return jsonify({
        "total": len(history),
        "platforms": platforms,
        "categories": categories,
        "confidences": confidences,
    })


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Return currently saved config (with secrets masked for display)."""
    cfg = load_config()
    masked = {}
    for k, v in cfg.items():
        if v and any(word in k.lower() for word in ["token","pat","secret","key","password"]):
            masked[k] = v[:4] + "•" * max(4, len(v) - 4) if len(v) > 4 else "••••"
        else:
            masked[k] = v
    return jsonify({"config": masked, "keys": list(cfg.keys())})


@app.route("/api/config", methods=["POST"])
def api_config_save():
    """Save platform credentials sent from the dashboard settings UI."""
    data = request.json or {}
    if not data:
        return jsonify({"error": "Empty payload"}), 400
    try:
        save_config(data)
        # Reload env-like vars into os.environ so running server picks them up immediately
        for k, v in data.items():
            if v:
                os.environ[k] = v
        log.info(f"Config updated: {list(data.keys())}")
        return jsonify({"status": "saved", "keys": list(data.keys())}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config/<key>", methods=["DELETE"])
def api_config_delete(key):
    """Remove a single credential key from config.json."""
    try:
        cfg = {}
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text())
        cfg.pop(key, None)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
        os.environ.pop(key, None)
        return jsonify({"status": "deleted", "key": key}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/webhook", methods=["POST"])
def universal_webhook():
    payload_bytes = request.data
    payload       = request.json or {}
    headers       = dict(request.headers)

    # Detect platform
    platform = detect_platform(headers, payload)
    log.info(f"Webhook received — detected platform: {platform}")

    if platform == "unknown":
        return jsonify({"skipped": "Could not detect CI platform"}), 200

    # Verify signatures per platform
    if platform == "github":
        if not _verify("GITHUB_WEBHOOK_SECRET", payload_bytes, "X-Hub-Signature-256"):
            return jsonify({"error": "Invalid GitHub signature"}), 401

    elif platform == "gitlab":
        secret = os.environ.get("GITLAB_WEBHOOK_SECRET", "")
        if secret and request.headers.get("X-Gitlab-Token", "") != secret:
            return jsonify({"error": "Invalid GitLab token"}), 401

    elif platform == "jenkins":
        if not _verify("JENKINS_WEBHOOK_SECRET", payload_bytes, "X-Jenkins-Signature", "sha256="):
            return jsonify({"error": "Invalid Jenkins signature"}), 401

    # Only process failures
    if not is_failure(platform, payload):
        status = (payload.get("workflow_run", {}).get("conclusion")
                  or payload.get("object_attributes", {}).get("status")
                  or payload.get("resource", {}).get("result")
                  or payload.get("build", {}).get("status", ""))
        return jsonify({"skipped": f"Not a failure (status: {status})"}), 200

    try:
        context   = fetch_context(platform, payload)
        diagnosis = diagnose_failure(context)
        comment_url = post_comment(platform, payload, diagnosis)

        append_diagnosis(platform, context, diagnosis, comment_url)

        log.info(f"Diagnosed [{platform}] {diagnosis['root_cause'][:80]}")

        return jsonify({
            "status": "diagnosed",
            "platform": platform,
            "root_cause": diagnosis["root_cause"],
            "confidence": diagnosis["confidence"],
            "comment_url": comment_url,
        }), 200

    except Exception as e:
        log.exception(f"Error processing {platform} webhook: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/")
def root():
    return send_from_directory(str(DASHBOARD_DIR), "index.html")


@app.route("/<path:fname>")
def static_files(fname):
    """Serve any static file from the dashboard folder."""
    return send_from_directory(str(DASHBOARD_DIR), fname)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    log.info(f"Universal CI/CD Agent listening on :{port}")
    log.info(f"Dashboard dir  : {DASHBOARD_DIR}")
    log.info(f"Dashboard found: {DASHBOARD_DIR.exists()}")
    if not DASHBOARD_DIR.exists():
        log.error("="*60)
        log.error("DASHBOARD FOLDER NOT FOUND!")
        log.error(f"Expected at: {DASHBOARD_DIR}")
        log.error("Make sure your folder structure looks like:")
        log.error("  Ci-Cd Intelligence/")
        log.error("    dashboard/")
        log.error("      index.html   <-- must exist")
        log.error("    src/")
        log.error("      server.py")
        log.error("="*60)
    log.info(f"Open: http://localhost:{port}/")
    app.run(host="0.0.0.0", port=port, debug=False)
