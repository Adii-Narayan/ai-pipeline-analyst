"""
Diagnosis engine — sends build context to an LLM and parses the structured response.

Supports Claude, OpenAI, and Gemini via LiteLLM.
Configure with LLM_MODEL plus the matching provider API key.
"""

import os
import json
import logging
from pathlib import Path

import litellm
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.json"

DEFAULT_MODEL = "anthropic/claude-sonnet-4-20250514"

SYSTEM_PROMPT = """You are a senior DevOps / platform engineer with deep expertise in CI/CD systems, 
Python, JavaScript, Docker, and cloud infrastructure. Your job is to diagnose CI build failures.

You will receive:
- The tail of the build log (last 300 lines)
- The git diff of the PR that triggered the build
- Test output (if available)
- Recent commit messages

You must respond ONLY with valid JSON — no markdown, no preamble, no explanation outside the JSON.

JSON schema:
{
  "root_cause": "One clear sentence explaining exactly why the build failed.",
  "technical_detail": "2-3 sentences with the precise technical explanation — file, line, variable, type mismatch, missing env var, etc.",
  "affected_files": [
    {"file": "path/to/file.py", "line": 42, "reason": "short reason"}
  ],
  "suggested_fix": "Plain-English description of what to change.",
  "suggested_fix_code": "Optional: a unified diff or code snippet showing the exact fix. Omit if not confident.",
  "confidence": "high | medium | low",
  "category": "test_failure | dependency | config | env | lint | type_error | network | timeout | other",
  "related_commit": "Optional: the commit SHA or message that introduced this, if identifiable from the diff."
}"""


def _get_cfg(key: str, default: str = "") -> str:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            if cfg.get(key):
                return cfg[key]
        except Exception:
            pass
    return os.environ.get(key, default)


def _resolve_api_key(model: str) -> str:
    """Pick the provider-specific API key for the configured model."""
    model_lower = model.lower()
    provider_keys = [
        (("anthropic/", "claude"), "ANTHROPIC_API_KEY"),
        (("openai/", "gpt-", "o1", "o3", "o4", "chatgpt"), "OPENAI_API_KEY"),
        (("gemini/", "gemini"), "GEMINI_API_KEY"),
        (("gemini/", "gemini"), "GOOGLE_API_KEY"),
    ]
    for prefixes, env_key in provider_keys:
        if any(p in model_lower for p in prefixes):
            key = _get_cfg(env_key)
            if key:
                return key
    return ""


def get_llm_settings() -> dict:
    model = _get_cfg("LLM_MODEL", DEFAULT_MODEL)
    api_key = _resolve_api_key(model)
    if not api_key:
        raise ValueError(
            "No supported provider API key configured for LLM_MODEL. "
            "Use a Claude, OpenAI, or Gemini model and set ANTHROPIC_API_KEY, "
            "OPENAI_API_KEY, GEMINI_API_KEY, or GOOGLE_API_KEY."
        )
    return {"model": model, "api_key": api_key}


def build_prompt(context: dict) -> str:
    parts = []

    parts.append("## Build log (last 300 lines)")
    parts.append("```")
    parts.append(context.get("log_tail", "(no log available)"))
    parts.append("```")

    if context.get("diff"):
        parts.append("\n## Git diff (files changed in this PR)")
        parts.append("```diff")
        diff = context["diff"]
        if len(diff) > 8000:
            diff = diff[:8000] + "\n... (diff truncated)"
        parts.append(diff)
        parts.append("```")

    if context.get("test_output"):
        parts.append("\n## Test output")
        parts.append("```")
        parts.append(context["test_output"][:3000])
        parts.append("```")

    if context.get("commit_messages"):
        parts.append("\n## Recent commit messages")
        for msg in context["commit_messages"][:5]:
            parts.append(f"- {msg}")

    parts.append("\nDiagnose this failure and respond with JSON only.")
    return "\n".join(parts)


def diagnose_failure(context: dict) -> dict:
    prompt = build_prompt(context)
    settings = get_llm_settings()

    log.info("Sending build context to %s for diagnosis...", settings["model"])

    kwargs = {
        "model": settings["model"],
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "api_key": settings["api_key"],
    }
    response = litellm.completion(**kwargs)
    raw = response.choices[0].message.content.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        diagnosis = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("LLM returned invalid JSON: %s\nRaw: %s", e, raw[:500])
        diagnosis = {
            "root_cause": "Could not parse diagnosis — see raw output.",
            "technical_detail": raw[:500],
            "affected_files": [],
            "suggested_fix": "Check the build log manually.",
            "confidence": "low",
            "category": "other",
        }

    log.info("Diagnosis complete: %s...", diagnosis["root_cause"][:80])
    return diagnosis
