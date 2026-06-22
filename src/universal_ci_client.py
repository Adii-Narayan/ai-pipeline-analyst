"""
universal_ci_client.py
----------------------
Single file that speaks to GitHub Actions, GitLab CI, Azure DevOps, and Jenkins.
Auto-detects the platform from the incoming webhook payload.

Environment variables needed (set only the ones for platforms you use):

  GitHub:
    GITHUB_TOKEN                 Personal access token (repo scope)
    GITHUB_WEBHOOK_SECRET        Optional shared secret

  GitLab:
    GITLAB_TOKEN                 Personal/project access token
    GITLAB_URL                   Base URL, default https://gitlab.com
    GITLAB_WEBHOOK_SECRET        Optional shared secret

  Azure DevOps:
    AZURE_DEVOPS_ORG             Organisation slug (from dev.azure.com/<org>)
    AZURE_DEVOPS_PAT             Personal access token

  Jenkins:
    JENKINS_URL                  e.g. https://jenkins.mycompany.com
    JENKINS_USER                 Jenkins username
    JENKINS_TOKEN                API token (User → Configure → API Token)
    JENKINS_WEBHOOK_SECRET       Optional shared secret
"""

import os
import re
import base64
import logging
import requests

log = logging.getLogger(__name__)

# ── Shared helpers ───────────────────────────────────────────────────────────

def _get(url: str, headers: dict, **kw) -> dict:
    r = requests.get(url, headers=headers, timeout=20, **kw)
    r.raise_for_status()
    return r.json()

def _get_text(url: str, headers: dict, **kw) -> str:
    r = requests.get(url, headers=headers, timeout=20, **kw)
    r.raise_for_status()
    return r.text

def _post(url: str, headers: dict, body: dict) -> dict:
    r = requests.post(url, headers=headers, json=body, timeout=20)
    r.raise_for_status()
    return r.json()

def tail(text: str, n: int = 300) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:] if len(lines) > n else lines)

def extract_test_lines(log_text: str) -> str:
    patterns = [
        r"FAILED|ERROR|PASSED|AssertionError|TypeError|ImportError",
        r"pytest|jest|mocha|go test|dotnet test|mvn test|gradle test",
        r"expected|actual|assertion|exception",
    ]
    hits = [l for l in log_text.splitlines()
            if any(re.search(p, l, re.IGNORECASE) for p in patterns)]
    return "\n".join(hits[:100])


# ═══════════════════════════════════════════════════════════════════════════
# PLATFORM DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def detect_platform(headers: dict, payload: dict) -> str:
    """Identify the CI platform from the webhook headers and payload shape."""
    # GitHub — X-GitHub-Event header
    if "X-GitHub-Event" in headers or "x-github-event" in headers:
        return "github"

    # GitLab — X-Gitlab-Event header
    if "X-Gitlab-Event" in headers or "x-gitlab-event" in headers:
        return "gitlab"

    # Jenkins — look for characteristic payload keys
    if "build" in payload and "url" in payload.get("build", {}):
        build_url = payload["build"].get("url", "")
        jenkins_url = os.environ.get("JENKINS_URL", "")
        if jenkins_url and jenkins_url in build_url:
            return "jenkins"
        if "/job/" in build_url:
            return "jenkins"

    # Azure DevOps — eventType field
    event_type = payload.get("eventType", "")
    if event_type.startswith("build.") or event_type.startswith("ms.vss"):
        return "azure"

    return "unknown"


def is_failure(platform: str, payload: dict) -> bool:
    """Return True only if this event represents a pipeline failure."""
    if platform == "github":
        return payload.get("workflow_run", {}).get("conclusion") == "failure"
    if platform == "gitlab":
        status = payload.get("object_attributes", {}).get("status", "")
        return status in ("failed", "failure")
    if platform == "azure":
        return payload.get("resource", {}).get("result") == "failed"
    if platform == "jenkins":
        phase  = payload.get("build", {}).get("phase", "")
        status = payload.get("build", {}).get("status", "")
        return phase == "FINALIZED" and status in ("FAILURE", "FAILED")
    return False


# ═══════════════════════════════════════════════════════════════════════════
# BUILD CONTEXT FETCHERS (one per platform)
# ═══════════════════════════════════════════════════════════════════════════

# ── GitHub ──────────────────────────────────────────────────────────────────

def _github_headers():
    token = os.environ.get("GITHUB_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def fetch_github_context(payload: dict) -> dict:
    h    = _github_headers()
    run  = payload.get("workflow_run", {})
    repo = payload["repository"]["full_name"]
    run_id = run["id"]

    # Logs
    jobs = _get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs", h)
    failed_job = next(
        (j for j in jobs.get("jobs", []) if j.get("conclusion") == "failure"),
        jobs.get("jobs", [{}])[0],
    )
    job_id = failed_job.get("id")
    log_text = ""
    if job_id:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs",
                headers=h, allow_redirects=True, timeout=20,
            )
            r.raise_for_status()
            log_text = tail(r.text)
        except Exception as e:
            log_text = f"(log fetch failed: {e})"

    # PR diff
    diff = ""
    prs = run.get("pull_requests", [])
    pr_number = prs[0]["number"] if prs else None
    commit_messages = []
    if pr_number:
        try:
            diff = _get_text(
                f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
                {**h, "Accept": "application/vnd.github.diff"},
            )
        except Exception:
            pass
        try:
            commits = _get(f"https://api.github.com/repos/{repo}/pulls/{pr_number}/commits", h)
            commit_messages = [c["commit"]["message"].splitlines()[0] for c in commits]
        except Exception:
            pass

    return {
        "platform": "GitHub Actions",
        "repo": repo,
        "run_id": run_id,
        "pr_number": pr_number,
        "log_tail": log_text,
        "diff": diff[:8000],
        "test_output": extract_test_lines(log_text),
        "commit_messages": commit_messages,
        "pipeline_name": run.get("name", ""),
        "branch": run.get("head_branch", ""),
    }

def post_github_comment(payload: dict, diagnosis: dict) -> str:
    h    = _github_headers()
    repo = payload["repository"]["full_name"]
    run  = payload.get("workflow_run", {})
    prs  = run.get("pull_requests", [])
    if not prs:
        return ""
    pr_number = prs[0]["number"]
    body = _format_comment(diagnosis, "github", payload)
    result = _post(
        f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
        h, {"body": body},
    )
    return result.get("html_url", "")


# ── GitLab ──────────────────────────────────────────────────────────────────

def _gitlab_headers():
    return {"PRIVATE-TOKEN": os.environ.get("GITLAB_TOKEN", "")}

def _gitlab_base():
    return os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")

def fetch_gitlab_context(payload: dict) -> dict:
    h          = _gitlab_headers()
    base       = _gitlab_base()
    project_id = payload.get("project_id") or payload.get("project", {}).get("id")
    attrs      = payload.get("object_attributes", {})
    pipeline_id = attrs.get("id")
    commit_sha  = attrs.get("sha", "")
    ref         = attrs.get("ref", "")
    mr_iid      = None

    # Log tail — find failed job and fetch its trace
    log_text = ""
    try:
        jobs = _get(f"{base}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs", h)
        failed_job = next((j for j in jobs if j.get("status") == "failed"), None)
        if failed_job:
            job_id = failed_job["id"]
            log_text = tail(_get_text(f"{base}/api/v4/projects/{project_id}/jobs/{job_id}/trace", h))
    except Exception as e:
        log_text = f"(log fetch failed: {e})"

    # MR diff
    diff = ""
    commit_messages = []
    try:
        mrs = _get(f"{base}/api/v4/projects/{project_id}/merge_requests"
                   f"?state=opened&source_branch={ref}", h)
        if mrs:
            mr_iid = mrs[0]["iid"]
            diff_data = _get(f"{base}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/diffs", h)
            lines = []
            for d in diff_data[:10]:
                lines.append(f"--- {d.get('old_path')}\n+++ {d.get('new_path')}")
                lines.append(d.get("diff", "")[:500])
            diff = "\n".join(lines)
    except Exception:
        pass

    try:
        commits = _get(f"{base}/api/v4/projects/{project_id}/repository/commits"
                       f"?ref_name={ref}&per_page=5", h)
        commit_messages = [c.get("title", "") for c in commits]
    except Exception:
        pass

    return {
        "platform": "GitLab CI",
        "project_id": project_id,
        "pipeline_id": pipeline_id,
        "mr_iid": mr_iid,
        "log_tail": log_text,
        "diff": diff,
        "test_output": extract_test_lines(log_text),
        "commit_messages": commit_messages,
        "pipeline_name": attrs.get("name", ""),
        "branch": ref,
    }

def post_gitlab_comment(payload: dict, diagnosis: dict) -> str:
    h          = _gitlab_headers()
    base       = _gitlab_base()
    project_id = payload.get("project_id") or payload.get("project", {}).get("id")
    attrs      = payload.get("object_attributes", {})
    ref        = attrs.get("ref", "")
    body       = _format_comment(diagnosis, "gitlab", payload)

    try:
        mrs = _get(f"{base}/api/v4/projects/{project_id}/merge_requests"
                   f"?state=opened&source_branch={ref}", h)
        if mrs:
            mr_iid = mrs[0]["iid"]
            _post(f"{base}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes",
                  h, {"body": body})
            return f"{base}/projects/{project_id}/merge_requests/{mr_iid}"
    except Exception as e:
        log.warning(f"Could not post GitLab comment: {e}")
    return ""


# ── Azure DevOps ─────────────────────────────────────────────────────────────

def _azure_headers():
    pat = os.environ.get("AZURE_DEVOPS_PAT", "")
    b64 = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {b64}", "Content-Type": "application/json"}

def _azure_base():
    org = os.environ.get("AZURE_DEVOPS_ORG", "")
    return f"https://dev.azure.com/{org}"

def fetch_azure_context(payload: dict) -> dict:
    h        = _azure_headers()
    base     = _azure_base()
    resource = payload.get("resource", {})
    build_id = resource.get("id")
    project  = resource.get("project", {}).get("name", "")
    repo_id  = resource.get("repository", {}).get("id", "")

    # Logs
    log_text = ""
    try:
        logs = _get(f"{base}/{project}/_apis/build/builds/{build_id}/logs?api-version=7.1", h)
        all_lines = []
        for entry in logs.get("value", [])[-5:]:
            lid = entry["id"]
            r = requests.get(
                f"{base}/{project}/_apis/build/builds/{build_id}/logs/{lid}?api-version=7.1",
                headers=h, timeout=20,
            )
            r.raise_for_status()
            all_lines.extend(r.text.splitlines())
        log_text = "\n".join(all_lines[-300:])
    except Exception as e:
        log_text = f"(log fetch failed: {e})"

    # Test results
    test_output = ""
    try:
        runs = _get(f"{base}/{project}/_apis/test/runs?buildId={build_id}&api-version=7.1", h)
        for run in runs.get("value", []):
            results = _get(
                f"{base}/{project}/_apis/test/runs/{run['id']}/results"
                f"?outcomes=Failed&api-version=7.1", h,
            )
            for r in results.get("value", [])[:5]:
                test_output += f"FAILED: {r.get('testCaseTitle')}\n{r.get('errorMessage','')}\n"
    except Exception:
        test_output = extract_test_lines(log_text)

    # PR info
    pr_id = None
    diff  = ""
    commit_messages = []
    trigger = resource.get("triggerInfo", {})
    pr_id_str = trigger.get("pr.number") or trigger.get("pr.id")
    if pr_id_str:
        try:
            pr_id = int(pr_id_str)
            changes = _get(
                f"{base}/{project}/_apis/git/pullrequests/{pr_id}/iterations?api-version=7.1", h,
            )
            iters = changes.get("value", [])
            if iters:
                latest = iters[-1]["id"]
                ch = _get(
                    f"{base}/{project}/_apis/git/pullrequests/{pr_id}"
                    f"/iterations/{latest}/changes?api-version=7.1", h,
                )
                diff = "\n".join(
                    f"{c.get('changeType','').upper()}: {c.get('item',{}).get('path','')}"
                    for c in ch.get("changeEntries", [])[:20]
                )
        except Exception:
            pass

    try:
        ch = _get(f"{base}/{project}/_apis/build/builds/{build_id}/changes?api-version=7.1", h)
        commit_messages = [c.get("message","").splitlines()[0] for c in ch.get("value",[])[:5]]
    except Exception:
        pass

    return {
        "platform": "Azure DevOps",
        "project": project,
        "repo_id": repo_id,
        "build_id": build_id,
        "pr_id": pr_id,
        "log_tail": log_text,
        "diff": diff,
        "test_output": test_output,
        "commit_messages": commit_messages,
        "pipeline_name": resource.get("definition", {}).get("name", ""),
        "branch": resource.get("sourceBranch", "").replace("refs/heads/", ""),
    }

def post_azure_comment(payload: dict, diagnosis: dict) -> str:
    h        = _azure_headers()
    base     = _azure_base()
    resource = payload.get("resource", {})
    project  = resource.get("project", {}).get("name", "")
    repo_id  = resource.get("repository", {}).get("id", "")
    build_id = resource.get("id")
    trigger  = resource.get("triggerInfo", {})
    pr_id_str = trigger.get("pr.number") or trigger.get("pr.id")
    if not pr_id_str:
        return ""
    pr_id = int(pr_id_str)
    body  = _format_comment(diagnosis, "azure", payload)
    try:
        _post(
            f"{base}/{project}/_apis/git/repositories/{repo_id}"
            f"/pullRequests/{pr_id}/threads?api-version=7.1",
            h,
            {"comments": [{"parentCommentId": 0, "content": body, "commentType": 1}],
             "status": "active"},
        )
        org = os.environ.get("AZURE_DEVOPS_ORG", "")
        return f"https://dev.azure.com/{org}/{project}/_git/{repo_id}/pullrequest/{pr_id}"
    except Exception as e:
        log.warning(f"Could not post Azure comment: {e}")
        return ""


# ── Jenkins ──────────────────────────────────────────────────────────────────

def _jenkins_headers():
    user  = os.environ.get("JENKINS_USER", "")
    token = os.environ.get("JENKINS_TOKEN", "")
    b64   = base64.b64encode(f"{user}:{token}".encode()).decode()
    return {"Authorization": f"Basic {b64}"}

def fetch_jenkins_context(payload: dict) -> dict:
    h          = _jenkins_headers()
    base_url   = os.environ.get("JENKINS_URL", "").rstrip("/")
    build      = payload.get("build", {})
    build_url  = build.get("url", "")
    build_num  = build.get("number", 0)
    job_name   = payload.get("name", "")
    full_url   = build_url if build_url.startswith("http") else f"{base_url}{build_url}"

    # Console log
    log_text = ""
    try:
        r = requests.get(f"{full_url}consoleText", headers=h, timeout=20)
        r.raise_for_status()
        log_text = tail(r.text)
    except Exception as e:
        log_text = f"(log fetch failed: {e})"

    # Build details — changes / commits
    commit_messages = []
    diff = ""
    try:
        details = _get(f"{full_url}api/json?tree=changeSets[items[comment,msg,paths[editType,file]]]", h)
        for cs in details.get("changeSets", []):
            for item in cs.get("items", [])[:5]:
                msg = item.get("msg") or item.get("comment", "")
                if msg:
                    commit_messages.append(msg.splitlines()[0])
                for p in item.get("paths", [])[:10]:
                    diff += f"{p.get('editType','').upper()}: {p.get('file','')}\n"
    except Exception:
        pass

    # Test results
    test_output = ""
    try:
        test_data = _get(f"{full_url}testReport/api/json?tree=suites[cases[name,status,errorDetails,errorStackTrace]]", h)
        for suite in test_data.get("suites", []):
            for case in suite.get("cases", []):
                if case.get("status") in ("FAILED", "ERROR"):
                    test_output += f"FAILED: {case.get('name')}\n{case.get('errorDetails','')}\n"
    except Exception:
        test_output = extract_test_lines(log_text)

    return {
        "platform": "Jenkins",
        "job_name": job_name,
        "build_number": build_num,
        "build_url": full_url,
        "log_tail": log_text,
        "diff": diff,
        "test_output": test_output,
        "commit_messages": commit_messages,
        "pipeline_name": job_name,
        "branch": build.get("scm", {}).get("branch", ""),
    }

def post_jenkins_comment(payload: dict, diagnosis: dict) -> str:
    """Jenkins has no native PR comments — log the diagnosis and return the build URL."""
    build    = payload.get("build", {})
    full_url = build.get("url", "")
    log.info(f"Jenkins diagnosis for {full_url}:\n{diagnosis['root_cause']}")
    return full_url


# ═══════════════════════════════════════════════════════════════════════════
# UNIFIED PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def fetch_context(platform: str, payload: dict) -> dict:
    """Route to the right platform fetcher."""
    if platform == "github":
        return fetch_github_context(payload)
    if platform == "gitlab":
        return fetch_gitlab_context(payload)
    if platform == "azure":
        return fetch_azure_context(payload)
    if platform == "jenkins":
        return fetch_jenkins_context(payload)
    raise ValueError(f"Unknown platform: {platform}")


def post_comment(platform: str, payload: dict, diagnosis: dict) -> str:
    """Post the diagnosis back to the originating platform."""
    if platform == "github":
        return post_github_comment(payload, diagnosis)
    if platform == "gitlab":
        return post_gitlab_comment(payload, diagnosis)
    if platform == "azure":
        return post_azure_comment(payload, diagnosis)
    if platform == "jenkins":
        return post_jenkins_comment(payload, diagnosis)
    return ""


# ── Comment formatter (shared across all platforms) ──────────────────────────

CATEGORY_EMOJI = {
    "test_failure": "🔴", "dependency": "📦", "config": "⚙️",
    "env": "🔑", "lint": "🔍", "type_error": "🔷",
    "network": "🌐", "timeout": "⏱️", "other": "⚠️",
}
CONFIDENCE_BADGE = {
    "high":   "🟢 High confidence",
    "medium": "🟡 Medium confidence",
    "low":    "🔴 Low confidence — review carefully",
}
PLATFORM_LABEL = {
    "github": "GitHub Actions",
    "gitlab": "GitLab CI",
    "azure":  "Azure DevOps",
    "jenkins": "Jenkins",
}

def _format_comment(diagnosis: dict, platform: str, payload: dict) -> str:
    emoji = CATEGORY_EMOJI.get(diagnosis.get("category", "other"), "⚠️")
    conf  = CONFIDENCE_BADGE.get(diagnosis.get("confidence", "low"), "")
    label = PLATFORM_LABEL.get(platform, platform)

    lines = [
        f"## {emoji} CI/CD Intelligence Agent — {label} Failure",
        "",
        f"**Root cause:** {diagnosis['root_cause']}",
        "",
    ]
    if diagnosis.get("technical_detail"):
        lines += ["### Technical detail", diagnosis["technical_detail"], ""]

    files = diagnosis.get("affected_files", [])
    if files:
        lines.append("### Affected files")
        for f in files:
            lr = f" (line {f['line']})" if f.get("line") else ""
            lines.append(f"- `{f['file']}`{lr} — {f.get('reason','')}")
        lines.append("")

    if diagnosis.get("suggested_fix"):
        lines += ["### Suggested fix", diagnosis["suggested_fix"], ""]

    if diagnosis.get("suggested_fix_code"):
        lines += ["```diff", diagnosis["suggested_fix_code"], "```", ""]

    if diagnosis.get("related_commit"):
        lines.append(f"**Likely introduced by:** `{diagnosis['related_commit']}`")
        lines.append("")

    lines += ["---", f"_{conf} · Powered by Claude · {label}_"]
    return "\n".join(lines)
