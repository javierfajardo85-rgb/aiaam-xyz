"""
AIAAM Sentinel Sniffer — Agente B1
Detecta repos GitHub con alto crecimiento de estrellas y los añade al catálogo.

Coste LLM: CERO en la detección (GitHub API pura).
Si foam_score >= FOAM_THRESHOLD → llama a translator con priority_high=True (Sonnet).

Pilares FOAM evaluados (0-6):
  1. Velocidad de estrellas: >500 en <24h  OR  >2000 en <7 días
  2. Topics: al menos 1 de {llm, agents, automation, gpt, tools, api-wrapper}
  3. Código ejecutable: directorio src/ o lib/ existe
  4. Actividad: >5 Pull Requests abiertas
  5. Novedad en catálogo: no existe ya en DB
  6. Edad: menos de 30 días de vida

Uso:
    python3 sentinel_sniffer.py             # single run
    python3 sentinel_sniffer.py --dry-run   # preview sin traducir
    python3 sentinel_sniffer.py --loop      # bucle cada 4h
"""

import os
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from sqlmodel import Session, select
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool
from translator import translate_and_save

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
FOAM_THRESHOLD = 4        # foam_score mínimo para disparar traducción
LOOP_INTERVAL_H = 4       # horas entre ejecuciones en modo --loop
FOAM_TOPICS = {"llm", "agents", "automation", "gpt", "tools", "api-wrapper"}


# =====================================================================
# GITHUB API HELPER
# =====================================================================

def _gh(url: str, params: dict = None) -> dict:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    resp = httpx.get(url, headers=headers, params=params or {}, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


# =====================================================================
# 6 PILARES FOAM — lógica Python pura, sin LLM
# =====================================================================

def _pillar_1_star_velocity(repo: dict) -> bool:
    """>500 estrellas en <24h  OR  >2000 en <7 días."""
    stars = repo.get("stargazers_count", 0)
    created_at = datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - created_at
    if age.total_seconds() < 86_400 and stars > 500:
        return True
    if age.days < 7 and stars > 2000:
        return True
    return False


def _pillar_2_topics(repo: dict) -> bool:
    """Al menos 1 topic de la lista FOAM."""
    return bool(set(repo.get("topics", [])) & FOAM_TOPICS)


def _pillar_3_executable_code(owner: str, name: str) -> bool:
    """Existe directorio src/ o lib/ en la raíz."""
    try:
        contents = _gh(f"https://api.github.com/repos/{owner}/{name}/contents/")
        dirs = {item["name"] for item in contents if item["type"] == "dir"}
        return bool({"src", "lib"} & dirs)
    except Exception:
        return False


def _pillar_4_open_prs(owner: str, name: str) -> bool:
    """>5 Pull Requests abiertas."""
    try:
        data = _gh(
            "https://api.github.com/search/issues",
            params={"q": f"repo:{owner}/{name} type:pr state:open", "per_page": 1},
        )
        return data.get("total_count", 0) > 5
    except Exception:
        return False


def _pillar_5_not_in_catalog(session: Session, aid: str) -> bool:
    """No existe ya en el catálogo (check DB)."""
    return session.get(Tool, aid) is None


def _pillar_6_age(repo: dict) -> bool:
    """Menos de 30 días de vida."""
    created_at = datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - created_at
    return age.days < 30


def _derive_aid(repo: dict) -> str:
    return repo["name"].lower().replace(".", "-") + "-v1"


# =====================================================================
# EVALUADOR FOAM
# =====================================================================

def evaluate_foam(repo: dict, session: Session, verbose: bool = True) -> int:
    """Evalúa los 6 pilares. Devuelve foam_score (0-6). Sin LLM."""
    owner = repo["owner"]["login"]
    name  = repo["name"]
    aid   = _derive_aid(repo)

    checks = [
        ("star_velocity",    _pillar_1_star_velocity(repo)),
        ("topics",           _pillar_2_topics(repo)),
        ("executable_code",  _pillar_3_executable_code(owner, name)),
        ("open_prs",         _pillar_4_open_prs(owner, name)),
        ("not_in_catalog",   _pillar_5_not_in_catalog(session, aid)),
        ("age_<30d",         _pillar_6_age(repo)),
    ]

    if verbose:
        for i, (label, passed) in enumerate(checks, 1):
            mark = "✓" if passed else "✗"
            print(f"      Pillar {i} ({label}): {mark}")

    return sum(passed for _, passed in checks)


# =====================================================================
# FETCH CANDIDATOS
# =====================================================================

def fetch_candidate_repos() -> list:
    """GitHub search: repos creados en últimas 48h con >500 estrellas."""
    since = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%d")
    data = _gh(
        "https://api.github.com/search/repositories",
        params={
            "q": f"created:>{since} stars:>500",
            "sort": "stars",
            "order": "desc",
            "per_page": 30,
        },
    )
    return data.get("items", [])


# =====================================================================
# MAIN RUN
# =====================================================================

def run(dry_run: bool = False) -> list[dict]:
    """Single pass del sniffer. Devuelve lista de repos procesados."""
    init_db()
    results = []
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[sentinel] {ts} — scanning GitHub for FOAM candidates...")

    try:
        repos = fetch_candidate_repos()
    except Exception as exc:
        print(f"[sentinel] ERROR fetching repos: {exc}")
        return []

    print(f"[sentinel] {len(repos)} candidatos encontrados\n")

    with Session(engine) as session:
        for repo in repos:
            url   = repo["html_url"]
            stars = repo.get("stargazers_count", 0)
            print(f"  → {url}  ({stars}★)")

            foam_score = evaluate_foam(repo, session)
            print(f"      foam_score: {foam_score}/6")

            if foam_score < FOAM_THRESHOLD:
                print(f"      SKIP (score < {FOAM_THRESHOLD})\n")
                continue

            if dry_run:
                print(f"      DRY-RUN — would translate (priority_high=True)\n")
                results.append({"url": url, "foam_score": foam_score, "action": "would_translate"})
                continue

            print(f"      TRANSLATING con Sonnet (priority_high)...")
            try:
                tool = translate_and_save(url, session, priority_high=True)
            except Exception as exc:
                print(f"      ERROR: {exc}\n")
                results.append({"url": url, "foam_score": foam_score, "action": "error", "detail": str(exc)})
                continue

            if tool:
                tool.foam_score = foam_score
                session.add(tool)
                session.commit()
                print(f"      OK → aid={tool.aid}  translator={tool.translator_used}\n")
                results.append({"url": url, "foam_score": foam_score, "aid": tool.aid})
            else:
                print(f"      FAIL — translation returned None\n")
                results.append({"url": url, "foam_score": foam_score, "action": "failed"})

            time.sleep(2)  # courtesy pause entre traducciones

    translated = sum(1 for r in results if "aid" in r)
    print(f"[sentinel] done — {translated} repos añadidos al catálogo.")
    return results


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Sentinel Sniffer")
    parser.add_argument("--dry-run", action="store_true", help="Preview sin traducir ni escribir en DB")
    parser.add_argument("--loop",    action="store_true", help=f"Ejecuta en bucle cada {LOOP_INTERVAL_H}h")
    args = parser.parse_args()

    if args.loop:
        print(f"[sentinel] modo loop — ejecutando cada {LOOP_INTERVAL_H}h")
        while True:
            run(dry_run=args.dry_run)
            print(f"[sentinel] próxima ejecución en {LOOP_INTERVAL_H}h\n")
            time.sleep(LOOP_INTERVAL_H * 3600)
    else:
        run(dry_run=args.dry_run)
