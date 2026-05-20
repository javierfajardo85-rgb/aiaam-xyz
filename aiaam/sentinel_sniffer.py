"""
AIAAM Sentinel Sniffer — Agente B1
Detecta repos GitHub con alto crecimiento de estrellas y los añade al catálogo.
También descubre specs OpenAPI públicas desde APIs.guru.

Coste LLM: CERO en modo --mode github (GitHub API pura).
           Haiku en modo --mode openapi (max 10 compilaciones/día, hard limit).

Pilares FOAM evaluados (0-6):
  1. Velocidad de estrellas: >500 en <24h  OR  >2000 en <7 días
  2. Topics: al menos 1 de {llm, agents, automation, gpt, tools, api-wrapper}
  3. Código ejecutable: directorio src/ o lib/ existe
  4. Actividad: >5 Pull Requests abiertas
  5. Novedad en catálogo: no existe ya en DB
  6. Edad: menos de 30 días de vida

Uso:
    python3 sentinel_sniffer.py --mode github             # default (GitHub trending)
    python3 sentinel_sniffer.py --mode github --dry-run   # preview sin traducir
    python3 sentinel_sniffer.py --mode github --loop      # bucle cada 4h
    python3 sentinel_sniffer.py --mode openapi --dry-run  # preview 20 candidatos
    python3 sentinel_sniffer.py --mode openapi --limit 3  # compila máx 3
"""

import os
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from sqlmodel import Session, select, func
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool
from translator import translate_and_save
from analytics import check_monetization_ratio, log_agent_run

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
    _t0 = time.time()
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
                check_monetization_ratio(session)
            else:
                print(f"      FAIL — translation returned None\n")
                results.append({"url": url, "foam_score": foam_score, "action": "failed"})

            time.sleep(2)  # courtesy pause entre traducciones

    translated = sum(1 for r in results if "aid" in r)
    failed     = sum(1 for r in results if r.get("action") in ("error", "failed"))
    print(f"[sentinel] done — {translated} repos añadidos al catálogo.")
    if not dry_run:
        log_agent_run(
            agent_code="B1", agent_name="Sentinel",
            items_processed=len(results),
            items_new=translated, items_failed=failed,
            duration_s=int(time.time() - _t0),
            summary=f"foam_candidates={len(results)} translated={translated}",
        )
    return results


# =====================================================================
# OPENAPI DISCOVERY — APIs.guru → CompiledAPIs (--mode openapi)
# =====================================================================

APIS_GURU_URL          = "https://api.apis.guru/v2/list.json"
TARGET_CATEGORIES      = {"financial", "analytics", "developer_tools", "ecommerce", "location", "payment", "transport", "security", "machine_learning", "search"}
MAX_CANDIDATES         = 20
MAX_DAILY_COMPILATIONS = 10          # hard cost limit — NEVER exceed
MAX_SPEC_BYTES         = 500_000     # 500KB — truncation would destroy quality
MONTHS_RECENCY_DAYS    = 48 * 30    # APIs.guru tops out at Apr 2023 — 48 months captures all 2022-2023 entries


def _count_today_compilations(session: Session) -> int:
    """Count CompiledAPIs records inserted today (UTC). SQLite + PostgreSQL safe."""
    from models import CompiledAPI
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return session.exec(
        select(func.count(CompiledAPI.id)).where(CompiledAPI.compiled_at >= today_start)
    ).one() or 0


def _already_compiled(session: Session, swagger_url: str) -> bool:
    """True if this spec URL is already in CompiledAPIs."""
    from models import CompiledAPI
    return session.exec(
        select(CompiledAPI).where(CompiledAPI.source_url == swagger_url)
    ).first() is not None


def _check_spec_url(swagger_url: str) -> tuple[bool, int]:
    """
    HEAD request to verify spec is accessible and not too large.
    Returns (accessible, size_bytes). size=0 if content-length absent.
    """
    try:
        r = httpx.head(swagger_url, follow_redirects=True, timeout=10)
        if r.status_code != 200:
            return False, 0
        size = int(r.headers.get("content-length", 0))
        return True, size
    except Exception:
        return False, 0


def fetch_openapi_candidates(session: Session) -> tuple[int, list[dict]]:
    """
    Pull APIs.guru catalog, apply all quality filters, return
    (total_in_catalog, filtered_candidates_up_to_MAX_CANDIDATES).
    Zero LLM calls.
    """
    print(f"[sentinel] fetching APIs.guru catalog...", end=" ", flush=True)
    try:
        r = httpx.get(APIS_GURU_URL, timeout=30)
        r.raise_for_status()
        catalog = r.json()
    except Exception as exc:
        print(f"FAIL — {exc}")
        return 0, []
    total_in_catalog = len(catalog)
    print(f"{total_in_catalog} APIs found")

    cutoff = datetime.utcnow() - timedelta(days=MONTHS_RECENCY_DAYS)
    candidates: list[dict] = []
    skipped_category = skipped_deprecated = skipped_old = skipped_compiled = 0

    for api_key, api_data in catalog.items():
        versions   = api_data.get("versions", {})
        preferred  = api_data.get("preferred", "")
        ver_data   = versions.get(preferred) or (next(iter(versions.values())) if versions else {})
        if not ver_data:
            continue

        info       = ver_data.get("info", {})
        provider   = info.get("x-providerName", api_key).lower()

        # EXCLUDE deprecated
        if "deprecated" in provider:
            skipped_deprecated += 1
            continue

        # INCLUDE only target categories (APIs.guru stores them in x-apisguru-categories)
        cats = info.get("x-apisguru-categories", [])
        category_match = next((c.lower() for c in cats if c.lower() in TARGET_CATEGORIES), None)
        if not category_match:
            skipped_category += 1
            continue

        # INCLUDE only APIs updated in last 18 months
        updated_str = ver_data.get("updated", "")
        if updated_str:
            try:
                updated_dt = datetime.fromisoformat(
                    updated_str.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                if updated_dt < cutoff:
                    skipped_old += 1
                    continue
            except ValueError:
                pass

        swagger_url = ver_data.get("swaggerUrl", "")
        if not swagger_url:
            continue

        # EXCLUDE already compiled
        if _already_compiled(session, swagger_url):
            skipped_compiled += 1
            continue

        candidates.append({
            "api_key":      api_key,
            "provider":     provider,
            "swagger_url":  swagger_url,
            "category":     category_match,
            "updated":      updated_str[:10],
        })

        # Over-collect then trim after accessibility check
        if len(candidates) >= MAX_CANDIDATES * 4:
            break

    print(
        f"[sentinel] pre-filter: {len(candidates)} pass "
        f"(skipped: {skipped_category} no-category, {skipped_deprecated} deprecated, "
        f"{skipped_old} stale, {skipped_compiled} already-compiled)"
    )
    print(f"[sentinel] checking accessibility (HEAD) for up to {MAX_CANDIDATES * 4} candidates...")

    valid: list[dict] = []
    for c in candidates:
        accessible, size = _check_spec_url(c["swagger_url"])
        if not accessible:
            continue
        if size > MAX_SPEC_BYTES:
            print(f"    SKIP {c['api_key']} — {size // 1024}KB > 500KB limit")
            continue
        valid.append(c)
        if len(valid) >= MAX_CANDIDATES:
            break
        time.sleep(0.15)  # courtesy between HEAD requests

    return total_in_catalog, valid


def run_openapi(dry_run: bool = False, limit: int = MAX_DAILY_COMPILATIONS) -> dict:
    """
    OpenAPI discovery mode:
      Step 1 — discover & filter from APIs.guru (max 20 candidates)
      Step 2 — compile via Haiku (respects 10/day hard limit)
      Step 3 — print summary report
    """
    init_db()
    _t0 = time.time()
    ts  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[sentinel] {ts} — mode=openapi{' [DRY-RUN]' if dry_run else ''}")

    results = {
        "total_in_catalog": 0, "passed_filter": 0,
        "compiled": 0, "failed": 0,
        "daily_used": 0, "daily_remaining": MAX_DAILY_COMPILATIONS,
    }

    with Session(engine) as session:
        # ── Pre-check daily budget ──────────────────────────────────
        today_count      = _count_today_compilations(session)
        daily_remaining  = MAX_DAILY_COMPILATIONS - today_count
        results["daily_used"]      = today_count
        results["daily_remaining"] = daily_remaining

        if daily_remaining <= 0:
            print(
                f"[sentinel] Daily limit reached "
                f"({MAX_DAILY_COMPILATIONS}/{MAX_DAILY_COMPILATIONS}). "
                f"Skipping. Next window: tomorrow."
            )
            return results

        compile_budget = min(limit, daily_remaining)

        # ── Step 1: Discover ─────────────────────────────────────────
        total_in_catalog, candidates = fetch_openapi_candidates(session)
        results["total_in_catalog"] = total_in_catalog
        results["passed_filter"]    = len(candidates)

        print(f"\n[sentinel] {len(candidates)} candidates passed all filters\n")

        if not candidates:
            print("[sentinel] Nothing to compile.")
            return results

        # ── Dry-run: show candidates and stop ─────────────────────────
        if dry_run:
            print(f"{'─'*70}")
            print(f"  {'#':<4} {'API Key':<38} {'Cat':<12} {'Updated':<12}")
            print(f"{'─'*70}")
            for i, c in enumerate(candidates, 1):
                print(f"  {i:<4} {c['api_key']:<38} {c['category']:<12} {c['updated']:<12}")
                print(f"       {c['swagger_url'][:65]}")
            print(f"{'─'*70}")
            print(f"  Total candidates: {len(candidates)} | Daily budget: {daily_remaining}/{MAX_DAILY_COMPILATIONS}")
            return results

        # ── Step 2: Compile (within budget) ──────────────────────────
        from compiler.openapi_compiler import compile_from_url, save_to_db

        print(f"[sentinel] compiling up to {compile_budget} specs (budget: {daily_remaining} remaining today)\n")

        for c in candidates[:compile_budget]:
            print(f"  → {c['api_key']}  ({c['category']})")
            print(f"    {c['swagger_url'][:70]}")
            try:
                result = compile_from_url(c["swagger_url"])
                save_to_db(result, category=c["category"])
                results["compiled"] += 1
                print(f"    OK — {result['tokens_used']} tokens · truncated={result['was_truncated']}")
            except Exception as exc:
                results["failed"] += 1
                print(f"    FAIL — {exc}")
            print()
            time.sleep(3)  # rate-limit protection between compilations

        results["daily_remaining"] = MAX_DAILY_COMPILATIONS - today_count - results["compiled"]

    # ── Step 3: Summary ──────────────────────────────────────────────
    elapsed = int(time.time() - _t0)
    print(f"{'─'*55}")
    print(f"  OpenAPI Discovery Summary")
    print(f"{'─'*55}")
    print(f"  Specs in APIs.guru catalog : {results['total_in_catalog']}")
    print(f"  Passed quality filters     : {results['passed_filter']}")
    print(f"  Compiled successfully      : {results['compiled']}")
    print(f"  Failed                     : {results['failed']}")
    print(f"  Daily budget remaining     : {results['daily_remaining']}/{MAX_DAILY_COMPILATIONS}")
    print(f"  Review pending at          : GET /admin/compiled-apis?verified=false")
    print(f"{'─'*55}")

    if not dry_run:
        log_agent_run(
            agent_code="B1", agent_name="Sentinel",
            items_processed=results["passed_filter"],
            items_new=results["compiled"], items_failed=results["failed"],
            duration_s=elapsed,
            summary=f"openapi: discovered={results['passed_filter']} compiled={results['compiled']}",
        )
    return results


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Sentinel Sniffer")
    parser.add_argument(
        "--mode", choices=["github", "openapi"], default="github",
        help="github = GitHub trending repos (default) | openapi = APIs.guru discovery",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing to DB or calling LLMs")
    parser.add_argument("--loop",    action="store_true",
                        help=f"(github mode only) Run in loop every {LOOP_INTERVAL_H}h")
    parser.add_argument("--limit",   type=int, default=MAX_DAILY_COMPILATIONS,
                        help="(openapi mode) Max specs to compile this run (default: 10)")
    args = parser.parse_args()

    if args.mode == "openapi":
        run_openapi(dry_run=args.dry_run, limit=args.limit)
    elif args.loop:
        print(f"[sentinel] modo loop — ejecutando cada {LOOP_INTERVAL_H}h")
        while True:
            run(dry_run=args.dry_run)
            print(f"[sentinel] próxima ejecución en {LOOP_INTERVAL_H}h\n")
            time.sleep(LOOP_INTERVAL_H * 3600)
    else:
        run(dry_run=args.dry_run)
