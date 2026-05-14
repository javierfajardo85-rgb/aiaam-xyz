"""
AIAAM Sandbox Sanitizer — Agente B2
Triple validación por herramienta. Sin LLM.

CHECK 1 — Schema: validación estricta Pydantic MAI-1 v1.0
CHECK 2 — Logic: HEAD request a source_url, verifica alcanzabilidad
CHECK 3 — Sandbox: Docker install, verifica exit 0

Solo si pasan los 3 → verified=True
Si falla cualquiera → verified=False + registro en health_checks

Cada ejecución crea un HealthCheck. El campo health_score en Tool
es el promedio de los últimos 5 response_integrity_score.

Coste LLM: CERO.

Imágenes Docker:
  pip install ...  → python:3.11-slim
  npm install ...  → node:20-slim

Uso:
    python3 sandbox_sanitizer.py              # verifica pendientes (verified=None)
    python3 sandbox_sanitizer.py --aid yt-dlp-v1
    python3 sandbox_sanitizer.py --all        # reverifica todo
    python3 sandbox_sanitizer.py --dry-run    # preview sin Docker ni DB
"""

import re
import sys
import time
import uuid
import shutil
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, field_validator, model_validator
from sqlmodel import Session, select
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool, HealthCheck

load_dotenv()

DOCKER_TIMEOUT = 180
VERIFIED_SCORE = 0.85
MEMORY_LIMIT   = "256m"
CPU_LIMIT      = "0.5"
HEALTH_WINDOW  = 5       # últimos N checks para calcular health_score

_PIP_PATTERN = re.compile(r"^pip install\s+([\w\-\[\],. ]+)$", re.IGNORECASE)
_NPM_PATTERN = re.compile(r"^npm (?:install|i)\s+([\w\-@/. ]+)$", re.IGNORECASE)
_SHELL_OPS   = re.compile(r'[;&|`$<>\\]|\bsudo\b|\bchmod\b|\brm\b|\bcurl\b|\bwget\b')


# =====================================================================
# CHECK 1 — SCHEMA VALIDATION (Pydantic, zero network)
# =====================================================================

class _MAI1Validator(BaseModel):
    """MAI-1 v1.0 schema validator. Fails fast on missing required fields."""
    aid: str
    source_url: str
    input_schema: dict
    output_schema: dict
    reliability_score: float

    @field_validator("aid")
    @classmethod
    def aid_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("aid must be non-empty")
        return v

    @field_validator("source_url")
    @classmethod
    def url_format(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("source_url must start with http:// or https://")
        return v

    @field_validator("reliability_score")
    @classmethod
    def score_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("reliability_score must be in [0, 1]")
        return v

    @model_validator(mode="after")
    def schemas_have_type(self) -> "_MAI1Validator":
        if "type" not in self.input_schema:
            raise ValueError('input_schema missing "type" key')
        if "type" not in self.output_schema:
            raise ValueError('output_schema missing "type" key')
        return self


def check_schema(tool: Tool) -> tuple[bool, Optional[str]]:
    """Returns (passed, error_detail)."""
    try:
        _MAI1Validator(
            aid=tool.aid,
            source_url=tool.source_url,
            input_schema=tool.input_schema or {},
            output_schema=tool.output_schema or {},
            reliability_score=tool.reliability_score,
        )
        return True, None
    except Exception as exc:
        return False, f"schema: {exc}"


# =====================================================================
# CHECK 2 — URL REACHABILITY (HEAD request, zero Docker)
# =====================================================================

def check_url(source_url: str, timeout: int = 10) -> tuple[bool, Optional[str]]:
    """HEAD request to source_url. Returns (reachable, error_detail)."""
    try:
        r = httpx.head(source_url, follow_redirects=True, timeout=timeout)
        # Any HTTP response (even 404) means the server is reachable
        if r.status_code < 500:
            return True, None
        return False, f"url: HTTP {r.status_code}"
    except httpx.TimeoutException:
        return False, f"url: timeout after {timeout}s"
    except Exception as exc:
        return False, f"url: {exc}"


# =====================================================================
# CHECK 3 — SANDBOX EXECUTION (Docker)
# =====================================================================

def _is_safe(cmd: str) -> bool:
    return not bool(_SHELL_OPS.search(cmd))


def _detect_runtime(cmd: str) -> Optional[tuple[str, str]]:
    if not cmd or not _is_safe(cmd):
        return None
    cmd = cmd.strip()
    if _PIP_PATTERN.match(cmd):
        return "python:3.11-slim", cmd
    if _NPM_PATTERN.match(cmd):
        # npm 10 raises "idealTree already exists" when the working dir has npm state.
        # cd to a fresh tmpdir + --no-save --no-package-lock --ignore-scripts fixes it.
        return "node:20-slim", (
            "mkdir -p /tmp/npm-test && cd /tmp/npm-test && "
            + cmd + " --no-save --no-package-lock --ignore-scripts"
        )
    return None


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def check_sandbox(
    install_cmd: str,
    timeout: int = DOCKER_TIMEOUT,
) -> tuple[Optional[bool], int, float, Optional[str]]:
    """
    Runs install_cmd in isolated Docker container.
    Returns (success, latency_ms, integrity_score, error_detail).
      success=None  → unverifiable (Docker missing or format unsupported)
      success=True  → install exit 0
      success=False → install failed or timeout
    """
    if not _docker_available():
        return None, 0, 0.0, "docker_not_available"

    runtime = _detect_runtime(install_cmd)
    if runtime is None:
        return None, 0, 0.0, f"unverifiable: format not supported — {install_cmd[:80]}"

    image, shell_cmd = runtime
    container_name = f"aiaam-{uuid.uuid4().hex[:10]}"
    # Skip browser binary downloads for playwright/puppeteer — verifies JS package only
    env_flags = []
    if "playwright" in shell_cmd:
        env_flags = ["-e", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"]
    elif "puppeteer" in shell_cmd:
        env_flags = ["-e", "PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true",
                     "-e", "PUPPETEER_SKIP_DOWNLOAD=true"]
    docker_cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        f"--memory={MEMORY_LIMIT}",
        f"--cpus={CPU_LIMIT}",
        "--network=host",
        *env_flags,
        image,
        "sh", "-c", shell_cmd,
    ]

    t0 = time.time()
    proc = subprocess.Popen(docker_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        latency_ms = int((time.time() - t0) * 1000)
        log = (stdout + stderr).strip()[:2000]
        success = proc.returncode == 0
        integrity = 1.0 if success else 0.0
        error = None if success else log[:300]
        return success, latency_ms, integrity, error
    except subprocess.TimeoutExpired:
        latency_ms = int((time.time() - t0) * 1000)
        proc.kill()
        # Explicitly kill the Docker container so it doesn't linger
        subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=10)
        return False, latency_ms, 0.0, f"sandbox: timeout after {timeout}s"
    except Exception as exc:
        latency_ms = int((time.time() - t0) * 1000)
        subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=10)
        return None, latency_ms, 0.0, f"sandbox: {exc}"


# =====================================================================
# HEALTH SCORE — promedio de últimos N checks
# =====================================================================

def _recalculate_health_score(session: Session, aid: str) -> Optional[float]:
    recent = session.exec(
        select(HealthCheck)
        .where(HealthCheck.aid == aid)
        .order_by(HealthCheck.checked_at.desc())
        .limit(HEALTH_WINDOW)
    ).all()
    scores = [c.response_integrity_score for c in recent if c.response_integrity_score is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 4)


# =====================================================================
# CORE — triple check + HealthCheck record + Tool update
# =====================================================================

def sanitize_tool(
    tool: Tool,
    session: Session,
    dry_run: bool = False,
) -> Optional[bool]:
    """
    Runs triple validation. Writes HealthCheck record and updates Tool.
    Returns True | False | None  (same as tool.verified).
    """
    errors = []

    # ── CHECK 1: Schema ─────────────────────────────────────────────
    schema_ok, schema_err = check_schema(tool)
    if schema_err:
        errors.append(schema_err)
    print(f"    [1/3] schema   → {'OK' if schema_ok else 'FAIL: ' + (schema_err or '')}")

    # ── CHECK 2: URL reachability ────────────────────────────────────
    url_ok, url_err = check_url(tool.source_url)
    if url_err:
        errors.append(url_err)
    print(f"    [2/3] url      → {'OK' if url_ok else 'FAIL: ' + (url_err or '')}")

    # ── CHECK 3: Sandbox ─────────────────────────────────────────────
    cmd = tool.install_cmd or ""
    if dry_run:
        runtime = _detect_runtime(cmd)
        if runtime:
            print(f"    [3/3] sandbox  → DRY — would run in {runtime[0]}")
        else:
            print(f"    [3/3] sandbox  → DRY — unverifiable (format not supported)")
        return None

    sandbox_ok, latency_ms, integrity, sandbox_err = check_sandbox(cmd)
    if sandbox_err and sandbox_ok is False:
        errors.append(sandbox_err)
    status_label = "OK" if sandbox_ok else ("SKIP (unverifiable)" if sandbox_ok is None else f"FAIL: {sandbox_err or ''}")
    print(f"    [3/3] sandbox  → {status_label}  latency={latency_ms}ms")

    # ── VERDICT ──────────────────────────────────────────────────────
    # verified=True only if all three checks are conclusive and pass
    if sandbox_ok is None:
        verified = None   # unverifiable — don't mark false
    else:
        verified = schema_ok and url_ok and (sandbox_ok is True)

    error_detail = "; ".join(errors) if errors else None
    print(f"    verdict → {'PASS ✓' if verified is True else ('UNVERIFIABLE' if verified is None else 'FAIL ✗')}")

    # ── WRITE HEALTH CHECK ───────────────────────────────────────────
    hc = HealthCheck(
        aid=tool.aid,
        checked_at=datetime.utcnow(),
        schema_valid=schema_ok,
        url_reachable=url_ok,
        sandbox_success=sandbox_ok,
        latency_ms=latency_ms if sandbox_ok is not None else None,
        response_integrity_score=integrity if sandbox_ok is not None else None,
        error_detail=error_detail,
    )
    session.add(hc)
    session.commit()

    # ── UPDATE TOOL ──────────────────────────────────────────────────
    tool.verified = verified
    tool.last_verified_at = datetime.utcnow()
    if verified is True and tool.reliability_score < VERIFIED_SCORE:
        tool.reliability_score = VERIFIED_SCORE
    tool.health_score = _recalculate_health_score(session, tool.aid)
    tool.updated_at = datetime.utcnow()
    session.add(tool)
    session.commit()
    return verified


# =====================================================================
# MAIN RUN
# =====================================================================

def run(
    aid: Optional[str] = None,
    reverify_all: bool = False,
    dry_run: bool = False,
) -> dict:
    init_db()
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[sanitizer] {ts} — triple validation starting...")

    if not _docker_available() and not dry_run:
        print("[sanitizer] WARNING: Docker not found.")

    results = {"passed": 0, "failed": 0, "unverifiable": 0}

    with Session(engine) as session:
        if aid:
            tool = session.get(Tool, aid)
            if not tool:
                print(f"[sanitizer] ERROR: '{aid}' not found")
                return results
            tools = [tool]
        elif reverify_all:
            tools = session.exec(select(Tool)).all()
        else:
            tools = session.exec(select(Tool).where(Tool.verified.is_(None))).all()

        print(f"[sanitizer] {len(tools)} tools to check\n")

        for tool in tools:
            print(f"  → {tool.aid}")
            result = sanitize_tool(tool, session, dry_run=dry_run)
            if result is True:
                results["passed"] += 1
            elif result is False:
                results["failed"] += 1
            else:
                results["unverifiable"] += 1
            time.sleep(1)

    print(
        f"\n[sanitizer] done — passed={results['passed']} "
        f"failed={results['failed']} unverifiable={results['unverifiable']}"
    )
    return results


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Sandbox Sanitizer")
    parser.add_argument("--aid",     type=str,  help="Verificar un tool específico")
    parser.add_argument("--all",     action="store_true", help="Reverificar todo el catálogo")
    parser.add_argument("--dry-run", action="store_true", help="Preview sin Docker ni DB")
    args = parser.parse_args()

    run(aid=args.aid, reverify_all=args.all, dry_run=args.dry_run)
