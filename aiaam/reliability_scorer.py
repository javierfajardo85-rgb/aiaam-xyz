"""
AIAAM Reliability Scorer — one-shot / cron-friendly
Actualiza reliability_score de todos los tools GitHub usando metadatos
públicos reales (stars + pushed_at) desde la GitHub REST API.

Lógica de scoring:
  stars > 10 000 + pushed < 30 días  →  0.95
  stars >  5 000 + pushed < 90 días  →  0.90
  stars >  1 000 + pushed < 90 días  →  0.85
  pushed > 90 días                   →  0.70  (legacy)
  No se puede leer metadata          →  0.80  (conservador)

No toca tools no-GitHub (HuggingFace, PyPI, npm, etc.).
Registra reliability_calculated_at con la hora UTC del cálculo.

Uso:
    python3 reliability_scorer.py           # actualiza todos
    python3 reliability_scorer.py --dry-run # preview sin escribir
    python3 reliability_scorer.py --aid langchain-v1
"""

import os
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
from sqlmodel import Session, select
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(override=True)

from database import engine, init_db
from models import Tool

# ── Scoring thresholds ────────────────────────────────────────────────────────
NOW = datetime.now(timezone.utc)
_D30 = timedelta(days=30)
_D90 = timedelta(days=90)


def _gh_headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
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


def fetch_github_meta(source_url: str) -> Optional[dict]:
    """Return {'stars': int, 'pushed_at': datetime} or None on error."""
    pair = _owner_repo(source_url)
    if not pair:
        return None
    owner, repo = pair
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=_gh_headers(),
            timeout=15,
            follow_redirects=True,
        )
        if r.status_code == 200:
            data = r.json()
            pushed_raw = data.get("pushed_at") or data.get("updated_at")
            pushed_at = datetime.fromisoformat(pushed_raw.replace("Z", "+00:00")) if pushed_raw else None
            return {
                "stars": data.get("stargazers_count", 0),
                "pushed_at": pushed_at,
                "archived": data.get("archived", False),
                "full_name": data.get("full_name", f"{owner}/{repo}"),
            }
        if r.status_code == 404:
            return {"stars": 0, "pushed_at": None, "archived": False, "full_name": f"{owner}/{repo}"}
    except Exception as exc:
        print(f"      GitHub API error: {exc}")
    return None


def compute_score(meta: Optional[dict]) -> tuple[float, str]:
    """
    Returns (score, reason) based on stars + recency.
    """
    if meta is None:
        return 0.80, "api_error"

    if meta.get("archived"):
        return 0.70, "archived"

    stars = meta.get("stars", 0)
    pushed_at = meta.get("pushed_at")

    if pushed_at is None:
        return 0.75, "no_push_date"

    age = NOW - pushed_at

    if stars > 10_000 and age < _D30:
        return 0.95, f"{stars:,}⭐ pushed {age.days}d ago"
    if stars > 5_000 and age < _D90:
        return 0.90, f"{stars:,}⭐ pushed {age.days}d ago"
    if stars > 1_000 and age < _D90:
        return 0.85, f"{stars:,}⭐ pushed {age.days}d ago"
    if age.days > 90:
        return 0.70, f"legacy — last push {age.days}d ago"

    # Active but small repo
    return 0.80, f"{stars:,}⭐ pushed {age.days}d ago (small)"


# ── PyPI scorer ──────────────────────────────────────────────────────────────

def _pypi_package_name(source_url: str) -> Optional[str]:
    """Extract package name from PyPI URL or install_cmd."""
    # e.g. https://pypi.org/project/requests/
    if "pypi.org/project/" in source_url:
        return source_url.rstrip("/").split("/")[-1].lower()
    return None


def fetch_pypi_meta(tool) -> Optional[dict]:
    """Return {'downloads_month': int, 'version': str} from pypistats.org."""
    # Try source_url first, then parse install_cmd
    pkg = _pypi_package_name(tool.source_url or "")
    if not pkg and tool.install_cmd:
        # e.g. "pip install requests" → "requests"
        parts = (tool.install_cmd or "").split()
        pkg = parts[-1].lower() if len(parts) >= 3 else None
    if not pkg:
        return None
    try:
        r = httpx.get(
            f"https://pypistats.org/api/packages/{pkg}/recent",
            timeout=10,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            return {
                "downloads_month": data.get("last_month", 0),
                "downloads_week":  data.get("last_week", 0),
            }
    except Exception as exc:
        print(f"      PyPI stats error: {exc}")
    return None


def compute_pypi_score(meta: Optional[dict]) -> tuple[float, str]:
    if meta is None:
        return 0.75, "pypi_api_error"
    dm = meta.get("downloads_month", 0)
    if dm >= 10_000_000:
        return 0.97, f"{dm/1_000_000:.1f}M dl/month"
    if dm >= 1_000_000:
        return 0.94, f"{dm/1_000_000:.1f}M dl/month"
    if dm >= 100_000:
        return 0.90, f"{dm/1_000:.0f}k dl/month"
    if dm >= 10_000:
        return 0.85, f"{dm/1_000:.0f}k dl/month"
    if dm >= 1_000:
        return 0.78, f"{dm:,} dl/month"
    return 0.72, f"{dm:,} dl/month (low)"


# ── npm scorer ───────────────────────────────────────────────────────────────

def _npm_package_name(tool) -> Optional[str]:
    if "npmjs.com/package/" in (tool.source_url or ""):
        return tool.source_url.rstrip("/").split("/")[-1]
    if tool.install_cmd:
        parts = (tool.install_cmd or "").split()
        # "npm install express" or "npm i express"
        if len(parts) >= 3 and parts[0] == "npm":
            return parts[-1]
    return None


def fetch_npm_meta(tool) -> Optional[dict]:
    pkg = _npm_package_name(tool)
    if not pkg:
        return None
    try:
        r = httpx.get(
            f"https://api.npmjs.org/downloads/point/last-month/{pkg}",
            timeout=10,
            follow_redirects=True,
        )
        if r.status_code == 200:
            return {"downloads_month": r.json().get("downloads", 0)}
    except Exception as exc:
        print(f"      npm stats error: {exc}")
    return None


# ── HuggingFace scorer ────────────────────────────────────────────────────────

def _hf_model_id(source_url: str) -> Optional[str]:
    # e.g. https://huggingface.co/openai/whisper-large-v3
    if "huggingface.co/" in source_url:
        parts = source_url.rstrip("/").split("huggingface.co/")
        if len(parts) == 2 and "/" in parts[1]:
            return parts[1]
    return None


def fetch_hf_meta(source_url: str) -> Optional[dict]:
    model_id = _hf_model_id(source_url)
    if not model_id:
        return None
    try:
        r = httpx.get(
            f"https://huggingface.co/api/models/{model_id}",
            timeout=10,
            follow_redirects=True,
        )
        if r.status_code == 200:
            data = r.json()
            return {
                "downloads": data.get("downloads", 0),
                "likes":     data.get("likes", 0),
            }
    except Exception as exc:
        print(f"      HF API error: {exc}")
    return None


def compute_hf_score(meta: Optional[dict]) -> tuple[float, str]:
    if meta is None:
        return 0.75, "hf_api_error"
    dl = meta.get("downloads", 0)
    likes = meta.get("likes", 0)
    if dl >= 1_000_000 or likes >= 5_000:
        return 0.95, f"{dl/1_000:.0f}k dl, {likes} likes"
    if dl >= 100_000 or likes >= 500:
        return 0.90, f"{dl/1_000:.0f}k dl, {likes} likes"
    if dl >= 10_000 or likes >= 50:
        return 0.83, f"{dl:,} dl, {likes} likes"
    return 0.74, f"{dl:,} dl, {likes} likes (low)"


# ── Main run ─────────────────────────────────────────────────────────────────

def run(aid: Optional[str] = None, dry_run: bool = False, platform: Optional[str] = None) -> dict:
    init_db()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[scorer] {ts} — reliability scorer{' [DRY-RUN]' if dry_run else ''}")
    if platform:
        print(f"[scorer] platform filter: {platform}")

    results = {"updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}

    with Session(engine) as session:
        if aid:
            tools = [session.get(Tool, aid)] if session.get(Tool, aid) else []
        else:
            stmt = select(Tool)
            if platform:
                stmt = stmt.where(Tool.source_platform == platform)
            tools = session.exec(stmt).all()

        print(f"[scorer] {len(tools)} tools to score\n")

        for tool in tools:
            print(f"  [{tool.source_platform}] {tool.aid}")
            plat = tool.source_platform

            if plat == "github":
                meta = fetch_github_meta(tool.source_url)
                new_score, reason = compute_score(meta)
                time.sleep(0.4)

            elif plat == "pypi":
                meta = fetch_pypi_meta(tool)
                new_score, reason = compute_pypi_score(meta)
                time.sleep(0.3)

            elif plat == "npm":
                meta = fetch_npm_meta(tool)
                new_score, reason = compute_pypi_score(meta)  # same scale
                time.sleep(0.3)

            elif plat == "huggingface":
                meta = fetch_hf_meta(tool.source_url)
                new_score, reason = compute_hf_score(meta)
                time.sleep(0.3)

            else:
                print(f"    skipped (unsupported platform: {plat})")
                results["skipped"] += 1
                continue

            old_score = round(tool.reliability_score, 2)
            changed = abs(new_score - old_score) >= 0.01
            print(f"    {old_score:.2f} → {new_score:.2f}  ({reason})")

            if not dry_run:
                tool.reliability_score = new_score
                tool.reliability_calculated_at = datetime.now(timezone.utc)
                session.add(tool)

            results["updated" if changed else "unchanged"] += 1

        if not dry_run:
            session.commit()

    print(f"\n[scorer] done — updated={results['updated']} unchanged={results['unchanged']} skipped={results['skipped']} errors={results['errors']}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Reliability Scorer")
    parser.add_argument("--aid",      type=str,  help="Score a single tool by aid")
    parser.add_argument("--dry-run",  action="store_true", help="Preview without writing to DB")
    parser.add_argument("--platform", type=str,  choices=["github","pypi","npm","huggingface"],
                        help="Only score tools from this platform")
    args = parser.parse_args()
    run(aid=args.aid, dry_run=args.dry_run, platform=args.platform)
