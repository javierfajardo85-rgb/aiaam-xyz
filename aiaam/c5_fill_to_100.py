"""
C5 Fill to 100 — Pipeline automático para completar el catálogo a 100 verified tools.

Fase 1: Elimina las 17 herramientas pending con install_cmds incorrectos
Fase 2: Traduce 21 herramientas desde PyPI (zero LLM — mapping directo)
Fase 3: Corre sandbox_sanitizer sobre todas las nuevas
Fase 4: Reporta cuántas verified=True nuevas se añadieron

Uso:
    python3 c5_fill_to_100.py           # ejecución completa
    python3 c5_fill_to_100.py --dry-run # preview sin tocar DB ni Docker
"""
import sys
import time
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

from sqlmodel import Session, select
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(override=True)

from database import engine, init_db
from models import Tool
from translator import translate_and_save

# ── Herramientas pending con traducciones incorrectas a eliminar ──────────────
BAD_AIDS = [
    "autogpt-v1", "browser-use-v1", "comfyui-v1", "dify-v1",
    "faiss-v1", "ffmpeg-v1", "flux-v1", "langflow-v1",
    "llama.cpp-v1", "ollama-v1", "qdrant-v1", "selenium-v1",
    "stable-diffusion-webui-v1", "supabase-py-v1",
    "text-generation-webui-v1", "vllm-v1",
    # Failed — re-translate
    "crewai-v1",
]

# ── PyPI URLs a traducir (mapping directo, cero LLM) ─────────────────────────
# 21 candidatos → objetivo: ≥19 pasan sandbox
PYPI_URLS = [
    # Priority gaps
    "https://pypi.org/project/crewai/",
    "https://pypi.org/project/selenium/",
    "https://pypi.org/project/faiss-cpu/",
    "https://pypi.org/project/qdrant-client/",
    "https://pypi.org/project/supabase/",
    "https://pypi.org/project/ollama/",
    "https://pypi.org/project/google-generativeai/",
    "https://pypi.org/project/pyarrow/",
    "https://pypi.org/project/ffmpeg-python/",
    # High-value additions
    "https://pypi.org/project/click/",
    "https://pypi.org/project/tqdm/",
    "https://pypi.org/project/loguru/",
    "https://pypi.org/project/tenacity/",
    "https://pypi.org/project/PyYAML/",
    "https://pypi.org/project/alembic/",
    "https://pypi.org/project/aiofiles/",
    "https://pypi.org/project/Jinja2/",
    "https://pypi.org/project/browser-use/",
    "https://pypi.org/project/vllm/",
    # Buffer (heavy — might fail Docker, but worth trying)
    "https://pypi.org/project/langflow/",
    "https://pypi.org/project/sentence-transformers/",
]


def phase1_delete_bad(session: Session, dry_run: bool) -> list[str]:
    """Elimina herramientas con traducciones incorrectas."""
    deleted = []
    for aid in BAD_AIDS:
        tool = session.get(Tool, aid)
        if tool:
            if not dry_run:
                session.delete(tool)
            deleted.append(aid)
            print(f"  [delete] {aid} (verified={tool.verified})")
    if not dry_run:
        session.commit()
    return deleted


def phase2_translate(session: Session, dry_run: bool) -> list[str]:
    """Traduce todas las URLs PyPI y guarda en DB."""
    translated = []
    for url in PYPI_URLS:
        pkg = url.rstrip("/").split("/")[-1]
        print(f"  [translate] {pkg} ... ", end="", flush=True)
        if dry_run:
            print("(dry-run skip)")
            translated.append(f"pypi-{pkg.lower()}-v1")
            continue
        try:
            tool = translate_and_save(url, session)
            if tool:
                print(f"OK → {tool.aid} | install: {tool.install_cmd}")
                translated.append(tool.aid)
            else:
                print("FAILED (translate returned None)")
        except Exception as exc:
            print(f"ERROR: {exc}")
        time.sleep(0.3)  # rate limit buffer
    return translated


def phase3_sanitize(new_aids: list[str], dry_run: bool) -> dict:
    """Corre sandbox_sanitizer sobre los aids recién traducidos."""
    results = {"verified": [], "failed": [], "errors": []}
    for aid in new_aids:
        print(f"  [sanitize] {aid} ... ", end="", flush=True)
        if dry_run:
            print("(dry-run skip)")
            continue
        try:
            proc = subprocess.run(
                [sys.executable, "sandbox_sanitizer.py", "--aid", aid],
                capture_output=True, text=True, timeout=300
            )
            output = proc.stdout + proc.stderr
            if "verified=True" in output or "✓" in output or "PASS" in output.upper():
                results["verified"].append(aid)
                print("PASS ✓")
            elif "verified=False" in output or "FAIL" in output.upper():
                results["failed"].append(aid)
                print("FAIL ✗")
            else:
                results["errors"].append(aid)
                print(f"UNKNOWN (rc={proc.returncode})")
                if proc.stderr:
                    print(f"    stderr: {proc.stderr[-200:]}")
        except subprocess.TimeoutExpired:
            results["errors"].append(aid)
            print("TIMEOUT")
        except Exception as exc:
            results["errors"].append(aid)
            print(f"ERROR: {exc}")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dry_run = args.dry_run
    init_db()

    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n{'='*60}")
    print(f"C5 Fill to 100 — {ts}{' [DRY-RUN]' if dry_run else ''}")
    print(f"{'='*60}\n")

    with Session(engine) as session:
        # Estado inicial
        verified_before = session.exec(
            select(Tool).where(Tool.verified == True)
        ).all()
        print(f"[state] verified before: {len(verified_before)}\n")

        # Fase 1 — eliminar bad pending
        print("── PHASE 1: Delete bad pending tools ──")
        deleted = phase1_delete_bad(session, dry_run)
        print(f"  → {len(deleted)} deleted\n")

        # Fase 2 — traducir desde PyPI
        print("── PHASE 2: Translate from PyPI (zero LLM) ──")
        new_aids = phase2_translate(session, dry_run)
        print(f"  → {len(new_aids)} translated\n")

    # Fase 3 — sanitizer (usa su propia sesión)
    print("── PHASE 3: Sandbox sanitizer ──")
    san_results = phase3_sanitize(new_aids, dry_run)
    print(f"  → verified: {len(san_results['verified'])} | failed: {len(san_results['failed'])} | errors: {len(san_results['errors'])}\n")

    # Estado final
    with Session(engine) as session:
        verified_after = len(session.exec(
            select(Tool).where(Tool.verified == True)
        ).all())
        total = len(session.exec(select(Tool)).all())

    print(f"{'='*60}")
    print(f"RESULT: {verified_after} verified / {total} total")
    print(f"  Added: {verified_after - len(verified_before)} new verified tools")
    if verified_after >= 100:
        print(f"  ✓ TARGET 100 REACHED")
    else:
        print(f"  Still need: {100 - verified_after} more to reach 100")
    print(f"{'='*60}\n")

    if not dry_run and san_results["verified"]:
        print("── PHASE 4: Push verified to Railway ──")
        try:
            proc = subprocess.run(
                [sys.executable, "push_to_production.py", "--only-verified"],
                capture_output=True, text=True, timeout=600
            )
            print(proc.stdout[-500:] if proc.stdout else "(no output)")
        except Exception as exc:
            print(f"Push failed: {exc}")


if __name__ == "__main__":
    main()
