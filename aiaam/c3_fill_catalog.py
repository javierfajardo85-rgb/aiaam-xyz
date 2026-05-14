"""
C3 — Completar el catálogo hasta cubrir las 10 categorías prioritarias.

Pasos en orden:
  0. Limpieza: resetea verified=False corruptos, arregla install_cmds con comillas
  1. Traduce los 20 tools que faltan con Haiku (PyPI/GitHub)
  2. Inyecta asyncio manualmente (built-in, sin install)
  3. Corre sandbox_sanitizer sobre todos los verified=None

Coste estimado: ~$0.04 Haiku
"""

import sys
import time
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool
from translator import translate_and_save
from sandbox_sanitizer import run as sanitizer_run

# ── Tools a traducir (URL fuente → aid esperado) ─────────────────────
TO_TRANSLATE = [
    # CAT 1
    ("https://github.com/deepset-ai/haystack",              "haystack-v1"),
    # CAT 2
    ("https://github.com/SeleniumHQ/selenium",              "selenium-v1"),
    # CAT 4
    ("https://github.com/pinecone-io/pinecone-python-client", "pinecone-v1"),
    # CAT 5
    ("https://github.com/pola-rs/polars",                   "polars-v1"),
    ("https://github.com/apache/arrow",                     "pyarrow-v1"),
    # CAT 6
    ("https://github.com/langfuse/langfuse-python",         "langfuse-v1"),
    ("https://github.com/langchain-ai/langsmith-sdk",       "langsmith-v1"),
    # CAT 7
    ("https://github.com/huggingface/huggingface_hub",      "huggingface-hub-v1"),
    ("https://github.com/google-gemini/generative-ai-python", "google-generativeai-v1"),
    # CAT 8
    ("https://github.com/docker/docker-py",                 "docker-py-v1"),
    ("https://github.com/celery/celery",                    "celery-v1"),
    ("https://github.com/agronholm/apscheduler",            "apscheduler-v1"),
    # CAT 9
    ("https://github.com/sqlalchemy/sqlalchemy",            "sqlalchemy-v1"),
    ("https://github.com/redis/redis-py",                   "redis-v1"),
    ("https://github.com/mongodb/mongo-python-driver",      "pymongo-v1"),
    ("https://github.com/supabase/supabase-py",             "supabase-v1"),
    # CAT 10
    ("https://github.com/encode/uvicorn",                   "uvicorn-v1"),
    ("https://github.com/theskumar/python-dotenv",          "python-dotenv-v1"),
    ("https://github.com/pydantic/pydantic-settings",       "pydantic-settings-v1"),
]

# asyncio se inyecta a mano (built-in Python, sin install)
ASYNCIO_MAI1 = Tool(
    aid="asyncio-v1",
    version="3.11+",
    input_schema={"type": "coroutine", "format": ["async def", "await"]},
    output_schema={"type": "any", "format": ["awaitable", "future"]},
    reliability_score=0.99,
    latency_ms=0,
    source_url="https://docs.python.org/3/library/asyncio.html",
    install_cmd=None,
    execute_cmd="import asyncio; asyncio.run(main())",
    source_platform="pypi",
    translator_used="mapped",
    verified=True,
    last_verified_at=datetime.utcnow(),
    health_score=1.0,
)


def step0_cleanup(session: Session) -> None:
    """
    Limpieza previa:
    - Resetea verified=False que vienen del run con --no-new-privileges roto
      (solo whisper-v1; el resto ya fueron reseteados antes)
    - Arregla install_cmds con comillas que bloquean el allowlist
    """
    print("\n[C3] STEP 0 — limpieza")

    # Resetear whisper-v1 (False incorrecto)
    whisper = session.get(Tool, "whisper-v1")
    if whisper and whisper.verified is False:
        whisper.verified = None
        whisper.updated_at = datetime.utcnow()
        session.add(whisper)
        print("  whisper-v1: verified=False → None (reset)")

    # autogen: quitar comillas y extras → pip install autogen-agentchat
    autogen = session.get(Tool, "autogen-v1")
    if autogen and '"' in (autogen.install_cmd or ""):
        autogen.install_cmd = "pip install autogen-agentchat"
        autogen.updated_at = datetime.utcnow()
        session.add(autogen)
        print("  autogen-v1: install_cmd simplificado (sin comillas)")

    # smolagents: quitar comillas → pip install smolagents
    smol = session.get(Tool, "smolagents-v1")
    if smol and '"' in (smol.install_cmd or ""):
        smol.install_cmd = "pip install smolagents"
        smol.updated_at = datetime.utcnow()
        session.add(smol)
        print("  smolagents-v1: install_cmd simplificado (sin comillas)")

    session.commit()


def step1_translate(session: Session) -> dict:
    """Traduce los 20 tools que faltan. Haiku por defecto."""
    print("\n[C3] STEP 1 — traducción de 20 tools")
    results = {"ok": 0, "skip": 0, "error": 0}

    for url, expected_aid in TO_TRANSLATE:
        # Skip si ya existe
        existing = session.get(Tool, expected_aid)
        if existing:
            print(f"  SKIP {expected_aid} — ya en catálogo (verified={existing.verified})")
            results["skip"] += 1
            continue

        print(f"  → {expected_aid}  [{url}]")
        try:
            tool = translate_and_save(url, session)
            if tool:
                print(f"    OK  aid={tool.aid}  translator={tool.translator_used}")
                results["ok"] += 1
            else:
                print(f"    FAIL — translate_and_save returned None")
                results["error"] += 1
        except Exception as exc:
            print(f"    ERROR — {exc}")
            results["error"] += 1

        time.sleep(1)  # cortesía con GitHub API

    return results


def step2_inject_asyncio(session: Session) -> None:
    """Inyecta asyncio manualmente (built-in, sin install)."""
    print("\n[C3] STEP 2 — inyección manual asyncio-v1")
    existing = session.get(Tool, "asyncio-v1")
    if existing:
        print("  SKIP — asyncio-v1 ya existe")
        return
    session.merge(ASYNCIO_MAI1)
    session.commit()
    print("  OK — asyncio-v1 inyectado (built-in, verified=True, score=0.99)")


def step3_sanitize() -> dict:
    """Corre triple validación sobre todos los verified=None."""
    print("\n[C3] STEP 3 — sandbox sanitizer sobre verified=None")
    return sanitizer_run(reverify_all=False, dry_run=False)


def run():
    init_db()
    print("=" * 60)
    print("C3 — COMPLETAR CATÁLOGO HASTA 10 CATEGORÍAS PRIORITARIAS")
    print("=" * 60)

    with Session(engine) as session:
        step0_cleanup(session)
        t1 = step1_translate(session)
        step2_inject_asyncio(session)

    print(f"\n[C3] traducción completa — ok={t1['ok']} skip={t1['skip']} error={t1['error']}")

    t3 = step3_sanitize()

    # Reporte final
    with Session(engine) as session:
        all_tools = session.exec(select(Tool)).all()
        verified  = [t for t in all_tools if t.verified is True]
        print(f"\n{'='*60}")
        print(f"RESULTADO FINAL")
        print(f"  Tools en catálogo:  {len(all_tools)}")
        print(f"  verified=True:      {len(verified)}")
        print(f"  sandbox passed:     {t3['passed']}")
        print(f"  sandbox failed:     {t3['failed']}")
        print(f"  sandbox skip/unver: {t3['unverifiable']}")
        print(f"{'='*60}")


if __name__ == "__main__":
    run()
