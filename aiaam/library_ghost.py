"""
AIAAM Library Ghost — Agente B7
Monitoriza issues recientes en repos de LangChain, CrewAI, AutoGPT y Haystack.
Cuando detecta un issue relevante (alguien busca herramientas/integraciones),
usa Sonnet para redactar un snippet de comentario y lo imprime para envío manual.

Principio de coste máximo:
  - GitHub API + regex: zero LLM
  - Sonnet: SOLO si issue es relevante Y hay tool(s) que encajan — ~200 tokens
  - Límite estricto: MAX_SNIPPETS_PER_MONTH = 10 (guardado en ghost_state.json)
  - NUNCA auto-publica — el humano copia y pega

Uso:
    python3 library_ghost.py             # escanea repos configurados
    python3 library_ghost.py --dry-run   # sin Sonnet, muestra issues relevantes
    python3 library_ghost.py --since 48  # issues de las últimas N horas (default 48)
    python3 library_ghost.py --reset     # resetea el contador mensual (uso admin)
"""

import json
import os
import re
import sys
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from anthropic import Anthropic
from sqlmodel import Session, select
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool

load_dotenv()

ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY")
GITHUB_TOKEN           = os.getenv("GITHUB_TOKEN")
MODEL_SONNET           = "claude-sonnet-4-6"
MAX_SNIPPETS_PER_MONTH = 10
STATE_FILE             = Path(__file__).resolve().parent / "ghost_state.json"
SINCE_HOURS_DEFAULT    = 48

REPOS = [
    "langchain-ai/langchain",
    "crewAIInc/crewAI",
    "Significant-Gravitas/AutoGPT",
    "deepset-ai/haystack",
]

# Keywords that indicate someone is looking for tools/integrations
_RELEVANT_PATTERN = re.compile(
    r"\b(tool|plugin|integration|connector|extension|package|library|"
    r"how\s+to\s+use|looking\s+for|recommend|catalog|registry|"
    r"install|execute|automate|workflow|agent|pipeline)\b",
    re.IGNORECASE,
)

client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# =====================================================================
# STATE — monthly counter + processed issue IDs
# =====================================================================

def _load_state() -> dict:
    month = datetime.utcnow().strftime("%Y-%m")
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            if state.get("month") != month:
                # New month — reset counter, keep processed IDs for dedup
                state = {"month": month, "snippets_this_month": 0, "processed_ids": []}
            return state
        except Exception:
            pass
    return {"month": month, "snippets_this_month": 0, "processed_ids": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# =====================================================================
# GITHUB — fetch recent issues (zero LLM)
# =====================================================================

def _gh_headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_issues(repo: str, since_hours: int = SINCE_HOURS_DEFAULT) -> list[dict]:
    """Returns open issues created/updated in the last since_hours hours."""
    since_dt = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    url = f"https://api.github.com/repos/{repo}/issues"
    params = {
        "state": "open",
        "sort": "created",
        "direction": "desc",
        "since": since_dt,
        "per_page": 20,
    }
    try:
        r = httpx.get(url, headers=_gh_headers(), params=params, timeout=15)
        if r.status_code == 200:
            # Filter out pull requests (GitHub issues API returns both)
            return [i for i in r.json() if "pull_request" not in i]
        print(f"  [ghost] {repo}: HTTP {r.status_code}")
        return []
    except Exception as exc:
        print(f"  [ghost] {repo}: {exc}")
        return []


# =====================================================================
# RELEVANCE — regex, zero LLM
# =====================================================================

def _is_relevant(issue: dict) -> bool:
    text = f"{issue.get('title', '')} {issue.get('body', '') or ''}"
    return bool(_RELEVANT_PATTERN.search(text))


def _keywords_from_issue(issue: dict) -> list[str]:
    """Extract candidate search keywords from title."""
    text = issue.get("title", "").lower()
    tokens = re.findall(r"[a-z][a-z0-9\-]{2,}", text)
    stopwords = {"how", "the", "and", "for", "with", "use", "get", "add", "can", "not",
                 "are", "this", "that", "from", "what", "does", "any", "using", "when"}
    return [t for t in tokens if t not in stopwords][:5]


# =====================================================================
# TOOL MATCH — keyword search in DB, zero LLM
# =====================================================================

def _find_matching_tools(session: Session, issue: dict) -> list[Tool]:
    """Find up to 3 relevant tools in the catalog using keyword matching."""
    keywords = _keywords_from_issue(issue)
    if not keywords:
        return []

    from sqlalchemy import cast, String, or_
    matches = set()
    for kw in keywords:
        pattern = f"%{kw}%"
        tools = session.exec(
            select(Tool)
            .where(
                Tool.verified.is_not(False),
                Tool.status.is_not("dead"),
                or_(
                    Tool.aid.ilike(pattern),
                    cast(Tool.input_schema,  String).ilike(pattern),
                    cast(Tool.output_schema, String).ilike(pattern),
                    Tool.install_cmd.ilike(pattern),
                    Tool.execute_cmd.ilike(pattern),
                )
            )
            .order_by(Tool.reliability_score.desc())
            .limit(3)
        ).all()
        for t in tools:
            matches.add(t.aid)
            if len(matches) >= 3:
                break
        if len(matches) >= 3:
            break

    return [session.get(Tool, aid) for aid in list(matches)[:3] if session.get(Tool, aid)]


# =====================================================================
# LLM — Sonnet, max 10/month, ~200 tokens
# =====================================================================

def generate_snippet(issue: dict, tools: list[Tool]) -> Optional[str]:
    """
    Redacta un comentario de GitHub con Sonnet.
    Max 60 palabras. Solo se llama si hay tools relevantes.
    ~200 tokens por llamada.
    """
    if not client:
        return None

    tools_block = "\n".join(
        f"- {t.aid}: install=`{t.install_cmd or 'n/a'}`, run=`{t.execute_cmd or 'n/a'}`"
        for t in tools
    )
    prompt = (
        f"A developer posted this GitHub issue:\n"
        f"Title: {issue['title']}\n"
        f"Body (first 300 chars): {(issue.get('body') or '')[:300]}\n\n"
        f"Relevant tools available at aiaam.xyz:\n{tools_block}\n\n"
        f"Write a SHORT, genuinely helpful GitHub comment (max 60 words) suggesting "
        f"these tools. Mention aiaam.xyz once. Be concrete, not promotional. "
        f"Output ONLY the comment text."
    )
    try:
        resp = client.messages.create(
            model=MODEL_SONNET,
            max_tokens=220,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        print(f"  [ghost] sonnet error: {exc}")
        return None


# =====================================================================
# MAIN RUN
# =====================================================================

def run(
    repos: list[str] = REPOS,
    since_hours: int = SINCE_HOURS_DEFAULT,
    dry_run: bool = False,
) -> dict:
    init_db()
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"\n[ghost] {ts} [{mode}] — escaneando {len(repos)} repos (últimas {since_hours}h)...")

    state = _load_state()
    remaining = MAX_SNIPPETS_PER_MONTH - state["snippets_this_month"]
    print(f"[ghost] snippets este mes: {state['snippets_this_month']}/{MAX_SNIPPETS_PER_MONTH} "
          f"(quedan {remaining})\n")

    summary = {"issues_scanned": 0, "relevant": 0, "snippets": 0, "skipped_budget": 0}

    with Session(engine) as session:
        for repo in repos:
            print(f"  [{repo}]")
            issues = fetch_issues(repo, since_hours)

            for issue in issues:
                summary["issues_scanned"] += 1
                issue_id = issue["id"]

                if issue_id in state["processed_ids"]:
                    continue

                if not _is_relevant(issue):
                    continue

                summary["relevant"] += 1
                tools = _find_matching_tools(session, issue)

                if not tools:
                    print(f"    #{issue['number']} — relevante pero sin tools que encajen: {issue['title'][:60]}")
                    state["processed_ids"].append(issue_id)
                    continue

                print(f"    #{issue['number']} — {issue['title'][:60]}")
                print(f"    tools: {[t.aid for t in tools]}")

                # Budget check
                if state["snippets_this_month"] >= MAX_SNIPPETS_PER_MONTH:
                    print(f"    SKIP — límite mensual alcanzado ({MAX_SNIPPETS_PER_MONTH})")
                    summary["skipped_budget"] += 1
                    continue

                if dry_run:
                    print(f"    DRY — would call Sonnet")
                    state["processed_ids"].append(issue_id)
                    continue

                snippet = generate_snippet(issue, tools)
                if snippet:
                    state["snippets_this_month"] += 1
                    state["processed_ids"].append(issue_id)
                    summary["snippets"] += 1

                    # ---- PRESENT FOR MANUAL SEND ----
                    print(f"\n{'='*60}")
                    print(f"SNIPPET #{summary['snippets']} — para envío manual")
                    print(f"Repo:  https://github.com/{repo}/issues/{issue['number']}")
                    print(f"Issue: {issue['title']}")
                    print(f"URL:   {issue['html_url']}")
                    print(f"{'─'*60}")
                    print(snippet)
                    print(f"{'='*60}\n")

    if not dry_run:
        # Trim processed_ids to last 500 to avoid unbounded growth
        state["processed_ids"] = state["processed_ids"][-500:]
        _save_state(state)

    print(
        f"[ghost] done — scanned={summary['issues_scanned']} "
        f"relevant={summary['relevant']} snippets={summary['snippets']} "
        f"budget_skipped={summary['skipped_budget']}"
    )
    return summary


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Library Ghost")
    parser.add_argument("--since",   type=int, default=SINCE_HOURS_DEFAULT,
                        help=f"Escanear issues de las últimas N horas (default {SINCE_HOURS_DEFAULT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview sin llamar a Sonnet ni guardar estado")
    parser.add_argument("--reset",   action="store_true",
                        help="Resetea el contador mensual y los IDs procesados")
    parser.add_argument("--repo",    type=str, default=None,
                        help="Escanear solo un repo (ej: langchain-ai/langchain)")
    args = parser.parse_args()

    if args.reset:
        STATE_FILE.unlink(missing_ok=True)
        print("[ghost] estado reseteado.")
        sys.exit(0)

    repos = [args.repo] if args.repo else REPOS
    run(repos=repos, since_hours=args.since, dry_run=args.dry_run)
