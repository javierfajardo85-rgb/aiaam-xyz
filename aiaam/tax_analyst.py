"""
AIAAM Tax Analyst — Agente B5
Lee TaxLogs cada hora y aplica penalizaciones de fiabilidad. Sin LLM.

Reglas:
  - >3 errores (execution_feedback >= 400) en las últimas 24 h → score -= 0.1
  - score < 0.50 → status = "degraded"
  - score < 0.30 → status = "dead"   (excluido del catálogo de búsqueda)
  - score >= 0.50 → status = "active"

Modo coste máximo:
  - Zero Anthropic calls — aritmética pura sobre DB
  - Corre como cron job cada hora (--loop) o una sola vez

Uso:
    python3 tax_analyst.py             # corre una vez sobre todos los tools
    python3 tax_analyst.py --loop      # bucle infinito cada 3600 s
    python3 tax_analyst.py --dry-run   # preview sin escribir en DB
    python3 tax_analyst.py --aid yt-dlp-v1  # analiza un tool concreto
"""

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool, TaxLog
from analytics import log_agent_run


ERROR_THRESHOLD   = 3     # errores en 24 h que disparan penalización
SCORE_PENALTY     = 0.10  # cuánto se resta por exceso de errores
DEGRADED_CUTOFF   = 0.50  # por debajo → "degraded"
DEAD_CUTOFF       = 0.30  # por debajo → "dead"
WINDOW_HOURS      = 24    # ventana de análisis en horas
LOOP_INTERVAL_S   = 3600  # intervalo del bucle --loop (segundos)


def _status_for_score(score: float) -> str:
    if score < DEAD_CUTOFF:
        return "dead"
    if score < DEGRADED_CUTOFF:
        return "degraded"
    return "active"


def analyse_tool(
    tool: Tool,
    session: Session,
    dry_run: bool = False,
) -> dict:
    """
    Evalúa un tool, aplica penalización si procede y actualiza status.
    Devuelve un dict con el resultado del análisis.
    """
    window_start = datetime.utcnow() - timedelta(hours=WINDOW_HOURS)

    # Errores en la ventana de 24 h
    logs = session.exec(
        select(TaxLog).where(
            TaxLog.tool_aid == tool.aid,
            TaxLog.timestamp >= window_start,
            TaxLog.execution_feedback >= 400,
        )
    ).all()
    error_count = len(logs)

    old_score  = tool.reliability_score
    new_score  = old_score
    penalised  = False

    if error_count > ERROR_THRESHOLD:
        new_score = round(max(0.0, old_score - SCORE_PENALTY), 4)
        penalised = True

    new_status = _status_for_score(new_score)
    changed    = (new_score != old_score) or (new_status != (tool.status or "active"))

    result = {
        "aid":          tool.aid,
        "errors_24h":   error_count,
        "old_score":    old_score,
        "new_score":    new_score,
        "old_status":   tool.status or "active",
        "new_status":   new_status,
        "penalised":    penalised,
        "changed":      changed,
    }

    if changed and not dry_run:
        tool.reliability_score = new_score
        tool.status            = new_status
        tool.updated_at        = datetime.utcnow()
        session.add(tool)
        session.commit()

    return result


def run(
    aid: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """
    Analiza todos los tools (o uno solo si aid está especificado).
    """
    _t0 = time.time()
    init_db()
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"\n[tax_analyst] {ts} [{mode}] — analizando telemetría...")

    summary = {"analysed": 0, "penalised": 0, "degraded": 0, "dead": 0, "unchanged": 0}

    with Session(engine) as session:
        if aid:
            tool = session.get(Tool, aid)
            if not tool:
                print(f"[tax_analyst] ERROR: aid '{aid}' not found")
                return summary
            tools = [tool]
        else:
            tools = session.exec(select(Tool)).all()

        for tool in tools:
            r = analyse_tool(tool, session, dry_run=dry_run)
            summary["analysed"] += 1

            if r["changed"]:
                flag = ""
                if r["penalised"]:
                    flag = f"  ⚠  score {r['old_score']} → {r['new_score']}"
                    summary["penalised"] += 1
                status_changed = r["old_status"] != r["new_status"]
                status_note = f"  status={r['new_status']}" if status_changed else ""
                print(
                    f"  → {tool.aid}  errors_24h={r['errors_24h']}"
                    f"{flag}{status_note}"
                )
            else:
                summary["unchanged"] += 1

            if r["new_status"] == "degraded":
                summary["degraded"] += 1
            elif r["new_status"] == "dead":
                summary["dead"] += 1

    print(
        f"\n[tax_analyst] done — analysed={summary['analysed']} "
        f"penalised={summary['penalised']} "
        f"degraded={summary['degraded']} dead={summary['dead']} "
        f"unchanged={summary['unchanged']}"
    )
    if not dry_run:
        log_agent_run(
            agent_code="B5", agent_name="Tax Analyst",
            items_processed=summary["analysed"],
            items_new=0, items_failed=summary["penalised"],
            duration_s=int(time.time() - _t0),
            summary=f"penalised={summary['penalised']} degraded={summary['degraded']} dead={summary['dead']}",
        )
    return summary


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Tax Analyst")
    parser.add_argument("--aid",     type=str,  help="Analizar un tool específico")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview sin escribir en DB")
    parser.add_argument("--loop",    action="store_true",
                        help=f"Bucle infinito, corre cada {LOOP_INTERVAL_S}s")
    args = parser.parse_args()

    if args.loop:
        print(f"[tax_analyst] modo loop — intervalo {LOOP_INTERVAL_S}s")
        while True:
            run(aid=args.aid, dry_run=args.dry_run)
            print(f"[tax_analyst] durmiendo {LOOP_INTERVAL_S}s...")
            time.sleep(LOOP_INTERVAL_S)
    else:
        run(aid=args.aid, dry_run=args.dry_run)
