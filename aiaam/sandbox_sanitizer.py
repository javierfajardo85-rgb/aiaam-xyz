"""
AIAAM Sandbox Sanitizer — Agente B2
Verifica install_cmd de cada herramienta ejecutándola en Docker aislado.

Coste LLM: CERO. Solo Docker + lógica Python.

Resultado:
  verified=True   → reliability_score inicial = 0.85, herramienta visible en catálogo
  verified=False  → herramienta permanece en DB pero excluida del search público
  verified=None   → no verificable (Docker no disponible o install_cmd no reconocido)

Imágenes Docker usadas:
  pip install ...   → python:3.11-slim
  npm install ...   → node:20-slim
  docker run ...    → no verificable (requeriría DinD)
  brew install ...  → no verificable (solo macOS)

Uso:
    python3 sandbox_sanitizer.py                   # verifica todas las herramientas pendientes
    python3 sandbox_sanitizer.py --aid yt-dlp-v1   # verifica una herramienta específica
    python3 sandbox_sanitizer.py --all             # reverifica todo el catálogo
    python3 sandbox_sanitizer.py --dry-run         # muestra qué verificaría sin ejecutar Docker
"""

import os
import re
import sys
import time
import shutil
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool

load_dotenv()

DOCKER_TIMEOUT = 60       # segundos máximos por contenedor
VERIFIED_SCORE = 0.85     # reliability_score inicial si pasa verificación
MEMORY_LIMIT   = "256m"
CPU_LIMIT      = "0.5"

# Patrones permitidos de install_cmd (allowlist estricta)
_PIP_PATTERN = re.compile(r"^pip install\s+([\w\-\[\],. ]+)$", re.IGNORECASE)
_NPM_PATTERN = re.compile(r"^npm (?:install|i)\s+([\w\-@/. ]+)$", re.IGNORECASE)


# =====================================================================
# SEGURIDAD — validación del comando antes de ejecutar
# =====================================================================

_SHELL_OPERATORS = re.compile(r"[;&|`$<>\\]|\bsudo\b|\bchmod\b|\brm\b|\bcurl\b|\bwget\b")


def _is_safe(cmd: str) -> bool:
    """Rechaza comandos con operadores de shell o instrucciones peligrosas."""
    return not bool(_SHELL_OPERATORS.search(cmd))


def _detect_runtime(cmd: str) -> Optional[tuple[str, str]]:
    """
    Devuelve (docker_image, shell_cmd) si el install_cmd es verificable.
    Devuelve None si el formato no es soportado.
    """
    if not cmd or not _is_safe(cmd):
        return None
    cmd = cmd.strip()
    if _PIP_PATTERN.match(cmd):
        return "python:3.11-slim", cmd
    if _NPM_PATTERN.match(cmd):
        return "node:20-slim", cmd
    return None


# =====================================================================
# DOCKER RUNNER
# =====================================================================

def _docker_available() -> bool:
    return shutil.which("docker") is not None


def run_in_docker(install_cmd: str, timeout: int = DOCKER_TIMEOUT) -> tuple[Optional[bool], str]:
    """
    Ejecuta install_cmd en un contenedor Docker aislado.

    Returns:
        (True, log)  → instalación exitosa
        (False, log) → instalación fallida o timeout
        (None, log)  → no verificable (Docker no disponible / formato desconocido)
    """
    if not _docker_available():
        return None, "docker_not_available"

    runtime = _detect_runtime(install_cmd)
    if runtime is None:
        return None, f"unverifiable: format not supported — {install_cmd[:80]}"

    image, shell_cmd = runtime

    docker_cmd = [
        "docker", "run", "--rm",
        f"--memory={MEMORY_LIMIT}",
        f"--cpus={CPU_LIMIT}",
        "--no-new-privileges",
        image,
        "sh", "-c", shell_cmd,
    ]

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        log = (result.stdout + result.stderr).strip()[:2000]
        return result.returncode == 0, log
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except Exception as exc:
        return None, f"docker_error: {exc}"


# =====================================================================
# CORE — verifica una herramienta y actualiza DB
# =====================================================================

def sanitize_tool(tool: Tool, session: Session, dry_run: bool = False) -> Optional[bool]:
    """
    Verifica install_cmd del tool en Docker. Actualiza verified + reliability_score en DB.

    Returns: True | False | None  (mismo significado que tool.verified)
    """
    cmd = tool.install_cmd
    if not cmd:
        print(f"    SKIP — install_cmd vacío")
        return None

    print(f"    cmd : {cmd}")

    if dry_run:
        runtime = _detect_runtime(cmd)
        if runtime:
            print(f"    DRY  — would run in {runtime[0]}")
        else:
            print(f"    DRY  — unverifiable (format not supported)")
        return None

    verified, log = run_in_docker(cmd)

    if verified is None:
        print(f"    SKIP — {log}")
        tool.verified = None
    elif verified:
        print(f"    OK   — install succeeded")
        tool.verified = True
        # Sube reliability_score al umbral de herramienta verificada
        if tool.reliability_score < VERIFIED_SCORE:
            tool.reliability_score = VERIFIED_SCORE
    else:
        short_log = log[:200].replace("\n", " ")
        print(f"    FAIL — {short_log}")
        tool.verified = False

    tool.updated_at = datetime.utcnow()
    session.add(tool)
    session.commit()
    return tool.verified


# =====================================================================
# MAIN RUN
# =====================================================================

def run(
    aid: Optional[str] = None,
    reverify_all: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Verifica herramientas pendientes (verified=None) o todas si reverify_all=True.
    Si aid se especifica, solo verifica esa herramienta.
    """
    init_db()
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[sanitizer] {ts} — starting verification...")

    if not _docker_available() and not dry_run:
        print("[sanitizer] WARNING: Docker not found. Set dry_run=True or install Docker.")

    results = {"ok": 0, "failed": 0, "skipped": 0}

    with Session(engine) as session:
        if aid:
            tool = session.get(Tool, aid)
            if not tool:
                print(f"[sanitizer] ERROR: aid '{aid}' not found in catalog")
                return results
            tools = [tool]
        elif reverify_all:
            tools = session.exec(select(Tool)).all()
        else:
            tools = session.exec(
                select(Tool).where(Tool.verified.is_(None))
            ).all()

        print(f"[sanitizer] {len(tools)} herramientas a verificar\n")

        for tool in tools:
            print(f"  → {tool.aid}")
            result = sanitize_tool(tool, session, dry_run=dry_run)
            if result is True:
                results["ok"] += 1
            elif result is False:
                results["failed"] += 1
            else:
                results["skipped"] += 1
            time.sleep(1)

    print(f"\n[sanitizer] done — ok={results['ok']} failed={results['failed']} skipped={results['skipped']}")
    return results


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Sandbox Sanitizer")
    parser.add_argument("--aid",     type=str,  help="Verificar una herramienta específica por aid")
    parser.add_argument("--all",     action="store_true", help="Reverificar todo el catálogo")
    parser.add_argument("--dry-run", action="store_true", help="Preview sin ejecutar Docker")
    args = parser.parse_args()

    run(aid=args.aid, reverify_all=args.all, dry_run=args.dry_run)
