# CI/CD Intelligence Agent

An AI-powered DevOps agent that diagnoses pipeline failures across **GitHub Actions, GitLab CI, Azure DevOps, and Jenkins** — all from a single codebase. When a build fails, it fetches the logs, diff, and test results, sends them to Claude, and posts a plain-English root cause explanation + suggested fix directly to your PR.

---

## Table of Contents

- [What It Does](#what-it-does)
- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [Quick Start](#quick-start)
- [File Reference](#file-reference)
  - [diagnose.py](#diagnosepy)
  - [universal_ci_client.py](#universal_ci_clientpy)
  - [server.py](#serverpy)
  - [dashboard/index.html](#dashboardindexhtml)
- [Platform Setup](#platform-setup)
  - [GitHub Actions](#github-actions)
  - [GitLab CI](#gitlab-ci)
  - [Azure DevOps](#azure-devops)
  - [Jenkins](#jenkins)
- [Environment Variables](#environment-variables)
- [Dashboard](#dashboard)
- [Example Diagnosis Output](#example-diagnosis-output)
- [Deployment](#deployment)
- [Extending the Agent](#extending-the-agent)

---

## What It Does

When any pipeline fails, this agent automatically:

1. Receives a webhook from your CI platform
2. Fetches the build logs, git diff, test results, and commit history
3. Sends everything to Claude with a structured prompt
4. Gets back a JSON diagnosis: root cause, affected files, suggested fix, confidence level
5. Posts the diagnosis as a comment on the PR or build
6. Stores it in the dashboard for your team to review

The result looks like this on your PR:

```
🔴 CI/CD Intelligence Agent — Build Failure Diagnosis

Root cause: Redis 4.x returns bytes instead of str — token comparison
on line 84 of auth/token_refresh.py always evaluates to False.

Technical detail: Upgrading redis-py from 3.5 to 4.x changed cache.get()
to return bytes. The == comparison against a str always fails silently.

Affected files:
- auth/token_refresh.py (line 84) — bytes vs str comparison

Suggested fix: Decode the cached value before comparing.

  - return self.cache.get(f"token:{token}") == token
  + stored = self.cache.get(f"token:{token}")
  + return stored and stored.decode() == token

Likely introduced by: Upgrade redis-py to 4.6.0

🟢 High confidence · Powered by Claude · GitHub Actions
```

---

## How It Works

```
CI pipeline fails
      │
      ▼
Webhook fires ──► /webhook endpoint (server.py)
      │
      ▼
Platform detected (GitHub / GitLab / Azure / Jenkins)
      │
      ▼
Context fetched (universal_ci_client.py)
  • Build logs (last 300 lines)
  • Git diff / changed files
  • Test output (structured or extracted)
  • Recent commit messages
      │
      ▼
Claude diagnoses (diagnose.py)
  • Root cause
  • Affected files + line numbers
  • Suggested fix + code diff
  • Confidence: high / medium / low
  • Category: test_failure / dependency / config / env / type_error / network / timeout
      │
      ├──► PR comment posted (back to originating platform)
      └──► Diagnosis saved to diagnoses.json (dashboard)
```

---

## Project Structure

```
cicd-unified/
├── src/
│   ├── diagnose.py              # Claude API call — platform-agnostic brain
│   ├── universal_ci_client.py   # All 4 platform clients in one file
│   └── server.py                # Flask webhook server + REST API for dashboard
├── dashboard/
│   └── index.html               # Live dashboard (no build step needed)
├── requirements.txt
├── .env.example
├── Dockerfile
└── README.md
```

---

## Quick Start

### 1. Clone and install

```bash
git clone <your-repo>
cd cicd-unified
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — only fill in the platforms you use
```

Minimum required:

```env
LLM_MODEL=your-provider-model
LLM_API_KEY=your-provider-api-key
GITHUB_TOKEN=ghp_...        # if using GitHub
```

You can also use provider-specific keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, etc.) instead of `LLM_API_KEY`.

### 3. Test locally (no CI platform needed)

The server ships with demo data so you can see the dashboard immediately:

```bash
python src/server.py
open http://localhost:8080
```

### 4. Expose to the internet

Use [ngrok](https://ngrok.com) for local development:

```bash
ngrok http 8080
# Copy the HTTPS URL, e.g. https://abc123.ngrok.io
```

### 5. Connect your CI platforms

Point each platform's webhook at: `https://your-url/webhook`

All four platforms share the **same single endpoint**. The server auto-detects which platform is sending the payload from the headers and payload shape.

---

## File Reference

### diagnose.py

The Claude-powered diagnosis engine. Completely platform-agnostic — it receives a context dictionary and returns a structured JSON diagnosis. This file never changes regardless of which CI platforms you add.

**Input — context dict:**

| Key | Description |
|---|---|
| `log_tail` | Last 300 lines of the build log |
| `diff` | Git diff of the PR / changed files |
| `test_output` | Failed test names + error messages |
| `commit_messages` | Last 5 commit messages |
| `timeline` | (Azure/Jenkins) Failed task names |

**Output — diagnosis dict:**

```python
{
  "root_cause": "One sentence: exactly why the build failed.",
  "technical_detail": "2-3 sentences with precise technical context.",
  "affected_files": [
    {"file": "path/to/file.py", "line": 84, "reason": "bytes vs str comparison"}
  ],
  "suggested_fix": "Plain-English description of what to change.",
  "suggested_fix_code": "--- old line\n+++ new line",   # unified diff
  "confidence": "high",    # high | medium | low
  "category": "test_failure",  # see categories below
  "related_commit": "Upgrade redis-py to 4.6.0"
}
```

**Failure categories:**

| Category | Meaning |
|---|---|
| `test_failure` | A unit/integration test assertion failed |
| `dependency` | Missing or incompatible package |
| `config` | Pipeline YAML or build config error |
| `env` | Missing environment variable or secret |
| `lint` | Linting or formatting failure |
| `type_error` | Type mismatch (TypeScript, mypy, etc.) |
| `network` | Timeout or connection failure |
| `timeout` | Build exceeded time limit |
| `other` | Anything else |

**The Claude prompt structure:**

```
## Build log (last 300 lines)
{log_tail}

## Git diff
{diff}

## Test output
{test_output}

## Recent commit messages
{commit_messages}

Diagnose this failure. Respond with JSON only.
```

**Customising the prompt:**

Edit the `SYSTEM_PROMPT` constant in `diagnose.py` to add your stack-specific knowledge, e.g.:

```python
SYSTEM_PROMPT = """You are a senior DevOps engineer at Acme Corp.
Our stack: Python 3.12, FastAPI, PostgreSQL 15, Redis 7, deployed on AWS ECS.
...
"""
```

---

### universal_ci_client.py

Single file containing all four platform clients. Handles:

- **Platform detection** — reads webhook headers (`X-GitHub-Event`, `X-Gitlab-Event`, `eventType`) and payload shape to identify the source automatically
- **Failure detection** — checks conclusion/result/status per platform
- **Context fetching** — logs, diffs, test results, commits for each platform
- **Comment posting** — posts the diagnosis back to the PR/MR in the correct format

**Public API (used by server.py):**

```python
from universal_ci_client import detect_platform, is_failure, fetch_context, post_comment

platform = detect_platform(headers, payload)   # "github" | "gitlab" | "azure" | "jenkins"
failure  = is_failure(platform, payload)       # bool
context  = fetch_context(platform, payload)    # dict passed to diagnose.py
url      = post_comment(platform, payload, diagnosis)  # posted comment URL
```

**What each platform fetches:**

| | GitHub | GitLab | Azure DevOps | Jenkins |
|---|---|---|---|---|
| Logs | Actions job logs API | Job trace API | Build logs API | `/consoleText` |
| Diff | PR unified diff | MR diffs API | PR iteration changes | Changeset paths |
| Tests | Extracted from logs | Extracted from logs | Test runs API (structured) | Test report API |
| Commits | PR commits API | Branch commits API | Build changes API | Changeset items |
| Comment target | PR issue comment | MR note | PR thread | Build URL (logged) |

---

### server.py

Flask webhook server. Single `/webhook` endpoint accepts payloads from all four platforms.

**Endpoints:**

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook` | Receives CI webhooks from any platform |
| `GET` | `/api/history` | Returns diagnosis history (JSON). Params: `limit`, `platform` |
| `GET` | `/api/stats` | Returns aggregate stats for the dashboard |
| `GET` | `/health` | Health check |
| `GET` | `/` | Serves the dashboard |

**Signature verification:**

Each platform's webhook secret is verified independently:

| Platform | Header checked | Env var |
|---|---|---|
| GitHub | `X-Hub-Signature-256` | `GITHUB_WEBHOOK_SECRET` |
| GitLab | `X-Gitlab-Token` | `GITLAB_WEBHOOK_SECRET` |
| Azure DevOps | Basic auth password | `AZURE_WEBHOOK_SECRET` |
| Jenkins | `X-Jenkins-Signature` | `JENKINS_WEBHOOK_SECRET` |

Secrets are optional but strongly recommended for production.

**Diagnosis history:**

Each diagnosis is appended to `diagnoses.json` (configurable via `HISTORY_FILE`). The file stores the last 200 entries. This is what the dashboard reads.

---

### dashboard/index.html

A self-contained single-file dashboard. No build step, no npm, no dependencies. Open it directly in a browser.

**Features:**

- Live table of all diagnoses, auto-refreshes every 15 seconds
- Filter by platform (GitHub / GitLab / Azure / Jenkins)
- Filter by category (Tests / Deps / Config / Env / Type / Network)
- Filter by confidence (High / Medium / Low)
- Full-text search across root causes and pipeline names
- Click any row → detail panel slides in with full diagnosis, code diff, and link to the PR comment
- Stats bar: total diagnosed, % high confidence, most common failure category, platforms connected
- Works standalone with demo data even when the server is not running

**Connecting to a live server:**

The dashboard fetches from `/api/history` on the same origin. If the server is running at `localhost:8080`, just open `http://localhost:8080` — the server serves the dashboard automatically.

---

## Platform Setup

### GitHub Actions

**Step 1 — Create a Personal Access Token**

Go to GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Required scopes: `Actions` (read), `Contents` (read), `Pull requests` (read/write).

**Step 2 — Add the webhook**

Go to your repo → Settings → Webhooks → Add webhook:

```
Payload URL:   https://your-agent-url/webhook
Content type:  application/json
Secret:        <same as GITHUB_WEBHOOK_SECRET in .env>
Events:        ☑ Workflow runs
```

**Step 3 — Set env vars**

```env
GITHUB_TOKEN=ghp_...
GITHUB_WEBHOOK_SECRET=your-secret
```

**Alternative — workflow-based trigger:**

Instead of a repo webhook, add this file to any repo:

```yaml
# .github/workflows/cicd-agent.yml
name: Notify CI/CD Agent
on:
  workflow_run:
    workflows: ["*"]
    types: [completed]
jobs:
  notify:
    if: ${{ github.event.workflow_run.conclusion == 'failure' }}
    runs-on: ubuntu-latest
    steps:
      - name: Send to agent
        run: |
          curl -X POST "${{ secrets.CICD_AGENT_URL }}/webhook" \
            -H "Content-Type: application/json" \
            -H "X-GitHub-Event: workflow_run" \
            -d '${{ toJson(github.event) }}'
```

---

### GitLab CI

**Step 1 — Create a Personal Access Token**

Go to GitLab → User Settings → Access Tokens. Required scopes: `api`, `read_repository`.

**Step 2 — Add the webhook**

Go to your project → Settings → Webhooks → Add new webhook:

```
URL:                   https://your-agent-url/webhook
Secret token:          <same as GITLAB_WEBHOOK_SECRET in .env>
Trigger:               ☑ Pipeline events
SSL verification:      ☑ Enable (recommended)
```

**Step 3 — Set env vars**

```env
GITLAB_TOKEN=glpat-...
GITLAB_URL=https://gitlab.com          # or your self-hosted URL
GITLAB_WEBHOOK_SECRET=your-secret
```

---

### Azure DevOps

**Step 1 — Create a Personal Access Token**

Go to `dev.azure.com` → User Settings (top right) → Personal access tokens → New Token.

Required scopes:

| Scope | Permission |
|---|---|
| Build | Read |
| Code | Read |
| Test Management | Read |
| Work Items | Read & Write |

Work Items write permission is needed to post PR thread comments.

**Step 2 — Add the Service Hook**

Go to your project → Project Settings → Service hooks → Create subscription:

```
Service:        Web Hooks
Event:          Build completed
Filters:        Build status = Failed
URL:            https://your-agent-url/webhook
Basic auth:     (username: anything, password: AZURE_WEBHOOK_SECRET)
```

**Step 3 — Set env vars**

```env
AZURE_DEVOPS_ORG=your-org-name
AZURE_DEVOPS_PAT=your-pat
AZURE_WEBHOOK_SECRET=your-secret
```

The org name is the slug from your Azure DevOps URL: `dev.azure.com/<org-name>`.

---

### Jenkins

**Step 1 — Create an API token**

Go to Jenkins → your user → Configure → API Token → Add new token. Copy it.

**Step 2 — Install the Generic Webhook Trigger plugin**

Manage Jenkins → Plugins → Available → search "Generic Webhook Trigger" → Install.

**Step 3 — Configure a job to send webhooks**

In each job's configuration → Post-build Actions → Generic Webhook → set the URL to `https://your-agent-url/webhook`.

Or configure globally via Jenkins → Manage Jenkins → Configure System → Generic Webhook.

The agent reads the standard Jenkins `build.complete` payload format. Make sure your webhook plugin sends the full build object including `build.url`, `build.phase`, `build.status`, and `name`.

**Step 4 — Set env vars**

```env
JENKINS_URL=https://jenkins.yourcompany.com
JENKINS_USER=admin
JENKINS_TOKEN=your-api-token
JENKINS_WEBHOOK_SECRET=your-secret
```

Note: Jenkins does not have a native PR comment API. The agent logs the full diagnosis and returns the build URL. To post to a PR, pair Jenkins with a GitHub/GitLab notifier plugin, or add Slack notifications (see Extending the Agent below).

---

## Environment Variables

Copy `.env.example` to `.env` and fill in only the platforms you use. Unused platform variables are safely ignored.

```env
# ── LLM (required) ────────────────────────────────────────────────────────
LLM_MODEL=anthropic/claude-sonnet-4-20250514
LLM_API_KEY=your-provider-api-key
# LLM_API_BASE=              # optional — Ollama, Azure OpenAI, proxies
# Or use provider-specific keys: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, …

# ── GitHub ────────────────────────────────────────────────────────────────
GITHUB_TOKEN=ghp_...
GITHUB_WEBHOOK_SECRET=your-secret

# ── GitLab ────────────────────────────────────────────────────────────────
GITLAB_TOKEN=glpat-...
GITLAB_URL=https://gitlab.com
GITLAB_WEBHOOK_SECRET=your-secret

# ── Azure DevOps ──────────────────────────────────────────────────────────
AZURE_DEVOPS_ORG=your-org-name
AZURE_DEVOPS_PAT=your-pat
AZURE_WEBHOOK_SECRET=your-secret

# ── Jenkins ───────────────────────────────────────────────────────────────
JENKINS_URL=https://jenkins.yourcompany.com
JENKINS_USER=admin
JENKINS_TOKEN=your-api-token
JENKINS_WEBHOOK_SECRET=your-secret

# ── Agent settings ────────────────────────────────────────────────────────
AUTO_OPEN_FIX_PR=false        # set true to auto-open draft PRs (GitHub only, high confidence)
PORT=8080
HISTORY_FILE=diagnoses.json   # path to the diagnosis history file
```

---

## Dashboard

The dashboard is a single HTML file at `dashboard/index.html`. No build tools needed.

**Open standalone (demo data):**

```bash
open dashboard/index.html
```

**Open connected to the live server:**

```bash
python src/server.py
open http://localhost:8080
```

**What you see:**

- Stats bar — total diagnoses, high confidence %, most common failure type, platform count
- Sidebar — filter by platform or confidence level
- Filter bar — filter by failure category, search by keyword
- Diagnosis table — one row per failure, click for full detail
- Detail panel — full root cause, technical explanation, affected files, code diff, link to PR comment

The dashboard auto-refreshes every 15 seconds. Data is read from `/api/history`.

---

## Example Diagnosis Output

**Test failure (GitHub Actions — Redis type mismatch):**

```json
{
  "root_cause": "Redis 4.x returns bytes instead of str — token comparison on line 84 always evaluates to False.",
  "technical_detail": "Upgrading redis-py from 3.5 to 4.x changed cache.get() to return bytes. The == comparison against a str fails silently.",
  "affected_files": [
    {"file": "auth/token_refresh.py", "line": 84, "reason": "bytes vs str comparison"}
  ],
  "suggested_fix": "Decode the cached value before comparing: stored.decode() == token",
  "suggested_fix_code": "-return self.cache.get(f\"token:{token}\") == token\n+stored = self.cache.get(f\"token:{token}\")\n+return stored and stored.decode() == token",
  "confidence": "high",
  "category": "test_failure",
  "related_commit": "Upgrade redis-py to 4.6.0"
}
```

**Config error (Azure DevOps — wrong working directory):**

```json
{
  "root_cause": "npm cannot find package.json because workingDirectory was changed to /frontend but package.json still lives at the repo root.",
  "technical_detail": "The pipeline YAML was updated to set workingDirectory to $(System.DefaultWorkingDirectory)/frontend but package.json was not moved.",
  "affected_files": [
    {"file": "azure-pipelines.yml", "line": 14, "reason": "wrong workingDirectory"}
  ],
  "suggested_fix": "Move package.json into /frontend or revert the workingDirectory setting.",
  "suggested_fix_code": "-workingDirectory: $(System.DefaultWorkingDirectory)/frontend\n+workingDirectory: $(System.DefaultWorkingDirectory)",
  "confidence": "high",
  "category": "config",
  "related_commit": "Move frontend code into /frontend subdirectory"
}
```

**Environment error (GitLab CI — missing secret):**

```json
{
  "root_cause": "STRIPE_SECRET_KEY is not set — Stripe SDK throws AuthenticationError on import.",
  "technical_detail": "The CI job variables block does not include STRIPE_SECRET_KEY. Stripe initialises on import and raises immediately.",
  "affected_files": [
    {"file": ".gitlab-ci.yml", "line": 22, "reason": "missing env var declaration"},
    {"file": "payments/stripe_client.py", "line": 5, "reason": "os.environ[\"STRIPE_SECRET_KEY\"] raises KeyError"}
  ],
  "suggested_fix": "Add STRIPE_SECRET_KEY as a masked CI/CD variable in GitLab project settings, then reference it in the job variables block.",
  "suggested_fix_code": "variables:\n  STRIPE_SECRET_KEY: $STRIPE_SECRET_KEY",
  "confidence": "high",
  "category": "env",
  "related_commit": "Add Stripe payment integration"
}
```

---

## Deployment

### Docker

```bash
docker build -t cicd-agent .
docker run -p 8080:8080 \
  -e LLM_MODEL=your-provider-model \
  -e LLM_API_KEY=your-api-key \
  -e GITHUB_TOKEN=ghp_... \
  -v $(pwd)/diagnoses.json:/app/diagnoses.json \
  cicd-agent
```

### Railway / Render / Fly.io

All three support deploy-from-repo. Set your environment variables in the dashboard and use this start command:

```
gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 60 src.server:app
```

### Systemd (Linux VPS)

```ini
[Unit]
Description=CI/CD Intelligence Agent
After=network.target

[Service]
WorkingDirectory=/opt/cicd-agent
EnvironmentFile=/opt/cicd-agent/.env
ExecStart=/opt/cicd-agent/.venv/bin/gunicorn \
  --bind 0.0.0.0:8080 --workers 2 --timeout 60 src.server:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable cicd-agent
sudo systemctl start cicd-agent
```

---

## Extending the Agent

**Add Slack notifications:**

After `post_comment()` in `server.py`, add:

```python
import requests

def notify_slack(diagnosis: dict, platform: str, context: dict):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return
    requests.post(webhook_url, json={
        "text": f"*[{platform}] Build failure diagnosed*\n>{diagnosis['root_cause']}",
        "attachments": [{
            "color": "danger" if diagnosis["confidence"] == "high" else "warning",
            "fields": [
                {"title": "Pipeline", "value": context.get("pipeline_name",""), "short": True},
                {"title": "Confidence", "value": diagnosis["confidence"], "short": True},
                {"title": "Suggested fix", "value": diagnosis.get("suggested_fix","")},
            ]
        }]
    })
```

**Add a new CI platform:**

In `universal_ci_client.py`:

1. Add detection in `detect_platform()` — check a unique header or payload key
2. Add `is_failure_<platform>()` logic
3. Add `fetch_<platform>_context()` — return the standard context dict
4. Add `post_<platform>_comment()` — post the formatted comment
5. Wire into `fetch_context()` and `post_comment()` router functions

The context dict must have these keys: `log_tail`, `diff`, `test_output`, `commit_messages`, `pipeline_name`, `branch`.

**Customise the Claude prompt:**

Edit `SYSTEM_PROMPT` in `diagnose.py` to add your team's stack, common failure patterns, or internal conventions. The more context you give Claude about your environment, the more specific and accurate the diagnoses will be.

**Persist to a database:**

Replace `append_diagnosis()` in `server.py` with a PostgreSQL or SQLite insert. Update `/api/history` to query instead of reading the JSON file.

**Auto-open fix PRs:**

Set `AUTO_OPEN_FIX_PR=true` in `.env`. The agent will call the GitHub API to create a draft PR branch whenever confidence is `high` and a `suggested_fix_code` is present. Currently GitHub only — GitLab MR creation can be added to `post_gitlab_comment()` similarly.
