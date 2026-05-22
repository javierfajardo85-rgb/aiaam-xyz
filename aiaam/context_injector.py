"""
AIAAM Context Injector — Agente B3
Añade AGENTS.md en repos GitHub con licencia MIT o Apache-2.0 que NO tengan
uno ya. NEVER modifica o reemplaza AGENTS.md existentes.

Reglas de seguridad (invariantes — no cambiar):
  PROHIBIDO: tocar repos que ya tienen AGENTS.md
  PROHIBIDO: URLs externas de aiaam.xyz en el contenido inyectado
  PERMITIDO: contrato MAI-1 auto-contenido (sin dependencias externas)

Plantilla (V3 — actual):
  Contrato MAI-1 embebido directamente en el AGENTS.md.
  No contiene URLs a aiaam.xyz. El formato se propaga, no el producto.

Reglas operativas:
  - Solo repos MIT o Apache-2.0
  - Máximo 3 PRs por ejecución (rate limit de seguridad)
  - Nunca inyecta dos veces el mismo aid (check DB)
  - Zero LLM
  - Tabla InjectedRepo: repo_url, aid, license_spdx, instructions_md, injected_at

Uso:
    python3 context_injector.py                # genera AGENTS.md y guarda en DB
    python3 context_injector.py --aid yt-dlp-v1
    python3 context_injector.py --dry-run      # preview sin escribir en DB
    python3 context_injector.py --force        # re-inyecta aunque ya exista

    python3 context_injector.py --submit-pr                # envía PRs (top 5 pendientes)
    python3 context_injector.py --submit-pr --limit 10     # envía hasta N PRs
    python3 context_injector.py --submit-pr --dry-run      # preview — no abre PRs
    python3 context_injector.py --submit-pr --aid crewai-v1 # un solo PR

    python3 context_injector.py --reapproach --repos sqlalchemy/sqlalchemy,pydantic/pydantic-settings
    python3 context_injector.py --reapproach --repos sqlalchemy/sqlalchemy --dry-run

Requisitos para --submit-pr / --reapproach:
  GITHUB_TOKEN con scopes: repo (fork + push + PR)
"""

import os
import sys
import time
import base64
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from sqlmodel import Session, select
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool, InjectedRepo
from analytics import log_agent_run

load_dotenv()

ALLOWED_LICENSES  = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause"}
_AGENTS_FILENAME  = "AGENTS.md"

# Topics that trigger Version B (agent/LLM-focused template)
_AGENT_TOPICS = {"agents", "llm", "langchain", "crewai", "autogen", "haystack",
                 "openai", "anthropic", "ai", "machine-learning", "nlp"}

MAX_PRS_PER_RUN   = 3   # hard safety limit — never send bursts


# =====================================================================
# TEMPLATES — additive, suggestive, never redirective
# =====================================================================

def _build_agents_md_v3(tool: Tool) -> str:
    """
    Version C (current) — self-contained MAI-1 contract.
    Uses the canonical four-section MAI-1 structure: aid / logic / trust / action.
    No external URLs. The format spreads; no dependency on aiaam.xyz.
    Maintainers own this block and can edit it like any other docs.
    """
    import json as _json

    input_schema  = tool.input_schema  or {}
    output_schema = tool.output_schema or {}

    contract = {
        "aid": tool.aid,
        "logic": {
            "input_schema":  input_schema  if input_schema  else {"type": "any"},
            "output_schema": output_schema if output_schema else {"type": "any"},
        },
        "trust": {
            "reliability_score": round(tool.reliability_score, 2)
                                  if tool.reliability_score is not None else None,
            "latency_ms": tool.latency_ms,
        },
        "action": {
            "install_cmd": tool.install_cmd or "",
            "execute_cmd": tool.execute_cmd or "",
        },
    }

    # Drop None values one level deep inside trust
    contract["trust"] = {k: v for k, v in contract["trust"].items() if v is not None}

    contract_json = _json.dumps(contract, indent=2)

    return (
        "# Agent Instructions\n\n"
        "This file provides guidance for AI coding agents (Claude Code, "
        "GitHub Copilot, Cursor, Gemini CLI) working in this repository.\n\n"
        "## Development Guidelines\n"
        "- Read the README and CONTRIBUTING docs before making changes\n"
        "- Follow the coding style and test conventions already in use\n"
        "- Check open issues and PRs to avoid duplicate work\n"
        "- Run existing tests before submitting\n\n"
        "## AI Agent Usage (MAI-1)\n\n"
        "Machine-readable contract for this library. "
        "AI agents can use this block to discover how to install and invoke "
        "the library without parsing the full README. "
        "This block is self-contained — no external services are queried.\n\n"
        f"```json\n{contract_json}\n```\n"
    )


# Aliases — all point to V3
def _build_agents_md_v1(tool: Tool) -> str:
    return _build_agents_md_v3(tool)

def _build_agents_md_v2(tool: Tool) -> str:
    return _build_agents_md_v3(tool)

# Used by --regenerate mode (was missing — now fixed)
_build_mcp_agents_md = _build_agents_md_v3


def _schema_summary(schema: dict) -> str:
    """Single-line human/agent-readable summary of a MAI-1 schema dict."""
    if not schema:
        return "unspecified"
    t = schema.get("type", "")
    fmt = schema.get("format", "")
    if fmt and isinstance(fmt, list):
        fmt = " | ".join(str(f) for f in fmt[:3])
    return f"{t} ({fmt})" if fmt else t or "unspecified"


def _tool_display_name(aid: str) -> str:
    """'langchain-v1' → 'langchain', 'openai-whisper-v1' → 'openai-whisper'."""
    import re
    return re.sub(r"-v\d+$", "", aid)


def _repo_has_agent_topics(source_url: str) -> bool:
    """Check GitHub topics to decide Version A vs Version B template."""
    pair = _owner_repo(source_url)
    if not pair:
        return False
    owner, repo = pair
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/topics",
            headers={**_gh_headers(), "Accept": "application/vnd.github.mercy-preview+json"},
            timeout=10,
        )
        if r.status_code == 200:
            topics = set(r.json().get("names", []))
            return bool(topics & _AGENT_TOPICS)
    except Exception:
        pass
    return False


def build_agents_md(tool: Tool, source_url: str = "") -> str:
    """
    Returns the AGENTS.md content to use for a repo with NO existing AGENTS.md.
    Always uses V3 (self-contained MAI-1 contract, no external URLs).
    Never receives existing content — we only act on repos without AGENTS.md.
    """
    return _build_agents_md_v3(tool)


# =====================================================================
# GITHUB API — sin LLM
# =====================================================================

def _gh_headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _owner_repo(source_url: str) -> Optional[tuple[str, str]]:
    if "github.com" not in source_url:
        return None
    parts = source_url.rstrip("/").split("/")
    if len(parts) < 5:
        return None
    return parts[-2], parts[-1]


def fetch_license(source_url: str) -> Optional[str]:
    """Devuelve el SPDX identifier de la licencia. None si no es GitHub o no se puede leer."""
    pair = _owner_repo(source_url)
    if not pair:
        return None
    owner, repo = pair
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/license",
            headers=_gh_headers(), timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("license", {}).get("spdx_id")
    except Exception:
        pass
    return None


def fetch_agents_md(source_url: str) -> Optional[str]:
    """
    Intenta descargar AGENTS.md del repo.
    Devuelve el contenido como string, o None si no existe.
    """
    pair = _owner_repo(source_url)
    if not pair:
        return None
    owner, repo = pair
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{_AGENTS_FILENAME}",
            headers=_gh_headers(), timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            content_b64 = data.get("content", "")
            return base64.b64decode(content_b64).decode("utf-8", errors="replace")
        if r.status_code == 404:
            return None
    except Exception:
        pass
    return None


# =====================================================================
# CORE — inyecta un tool
# =====================================================================

def inject_tool(
    tool: Tool,
    session: Session,
    dry_run: bool = False,
    force: bool = False,
) -> Optional[InjectedRepo]:
    """
    Genera y persiste el AGENTS.md para un tool.
    Devuelve el InjectedRepo creado/actualizado, o None si se omite.
    """
    # Nunca inyectar dos veces el mismo aid (salvo --force)
    existing = session.exec(
        select(InjectedRepo).where(InjectedRepo.aid == tool.aid)
    ).first()
    if existing and not force:
        print(f"    SKIP — ya inyectado el {existing.injected_at.strftime('%Y-%m-%d')}")
        return None

    # Verificar licencia
    license_spdx = fetch_license(tool.source_url)
    if license_spdx not in ALLOWED_LICENSES:
        print(f"    SKIP — licencia: {license_spdx or 'desconocida'} (requiere MIT o Apache-2.0)")
        return None

    # REGLA 1 — Skip repos that already have AGENTS.md (never touch existing files)
    existing_agents_md = fetch_agents_md(tool.source_url)
    if existing_agents_md is not None:
        print(f"    SKIP — repo already has AGENTS.md (never overwrite)")
        return None

    content = build_agents_md(tool, source_url=tool.source_url)
    print(f"    OK   — licencia {license_spdx} · CREATE · {len(content)} chars")

    if dry_run:
        print(f"    DRY  — no se escribe en DB")
        print(f"    Preview:\n      " + content[:200].replace("\n", "\n      ") + "...")
        return None

    record = InjectedRepo(
        repo_url=tool.source_url,
        aid=tool.aid,
        license_spdx=license_spdx,
        instructions_md=content,
        injected_at=datetime.utcnow(),
    )
    if existing:
        existing.license_spdx    = license_spdx
        existing.instructions_md = content
        existing.injected_at     = datetime.utcnow()
        session.add(existing)
        session.commit()
        return existing

    session.add(record)
    session.commit()
    return record


# =====================================================================
# PR SUBMISSION — fork → branch → commit → pull request
# =====================================================================

_PR_BRANCH        = "mai1-agents-md"        # legacy (open PRs still use this)
_PR_BRANCH_V3     = "mai1-inline-contract"  # all new PRs and --reapproach

_PR_TITLE         = "docs: add MAI-1 agent contract to AGENTS.md"
_PR_BODY          = """\
Adds an `AGENTS.md` containing a self-contained MAI-1 tool contract.

**Why this helps:** AI coding agents (Claude Code, GitHub Copilot, Cursor, \
Gemini CLI) can read the contract block to discover how to install and invoke \
this library without parsing the full README or hallucinating the API.

The block follows the MAI-1 four-section structure:

```json
{{
  "aid": "{aid}",
  "logic":  {{ "input_schema": {{...}},  "output_schema": {{...}} }},
  "trust":  {{ "reliability_score": 0.0, "latency_ms": 0       }},
  "action": {{ "install_cmd": "...",     "execute_cmd": "..."   }}
}}
```

No external services are queried. The file lives entirely in this repo \
and can be maintained by the team like any other documentation.

Feel free to close if this doesn't fit the project.
"""

# --reapproach only: used for repos that closed the V1/V2 PR due to external URL
_PR_TITLE_REAPPROACH = "docs: add MAI-1 agent contract to AGENTS.md (self-contained, no external URLs)"
_PR_BODY_REAPPROACH  = """\
Following up after the earlier PR was closed.

The previous version referenced an external URL. This version is entirely \
self-contained: the MAI-1 contract block lives in this file, no external \
services are queried, and there are no automatic network calls.

**What it adds:** a machine-readable block that lets AI coding agents \
(Claude Code, GitHub Copilot, Cursor, Gemini CLI) discover how to install \
and invoke this library — without parsing the full README.

```json
{{
  "aid": "{aid}",
  "logic":  {{ "input_schema": {{...}}, "output_schema": {{...}} }},
  "trust":  {{ "reliability_score": 0.0 }},
  "action": {{ "install_cmd": "...", "execute_cmd": "..." }}
}}
```

No build step, no dependency, no network call. Maintainers can edit it \
like any other docs. Feel free to close if it still doesn't fit.
"""

# Repos that explicitly declined — never re-approach these
_REAPPROACH_BLOCKLIST = {
    "sqlalchemy/sqlalchemy",        # "can't have random URLs in repo"
    "pydantic/pydantic-settings",   # "not interested in 3rd-party MCP registry"
    "microsoft/playwright",         # "doesn't improve our workflows"
    "pola-rs/polars",               # "doesn't fit our project"
    "apache/arrow",                 # "not a good idea"
    "run-llama/llama_index",        # "nah no thanks"
    "huggingface/huggingface_hub",  # "Not gonna happen"
    "agronholm/apscheduler",        # "don't involve 3rd-party tools"
    "puppeteer/puppeteer",          # "do not plan to merge"
}


def _check_repo_status(owner: str, repo: str) -> tuple[bool, str]:
    """
    Verify the repo is accessible and not archived/disabled before forking.
    Returns (True, "ok") or (False, reason_string).
    """
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=_gh_headers(),
            timeout=10,
        )
        if r.status_code == 404:
            return False, "repo deleted or private"
        if r.status_code == 403:
            return False, "repo access forbidden"
        if r.status_code != 200:
            return False, f"GitHub API {r.status_code}"
        data = r.json()
        if data.get("archived"):
            return False, "repo is archived"
        if data.get("disabled"):
            return False, "repo is disabled"
        return True, "ok"
    except Exception as exc:
        return False, f"network error: {exc}"


def _get_gh_user(token: str) -> Optional[str]:
    """Return the authenticated GitHub username."""
    try:
        r = httpx.get(
            "https://api.github.com/user",
            headers={**_gh_headers(), "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("login")
    except Exception:
        pass
    return None


def _fork_repo(owner: str, repo: str, token: str) -> Optional[str]:
    """Fork a repo. Returns the fork's full_name (gh_user/repo), or None on error."""
    try:
        r = httpx.post(
            f"https://api.github.com/repos/{owner}/{repo}/forks",
            headers={**_gh_headers(), "Authorization": f"Bearer {token}"},
            json={},
            timeout=30,
        )
        if r.status_code in (200, 202):
            return r.json().get("full_name")
    except Exception:
        pass
    return None


def _get_default_branch_sha(full_name: str, token: str) -> Optional[tuple[str, str]]:
    """Return (default_branch_name, HEAD SHA) for a fork."""
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{full_name}",
            headers={**_gh_headers(), "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        branch = data.get("default_branch", "main")
        sha_r = httpx.get(
            f"https://api.github.com/repos/{full_name}/git/ref/heads/{branch}",
            headers={**_gh_headers(), "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if sha_r.status_code != 200:
            return None
        sha = sha_r.json()["object"]["sha"]
        return branch, sha
    except Exception:
        return None


def _create_branch(full_name: str, branch: str, sha: str, token: str) -> bool:
    """Create a new branch pointing at sha. Returns True on success (or already exists)."""
    try:
        r = httpx.post(
            f"https://api.github.com/repos/{full_name}/git/refs",
            headers={**_gh_headers(), "Authorization": f"Bearer {token}"},
            json={"ref": f"refs/heads/{branch}", "sha": sha},
            timeout=10,
        )
        return r.status_code in (201, 422)  # 422 = already exists
    except Exception:
        return False


def _put_agents_md(full_name: str, branch: str, content: str, token: str) -> bool:
    """Create or update AGENTS.md on the branch. Returns True on success."""
    headers = {**_gh_headers(), "Authorization": f"Bearer {token}"}
    # Check if file exists to get its SHA (required for updates)
    sha = None
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{full_name}/contents/{_AGENTS_FILENAME}",
            headers=headers, params={"ref": branch}, timeout=10,
        )
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass

    payload: dict = {
        "message": f"Add {_AGENTS_FILENAME} — AI agent development guidelines",
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = httpx.put(
            f"https://api.github.com/repos/{full_name}/contents/{_AGENTS_FILENAME}",
            headers=headers, json=payload, timeout=20,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


def _open_pr(owner: str, repo: str, fork_user: str, branch: str,
             aid: str, token: str,
             title: str = _PR_TITLE,
             body_template: str = _PR_BODY) -> Optional[str]:
    """Open a PR from fork_user:branch → owner:default_branch. Returns PR URL."""
    body = body_template.format(aid=aid)
    headers = {**_gh_headers(), "Authorization": f"Bearer {token}"}
    for base in ("main", "master"):
        try:
            r = httpx.post(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=headers,
                json={
                    "title": title,
                    "body": body,
                    "head": f"{fork_user}:{branch}",
                    "base": base,
                    "maintainer_can_modify": True,
                },
                timeout=20,
            )
            if r.status_code == 201:
                return r.json().get("html_url")
            if r.status_code != 422:
                break
        except Exception:
            pass
    return None


def _delete_fork_branch(fork_name: str, branch: str, token: str) -> bool:
    """Delete branch from fork. Returns True if gone (204) or already gone (422)."""
    try:
        r = httpx.delete(
            f"https://api.github.com/repos/{fork_name}/git/refs/heads/{branch}",
            headers={**_gh_headers(), "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return r.status_code in (204, 422)
    except Exception:
        return False


def submit_pr_for_record(
    record: InjectedRepo,
    tool: Tool,
    gh_user: str,
    token: str,
    dry_run: bool = False,
) -> Optional[str]:
    """
    Full flow: fork → branch → commit AGENTS.md → open PR.
    Returns PR URL on success, None otherwise.
    """
    pair = _owner_repo(record.repo_url)
    if not pair:
        print(f"    ERROR — cannot parse owner/repo from {record.repo_url}")
        return None
    owner, repo = pair

    print(f"    repo     : {owner}/{repo}")

    # REGLA 1 — Final safety check: abort if upstream already has AGENTS.md
    upstream_agents_md = fetch_agents_md(record.repo_url)
    if upstream_agents_md is not None:
        print(f"    SKIP — upstream already has AGENTS.md (never overwrite)")
        return None

    if dry_run:
        print(f"    dry-run  : would fork → branch {_PR_BRANCH_V3} → commit → PR")
        print(f"    content  : {len(record.instructions_md)} chars")
        return "DRY_RUN"

    # 1. Fork
    print(f"    forking  ...", end=" ", flush=True)
    fork_name = _fork_repo(owner, repo, token)
    if not fork_name:
        print("FAIL")
        return None
    print(f"OK ({fork_name})")
    time.sleep(3)  # GitHub needs a moment to initialise the fork

    # 2. Get default branch SHA
    result = _get_default_branch_sha(fork_name, token)
    if not result:
        print(f"    ERROR — cannot get default branch SHA for {fork_name}")
        return None
    default_branch, head_sha = result

    # 3. Create branch (V3 branch name for all new PRs)
    print(f"    branch   : {_PR_BRANCH_V3} from {default_branch}@{head_sha[:7]} ...", end=" ", flush=True)
    if not _create_branch(fork_name, _PR_BRANCH_V3, head_sha, token):
        print("FAIL")
        return None
    print("OK")

    # 4. Commit AGENTS.md
    print(f"    commit   : AGENTS.md ...", end=" ", flush=True)
    if not _put_agents_md(fork_name, _PR_BRANCH_V3, record.instructions_md, token):
        print("FAIL")
        return None
    print("OK")

    # 5. Open PR
    print(f"    PR       : opening against {owner}/{repo} ...", end=" ", flush=True)
    pr_url = _open_pr(owner, repo, gh_user, _PR_BRANCH_V3, tool.aid, token)
    if pr_url:
        print(f"OK → {pr_url}")
    else:
        print("FAIL (PR may already exist or base branch mismatch)")
    return pr_url


def run_submit_pr(
    aid: Optional[str] = None,
    limit: int = MAX_PRS_PER_RUN,
    dry_run: bool = False,
) -> dict:
    """Submit PRs for InjectedRepo records that don't have pr_url yet.
    Hard cap: MAX_PRS_PER_RUN (3) per execution regardless of --limit."""
    limit = min(limit, MAX_PRS_PER_RUN)  # enforce hard cap
    token = os.getenv("GITHUB_TOKEN", "")
    if not token and not dry_run:
        print("[injector] ERROR: GITHUB_TOKEN env var required for --submit-pr")
        return {"submitted": 0, "failed": 0}

    gh_user = _get_gh_user(token) if not dry_run else "dry-run-user"
    if not gh_user and not dry_run:
        print("[injector] ERROR: cannot authenticate with GITHUB_TOKEN")
        return {"submitted": 0, "failed": 0}

    init_db()
    results = {"submitted": 0, "failed": 0, "skipped_no_tool": 0, "skipped_archived": 0}
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[injector] {ts} — submit-pr mode (gh_user={gh_user}, limit={limit}){' [DRY-RUN]' if dry_run else ''}\n")

    with Session(engine) as session:
        if aid:
            records = session.exec(
                select(InjectedRepo).where(InjectedRepo.aid == aid)
            ).all()
        else:
            records = session.exec(
                select(InjectedRepo).where(InjectedRepo.pr_url == None)
            ).all()
            records = list(records)[:limit]

        print(f"[injector] {len(records)} records to process\n")

        for record in records:
            tool = session.get(Tool, record.aid)
            if not tool:
                print(f"  → {record.aid:<35} SKIP — tool not in catalog")
                results["skipped_no_tool"] += 1
                continue

            # Check repo is alive and not archived before forking
            pair = _owner_repo(record.repo_url)
            if not pair:
                print(f"  → {record.aid:<35} SKIP — cannot parse repo URL")
                results["failed"] += 1
                continue

            owner, repo = pair
            ok, reason = _check_repo_status(owner, repo)
            if not ok:
                print(f"  → {record.aid:<35} SKIP — {reason}")
                results["skipped_archived"] += 1
                continue

            print(f"  → {record.aid}")
            pr_url = submit_pr_for_record(record, tool, gh_user, token, dry_run=dry_run)

            if pr_url and pr_url != "DRY_RUN":
                record.pr_url = pr_url
                record.pr_submitted_at = datetime.utcnow()
                session.add(record)
                session.commit()
                results["submitted"] += 1
            elif pr_url == "DRY_RUN":
                results["submitted"] += 1
            else:
                results["failed"] += 1

            print()
            time.sleep(2)  # courtesy between PRs

    # Summary table
    total = sum(results.values())
    print(f"\n{'─'*50}")
    print(f"  submit-pr summary")
    print(f"{'─'*50}")
    print(f"  submitted       : {results['submitted']}")
    print(f"  failed          : {results['failed']}")
    print(f"  skipped (archived/deleted) : {results['skipped_archived']}")
    print(f"  skipped (no tool in DB)    : {results['skipped_no_tool']}")
    print(f"  total processed : {total}")
    print(f"{'─'*50}")
    return results


# =====================================================================
# REAPPROACH — re-submit V3 PR to repos that closed V1/V2 without merge
# =====================================================================

# Targets: only repos closed without explicit reasoning (silent close)
# Explicit rejections are in _REAPPROACH_BLOCKLIST — never touched again.
_REAPPROACH_TARGETS = [
    ("mongodb",  "mongo-python-driver", "mongodb/mongo-python-driver/pull/2807"),
    ("scrapy",   "scrapy",              "scrapy/scrapy/pull/7537"),
    ("pydantic", "pydantic-ai",         "pydantic/pydantic-ai/pull/5541"),
]


def run_reapproach(
    repos: Optional[str] = None,   # comma-separated "owner/repo" overrides
    dry_run: bool = False,
) -> dict:
    """
    Re-submit the V3 (self-contained) PR to repos that closed the V1/V2 PR
    without explanation. Blocklisted repos (explicit rejections) are never touched.

    Flow per target:
      1. Verify upstream still has no AGENTS.md (skip if merged by now)
      2. Delete the old mai1-agents-md branch from our fork (if present)
      3. Fork → new branch mai1-inline-contract → commit V3 content → open PR
    """
    token = os.getenv("GITHUB_TOKEN", "")
    if not token and not dry_run:
        print("[reapproach] ERROR: GITHUB_TOKEN required")
        return {}

    gh_user = _get_gh_user(token) if not dry_run else "dry-run-user"
    if not gh_user and not dry_run:
        print("[reapproach] ERROR: cannot authenticate")
        return {}

    # Build target list
    if repos:
        targets = []
        for slug in repos.split(","):
            slug = slug.strip()
            parts = slug.split("/")
            if len(parts) == 2:
                targets.append((parts[0], parts[1], None))
            else:
                print(f"[reapproach] WARN: bad slug '{slug}', skip")
    else:
        targets = list(_REAPPROACH_TARGETS)

    results = {"submitted": 0, "skipped": 0, "failed": 0}
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[reapproach] {ts} — {len(targets)} targets{' [DRY-RUN]' if dry_run else ''}\n")

    init_db()

    with Session(engine) as session:
        for owner, repo, _old_pr in targets:
            slug = f"{owner}/{repo}"
            print(f"  → {slug}")

            # Safety: blocklist check
            if slug in _REAPPROACH_BLOCKLIST:
                print(f"    SKIP — in blocklist (explicit rejection)")
                results["skipped"] += 1
                print()
                continue

            # Find InjectedRepo record
            repo_url = f"https://github.com/{slug}"
            record = session.exec(
                select(InjectedRepo).where(InjectedRepo.repo_url == repo_url)
            ).first()
            if not record:
                print(f"    SKIP — no InjectedRepo record found for {repo_url}")
                results["skipped"] += 1
                print()
                continue

            tool = session.get(Tool, record.aid)
            if not tool:
                print(f"    SKIP — tool {record.aid!r} not in catalog")
                results["skipped"] += 1
                print()
                continue

            # Check upstream: if AGENTS.md already exists, skip
            upstream = fetch_agents_md(repo_url)
            if upstream is not None:
                print(f"    SKIP — upstream already has AGENTS.md")
                results["skipped"] += 1
                print()
                continue

            # Rebuild content with V3 template
            content = _build_agents_md_v3(tool)

            if dry_run:
                print(f"    dry-run  : would delete old branch, create {_PR_BRANCH_V3}, open PR")
                print(f"    preview  : {content.splitlines()[0]}")
                results["submitted"] += 1
                print()
                continue

            fork_name = f"{gh_user}/{repo}"

            # 1. Delete legacy branch from fork (ignore errors — may not exist)
            print(f"    cleanup  : deleting {_PR_BRANCH} from fork ...", end=" ", flush=True)
            _delete_fork_branch(fork_name, _PR_BRANCH, token)
            print("OK")

            # 2. Fork (idempotent)
            print(f"    forking  ...", end=" ", flush=True)
            fork_result = _fork_repo(owner, repo, token)
            if not fork_result:
                print("FAIL")
                results["failed"] += 1
                print()
                continue
            print(f"OK ({fork_result})")
            time.sleep(3)

            # 3. Get default branch SHA
            branch_result = _get_default_branch_sha(fork_result, token)
            if not branch_result:
                print(f"    ERROR — cannot get SHA for {fork_result}")
                results["failed"] += 1
                print()
                continue
            default_branch, head_sha = branch_result

            # 4. Create new branch
            print(f"    branch   : {_PR_BRANCH_V3} from {default_branch}@{head_sha[:7]} ...", end=" ", flush=True)
            if not _create_branch(fork_result, _PR_BRANCH_V3, head_sha, token):
                print("FAIL")
                results["failed"] += 1
                print()
                continue
            print("OK")

            # 5. Commit V3 content
            print(f"    commit   : AGENTS.md ...", end=" ", flush=True)
            if not _put_agents_md(fork_result, _PR_BRANCH_V3, content, token):
                print("FAIL")
                results["failed"] += 1
                print()
                continue
            print("OK")

            # 6. Open PR with reapproach title/body
            print(f"    PR       : opening against {slug} ...", end=" ", flush=True)
            pr_url = _open_pr(
                owner, repo, gh_user, _PR_BRANCH_V3, tool.aid, token,
                title=_PR_TITLE_REAPPROACH,
                body_template=_PR_BODY_REAPPROACH,
            )
            if pr_url:
                print(f"OK → {pr_url}")
                record.pr_url = pr_url
                record.pr_submitted_at = datetime.utcnow()
                session.add(record)
                session.commit()
                results["submitted"] += 1
            else:
                print("FAIL")
                results["failed"] += 1

            print()
            time.sleep(2)

    print(f"{'─'*50}")
    print(f"  reapproach summary")
    print(f"{'─'*50}")
    print(f"  submitted : {results['submitted']}")
    print(f"  skipped   : {results['skipped']}")
    print(f"  failed    : {results['failed']}")
    print(f"{'─'*50}")
    return results


# =====================================================================
# REGENERATE — rebuild all AGENTS.md from DB, zero network calls
# =====================================================================

def run_regenerate(aid: Optional[str] = None, dry_run: bool = False) -> dict:
    """
    Regenerate instructions_md for all InjectedRepo records using the
    new MCP-aware template. Reads Tool fields from DB — no GitHub API,
    no LLM calls. Does NOT submit PRs (run --submit-pr separately).
    """
    import time as _time
    init_db()
    _t0 = _time.time()
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[injector] {ts} — regenerate mode (MCP template){' [DRY-RUN]' if dry_run else ''}")

    results = {"updated": 0, "skipped": 0, "missing_tool": 0}

    with Session(engine) as session:
        if aid:
            records = session.exec(
                select(InjectedRepo).where(InjectedRepo.aid == aid)
            ).all()
        else:
            records = session.exec(select(InjectedRepo)).all()

        print(f"[injector] {len(records)} records to regenerate\n")

        for record in records:
            tool = session.get(Tool, record.aid)
            if not tool:
                print(f"  → {record.aid}  SKIP — tool not in DB")
                results["missing_tool"] += 1
                continue

            new_md = _build_mcp_agents_md(tool)

            if dry_run:
                print(f"  → {record.aid}  DRY — {len(new_md)} chars preview:")
                print("    " + new_md.split("\n")[0])   # show first line only
                results["updated"] += 1
                continue

            record.instructions_md = new_md
            record.injected_at = datetime.utcnow()
            session.add(record)

            print(f"  → {record.aid}  OK   — {len(new_md)} chars")
            results["updated"] += 1

        if not dry_run:
            session.commit()

    elapsed = int(_time.time() - _t0)
    print(
        f"\n[injector] regenerate done — "
        f"updated={results['updated']} skipped={results['skipped']} "
        f"missing_tool={results['missing_tool']} ({elapsed}s)"
    )
    if not dry_run:
        log_agent_run(
            agent_code="B3", agent_name="Context Injector",
            items_processed=results["updated"] + results["missing_tool"],
            items_new=results["updated"], items_failed=results["missing_tool"],
            duration_s=elapsed,
            summary=f"regenerate MCP template: updated={results['updated']}",
        )
    return results


# =====================================================================
# MAIN RUN
# =====================================================================

def run(
    aid: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    import time as _time
    init_db()
    _t0 = _time.time()
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[injector] {ts} — generating AGENTS.md sections...")

    results = {"injected": 0, "skipped": 0}

    with Session(engine) as session:
        if aid:
            tool = session.get(Tool, aid)
            if not tool:
                print(f"[injector] ERROR: aid '{aid}' not found")
                return results
            tools = [tool]
        else:
            tools = session.exec(
                select(Tool).where(Tool.source_url.contains("github.com"))
            ).all()

        print(f"[injector] {len(tools)} tools GitHub a procesar\n")

        for tool in tools:
            print(f"  → {tool.aid}")
            record = inject_tool(tool, session, dry_run=dry_run, force=force)
            if record:
                results["injected"] += 1
            else:
                results["skipped"] += 1
            time.sleep(0.5)  # cortesía con GitHub API

    print(f"\n[injector] done — injected={results['injected']} skipped={results['skipped']}")
    if not dry_run:
        log_agent_run(
            agent_code="B3", agent_name="Context Injector",
            items_processed=results["injected"] + results["skipped"],
            items_new=results["injected"], items_failed=0,
            duration_s=int(_time.time() - _t0),
            summary=f"injected={results['injected']} skipped={results['skipped']}",
        )
    return results


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Context Injector")
    parser.add_argument("--aid",         type=str,  help="Procesar un tool específico por aid")
    parser.add_argument("--dry-run",     action="store_true", help="Preview sin escribir en DB ni abrir PRs")
    parser.add_argument("--force",       action="store_true", help="Re-inyecta aunque ya exista")
    parser.add_argument("--submit-pr",   action="store_true", help="Abre PRs en GitHub para los registros sin pr_url")
    parser.add_argument("--regenerate",  action="store_true", help="Regenera instructions_md con template V3 (sin red, sin LLM)")
    parser.add_argument("--reapproach",  action="store_true", help="Re-abre PRs cerrados (V3, sin URLs) para targets silenciosos")
    parser.add_argument("--repos",       type=str,  help="Para --reapproach: lista de owner/repo separados por coma")
    parser.add_argument("--limit",       type=int, default=5, help="Máximo de PRs a abrir (default: 5)")
    args = parser.parse_args()

    if args.reapproach:
        run_reapproach(repos=args.repos, dry_run=args.dry_run)
    elif args.submit_pr:
        run_submit_pr(aid=args.aid, limit=args.limit, dry_run=args.dry_run)
    elif args.regenerate:
        run_regenerate(aid=args.aid, dry_run=args.dry_run)
    else:
        run(aid=args.aid, dry_run=args.dry_run, force=args.force)
