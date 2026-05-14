"""
AIAAM Context Injector — Agente B3
Genera AGENT_INSTRUCTIONS.md para cada tool del catálogo con licencia
MIT o Apache 2.0. Plantilla fija — cero llamadas LLM.

Tabla nueva: InjectedRepo (repo_url, aid, license_spdx, instructions_md, injected_at)
Nunca inyecta dos veces el mismo repo (check DB por aid).

Uso:
    python3 context_injector.py                # inyecta todos los pendientes
    python3 context_injector.py --aid yt-dlp-v1  # inyecta uno específico
    python3 context_injector.py --dry-run      # preview sin escribir en DB
    python3 context_injector.py --force        # re-inyecta aunque ya exista
"""

import os
import sys
import time
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

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ALLOWED_LICENSES = {"MIT", "Apache-2.0"}

TEMPLATE_PATH = Path(__file__).parent / "AGENT_INSTRUCTIONS.md.template"


# =====================================================================
# GITHUB LICENSE CHECK — sin LLM, solo API
# =====================================================================

def _gh(url: str) -> Optional[dict]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    try:
        resp = httpx.get(url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def fetch_license(source_url: str) -> Optional[str]:
    """
    Devuelve el SPDX identifier de la licencia del repo.
    Ejemplos: "MIT", "Apache-2.0", "GPL-3.0", None si no se puede determinar.

    Para repos no-GitHub (PyPI, npm, HuggingFace) retorna None → se omiten.
    """
    if "github.com" not in source_url:
        return None

    parts = source_url.rstrip("/").split("/")
    if len(parts) < 5:
        return None
    owner, repo = parts[-2], parts[-1]

    data = _gh(f"https://api.github.com/repos/{owner}/{repo}/license")
    if not data:
        return None
    return data.get("license", {}).get("spdx_id")  # "MIT", "Apache-2.0", "NOASSERTION", ...


# =====================================================================
# TEMPLATE RENDERER — sustitución de variables, sin LLM
# =====================================================================

def _load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def render_instructions(tool: Tool, license_spdx: str) -> str:
    """Rellena la plantilla con los datos del tool. Sin LLM."""
    template = _load_template()
    return (
        template
        .replace("{tool_name}",       tool.aid.replace("-v1", "").replace("-", " ").title())
        .replace("{aid}",             tool.aid)
        .replace("{source_url}",      tool.source_url)
        .replace("{license_spdx}",    license_spdx)
        .replace("{install_cmd}",     tool.install_cmd or "N/A")
        .replace("{execute_cmd}",     tool.execute_cmd or "N/A")
        .replace("{input_type}",      tool.input_schema.get("type", "unknown") if tool.input_schema else "unknown")
        .replace("{output_type}",     tool.output_schema.get("type", "unknown") if tool.output_schema else "unknown")
        .replace("{reliability_score}", str(round(tool.reliability_score, 2)))
        .replace("{latency_ms}",      str(tool.latency_ms) if tool.latency_ms else "unknown")
        .replace("{injected_at}",     datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
    )


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
    Genera y persiste las instrucciones para un tool.
    Devuelve el InjectedRepo creado, o None si se omite.
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

    instructions = render_instructions(tool, license_spdx)
    print(f"    OK   — licencia {license_spdx} · {len(instructions)} chars")

    if dry_run:
        print(f"    DRY  — no se escribe en DB")
        return None

    record = InjectedRepo(
        repo_url=tool.source_url,
        aid=tool.aid,
        license_spdx=license_spdx,
        instructions_md=instructions,
        injected_at=datetime.utcnow(),
    )
    if existing:
        # force=True: actualiza el registro existente
        existing.license_spdx   = license_spdx
        existing.instructions_md = instructions
        existing.injected_at     = datetime.utcnow()
        session.add(existing)
        session.commit()
        return existing

    session.add(record)
    session.commit()
    return record


# =====================================================================
# MAIN RUN
# =====================================================================

def run(
    aid: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """
    Inyecta tools pendientes (o el especificado por aid).
    Devuelve contadores.
    """
    init_db()
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[injector] {ts} — generating AGENT_INSTRUCTIONS...")

    results = {"injected": 0, "skipped": 0, "no_license": 0}

    with Session(engine) as session:
        if aid:
            tool = session.get(Tool, aid)
            if not tool:
                print(f"[injector] ERROR: aid '{aid}' not found")
                return results
            tools = [tool]
        else:
            # Solo tools con source_url de GitHub (los únicos con licencia verificable)
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
    return results


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Context Injector")
    parser.add_argument("--aid",     type=str,  help="Inyectar un tool específico por aid")
    parser.add_argument("--dry-run", action="store_true", help="Preview sin escribir en DB")
    parser.add_argument("--force",   action="store_true", help="Re-inyecta aunque ya exista")
    args = parser.parse_args()

    run(aid=args.aid, dry_run=args.dry_run, force=args.force)
