"""
AIAAM Zero Waste Auditor — Agente B6
Detecta entradas MAI-1 con footprint de tokens excesivo y las comprime.

Estrategia (coste ascendente):
  1. Reglas (zero LLM): primera línea de comandos multilínea, elimina claves
     verbose de esquemas (description, example, title…).
  2. Haiku (solo si sigue sobre umbral): comprime el campo de prosa más largo
     a una sola línea ≤ 15 palabras. ~30 tokens por herramienta afectada.

Umbral: TOKEN_BUDGET = 300 tokens  (≈ len(json) // 4)

Principio de coste máximo:
  - Zero LLM para herramientas bajo umbral
  - Haiku solo cuando reglas no bastan — esperado: <5 % del catálogo
  - Cache implícita: si token_count <= TOKEN_BUDGET → skip

Uso:
    python3 zero_waste_auditor.py             # audita todos los tools
    python3 zero_waste_auditor.py --aid yt-dlp-v1
    python3 zero_waste_auditor.py --dry-run   # preview sin escribir
"""

import json
import os
import sys
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from sqlmodel import Session, select
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool, tool_to_mai1

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TOKEN_BUDGET      = 300    # tokens por encima de los cuales se audita
MODEL_HAIKU       = os.getenv("TRANSLATOR_MODEL_PRIMARY", "claude-haiku-4-5-20251001")

client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# =====================================================================
# TOKEN ESTIMATE — sin tiktoken, aproximación suficiente para umbral
# =====================================================================

def _token_estimate(tool: Tool) -> int:
    """Aproximación: chars del JSON serializado // 4 (≈1 token per 4 chars)."""
    return len(json.dumps(tool_to_mai1(tool, include_action=True))) // 4


# =====================================================================
# REGLAS — zero LLM
# =====================================================================

_VERBOSE_KEYS = {"description", "example", "examples", "default", "title", "comment", "$comment"}


def _strip_verbose(obj):
    """Elimina recursivamente claves verbosas de dicts/listas."""
    if isinstance(obj, dict):
        return {k: _strip_verbose(v) for k, v in obj.items() if k not in _VERBOSE_KEYS}
    if isinstance(obj, list):
        return [_strip_verbose(i) for i in obj]
    return obj


def _apply_rules(tool: Tool) -> bool:
    """
    Aplica reglas de truncación sin LLM.
    Devuelve True si se hizo algún cambio.
    """
    changed = False

    # 1. install_cmd multilínea → primera línea
    if tool.install_cmd and "\n" in tool.install_cmd:
        tool.install_cmd = tool.install_cmd.split("\n")[0].strip()
        changed = True

    # 2. execute_cmd multilínea → primera línea
    if tool.execute_cmd and "\n" in tool.execute_cmd:
        tool.execute_cmd = tool.execute_cmd.split("\n")[0].strip()
        changed = True

    # 3. Esquemas: eliminar claves verbose
    for attr in ("input_schema", "output_schema"):
        schema = getattr(tool, attr)
        if schema and isinstance(schema, dict):
            clean = _strip_verbose(schema)
            if clean != schema:
                setattr(tool, attr, clean)
                changed = True

    return changed


# =====================================================================
# LLM — Haiku solo si reglas no bastan
# =====================================================================

def _haiku_compress(text: str) -> Optional[str]:
    """Comprime un campo de texto largo a una sola línea (≤15 palabras). ~30 tokens."""
    if not client:
        return None
    prompt = (
        f"Shorten the following to ONE line, max 15 words, keep it actionable:\n{text}"
    )
    try:
        resp = client.messages.create(
            model=MODEL_HAIKU,
            max_tokens=40,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        print(f"    [haiku] error: {exc}")
        return None


def _apply_haiku(tool: Tool) -> bool:
    """
    Comprime el campo de prosa más largo (install_cmd o execute_cmd).
    Solo se llama si _apply_rules() no fue suficiente.
    Devuelve True si hubo cambio.
    """
    candidates = [
        ("execute_cmd", tool.execute_cmd or ""),
        ("install_cmd", tool.install_cmd or ""),
    ]
    # Ordena por longitud descendente — comprime el más largo primero
    candidates.sort(key=lambda x: len(x[1]), reverse=True)

    changed = False
    for field, value in candidates:
        if len(value) > 80:
            compressed = _haiku_compress(value)
            if compressed and compressed != value:
                setattr(tool, field, compressed)
                changed = True
            break  # un campo por llamada es suficiente

    return changed


# =====================================================================
# CORE
# =====================================================================

def audit_tool(
    tool: Tool,
    session: Session,
    dry_run: bool = False,
) -> dict:
    """
    Audita un tool, aplica reglas y opcionalmente llama a Haiku.
    """
    before = _token_estimate(tool)

    if before <= TOKEN_BUDGET:
        return {"aid": tool.aid, "before": before, "after": before, "action": "skip"}

    # Paso 1 — reglas
    rules_changed = _apply_rules(tool)
    after_rules   = _token_estimate(tool)

    haiku_called = False

    # Paso 2 — Haiku solo si sigue sobre umbral
    if after_rules > TOKEN_BUDGET:
        if dry_run:
            print(f"    DRY — would call Haiku (still {after_rules} tokens after rules)")
        else:
            haiku_changed = _apply_haiku(tool)
            if haiku_changed:
                haiku_called = True

    after = _token_estimate(tool)

    action = "rules" if rules_changed and not haiku_called else \
             "haiku" if haiku_called else \
             "rules+haiku" if rules_changed and haiku_called else "none"

    if not dry_run and (rules_changed or haiku_called):
        tool.updated_at = datetime.utcnow()
        session.add(tool)
        session.commit()

    return {
        "aid":         tool.aid,
        "before":      before,
        "after":       after,
        "saved":       before - after,
        "action":      action,
        "haiku_called": haiku_called,
    }


# =====================================================================
# MAIN RUN
# =====================================================================

def run(
    aid: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    init_db()
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"\n[auditor] {ts} [{mode}] — auditando footprint de tokens...")

    summary = {"audited": 0, "skipped": 0, "rules_only": 0, "haiku_calls": 0, "tokens_saved": 0}

    with Session(engine) as session:
        if aid:
            tool = session.get(Tool, aid)
            if not tool:
                print(f"[auditor] ERROR: aid '{aid}' not found")
                return summary
            tools = [tool]
        else:
            tools = session.exec(select(Tool)).all()

        for tool in tools:
            r = audit_tool(tool, session, dry_run=dry_run)
            if r["action"] == "skip":
                summary["skipped"] += 1
                continue

            summary["audited"] += 1
            summary["tokens_saved"] += r["saved"]
            if r["haiku_called"]:
                summary["haiku_calls"] += 1
            else:
                summary["rules_only"] += 1

            print(
                f"  → {tool.aid}  "
                f"{r['before']}→{r['after']} tokens  "
                f"action={r['action']}"
            )

    print(
        f"\n[auditor] done — audited={summary['audited']} "
        f"skipped={summary['skipped']} "
        f"rules_only={summary['rules_only']} "
        f"haiku_calls={summary['haiku_calls']} "
        f"tokens_saved={summary['tokens_saved']}"
    )
    return summary


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Zero Waste Auditor")
    parser.add_argument("--aid",     type=str, help="Auditar un tool específico")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview sin escribir en DB ni llamar a Haiku")
    args = parser.parse_args()

    run(aid=args.aid, dry_run=args.dry_run)
