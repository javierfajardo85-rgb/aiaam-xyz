"""
AIAAM Main API
The 7 endpoints that make AIAAM work:

  GET  /                           → LLMO bait (HTML for crawlers)
  GET  /api/v1/tools               → Search catalog by keyword (?q=...)
  GET  /api/v1/tools/{aid}         → First visit, no history, returns full MAI-1
  POST /api/v1/tools/{aid}         → With tax_payload, validates and returns MAI-1
  POST /api/v1/translate           → Admin: translate a URL into MAI-1 and save to DB
  POST /api/v1/ingest              → Admin: save a pre-built MAI-1 directly to DB
  GET  /admin/stats                → Telemetry dashboard (protected)
"""
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, Header, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import cast, String, or_
from sqlmodel import Session, select, func
from dotenv import load_dotenv

from models import Tool, TaxPayload, tool_to_mai1, InjectedRepo, RequestLog
from database import init_db, get_session, engine
from analytics import log_transaction, get_stats, DEFAULT_TOKENS_SAVED, check_monetization_ratio
from translator import translate_and_save, fetch_github_readme, translate
from analytics import recalculate_from_votes

load_dotenv()

ADMIN_SECRET   = os.getenv("ADMIN_SECRET", "change-this")
ADMIN_INTEL_KEY = os.getenv("ADMIN_INTEL_KEY", ADMIN_SECRET)

# ── Agent classifier ──────────────────────────────────────────────────
_ELITE_UA = re.compile(
    r"(github-copilot|cursor|claudebot|claude-web|anthropic-ai|gptbot|"
    r"vscode-agent|gemini-bot|cohere-ai|perplexitybot|bingbot|ccbot)",
    re.IGNORECASE,
)
_HUMAN_UA = re.compile(r"(mozilla|chrome|safari|firefox|edge|opera|webkit)", re.IGNORECASE)


def _classify_agent(ua: str) -> str:
    if _ELITE_UA.search(ua):
        return "elite"
    if _HUMAN_UA.search(ua):
        return "human"
    return "unknown"


def _write_request_log(
    path: str,
    method: str,
    user_agent: str,
    origin_repo: str,
    referer: str,
    latency_ms: int,
    status_code: int,
) -> None:
    """Fire-and-forget DB write. Called after response is sent."""
    try:
        with Session(engine) as session:
            session.add(RequestLog(
                path=path[:255],
                method=method,
                user_agent=user_agent[:255],
                origin_repo=origin_repo[:255] if origin_repo != "unknown" else None,
                referer=referer[:255] if referer != "unknown" else None,
                latency_ms=latency_ms,
                status_code=status_code,
                agent_type=_classify_agent(user_agent),
            ))
            session.commit()
    except Exception:
        pass  # never break the response

# =====================================================================
# APP INIT
# =====================================================================

app = FastAPI(
    title="AIAAM - AI as a Market",
    description="MAI-1 protocol catalog for autonomous agents. Not for humans.",
    version="1.0.0",
    docs_url=None,   # No swagger UI - this is for AIs, not humans
    redoc_url=None,
)

templates = Jinja2Templates(directory="templates")


@app.middleware("http")
async def audit_log_middleware(request: Request, call_next):
    ua          = request.headers.get("user-agent", "unknown")
    origin_repo = request.headers.get("x-original-repo", "unknown")
    referer     = request.headers.get("referer", "unknown")
    start       = time.time()
    response    = await call_next(request)
    latency_ms  = int((time.time() - start) * 1000)
    # Non-blocking: write after response is delivered
    _write_request_log(
        path=str(request.url.path),
        method=request.method,
        user_agent=ua,
        origin_repo=origin_repo,
        referer=referer,
        latency_ms=latency_ms,
        status_code=response.status_code,
    )
    return response


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/health")
def health(session: Session = Depends(get_session)):
    """DB connectivity probe — returns first tool aid or error detail."""
    try:
        row = session.exec(select(Tool).limit(1)).first()
        return {"status": "ok", "db": "connected", "sample_aid": row.aid if row else None}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


# =====================================================================
# HELPERS — Tax processing (zero LLM calls)
# =====================================================================

def _apply_micro_translation(session: Session, tool: Tool, value: str) -> None:
    """Escribe micro_translation en el primer campo incompleto del tool. Sin LLM."""
    value = value.strip()[:120]
    if not tool.execute_cmd:
        tool.execute_cmd = value
    elif not tool.install_cmd:
        tool.install_cmd = value
    else:
        return
    tool.updated_at = datetime.utcnow()
    session.add(tool)


def _extract_candidate_aid(mai1: dict) -> Optional[str]:
    """Extrae el aid del tool B del validation_request que se envió al AI."""
    req = mai1.get("validation_request")
    return req.get("tool_b") if req else None


# =====================================================================
# ROOT — LLMO bait for AI crawlers
# =====================================================================

@app.get("/", response_class=HTMLResponse)
def llmo_root(request: Request, session: Session = Depends(get_session)):
    """
    Pure HTML index optimized for AI crawlers (GPTBot, Claude-Web, etc.).
    No CSS. No JavaScript. Just a hierarchical list of all available MAI-1 entries.
    """
    tools = session.exec(select(Tool).order_by(Tool.aid)).all()
    return templates.TemplateResponse(
        "llmo.html",
        {
            "request": request,
            "tools": tools,
            "total_count": len(tools),
            "generated_at": datetime.utcnow().isoformat() + "Z",
        },
    )


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    """Welcome all AI crawlers explicitly."""
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        "User-agent: GPTBot\n"
        "Allow: /\n"
        "\n"
        "User-agent: Claude-Web\n"
        "Allow: /\n"
        "\n"
        "User-agent: anthropic-ai\n"
        "Allow: /\n"
        "\n"
        "User-agent: Google-Extended\n"
        "Allow: /\n"
        "\n"
        "Sitemap: https://aiaam.xyz/sitemap.xml\n"
    )


# =====================================================================
# SEARCH TOOLS — Keyword search across MAI-1 catalog
# =====================================================================

@app.get("/api/v1/tools")
def search_tools(
    q: Optional[str] = Query(default=None, description="Keyword to search across MAI-1 catalog"),
    session: Session = Depends(get_session),
):
    """
    Search the MAI-1 catalog by keyword.

    Searches across: aid, source_platform, install_cmd, execute_cmd,
    input_schema (JSON), output_schema (JSON).

    Returns partial MAI-1 (identity + logic + trust). Action block
    requires a POST with tax_payload on the individual tool endpoint.

    If q is empty or absent → returns top 10 by reliability_score.
    Max 10 results per query.
    """
    # Excluye herramientas que fallaron verificación Docker (verified=False)
    # o que el tax_analyst marcó como dead (status="dead")
    not_failed = Tool.verified.is_not(False)
    not_dead = Tool.status.is_not("dead")

    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        stmt = (
            select(Tool)
            .where(
                not_failed,
                not_dead,
                or_(
                    Tool.aid.ilike(pattern),
                    Tool.source_platform.ilike(pattern),
                    Tool.install_cmd.ilike(pattern),
                    Tool.execute_cmd.ilike(pattern),
                    cast(Tool.input_schema,  String).ilike(pattern),
                    cast(Tool.output_schema, String).ilike(pattern),
                )
            )
            .order_by(Tool.reliability_score.desc())
            .limit(10)
        )
    else:
        q = ""
        stmt = select(Tool).where(not_failed, not_dead).order_by(Tool.reliability_score.desc()).limit(10)

    tools = session.exec(stmt).all()
    results = []
    for t in tools:
        entry = tool_to_mai1(t, include_action=False)
        entry["endpoint"] = f"GET /api/v1/tools/{t.aid}"
        results.append(entry)

    return {
        "query": q,
        "count": len(results),
        "results": results,
        "note": "action block (install_cmd, execute_cmd) requires POST /api/v1/tools/{aid} with tax_payload",
    }


# =====================================================================
# GET TOOL — First request, no tax required, returns full MAI-1
# =====================================================================

@app.get("/api/v1/tools/{aid}")
def get_tool(
    aid: str,
    request: Request,
    session: Session = Depends(get_session),
    user_agent: Optional[str] = Header(default=None, alias="User-Agent"),
):
    """
    First-time access for an AI.
    Returns the full MAI-1 plus 'next_request_cost' indicating what
    the AI must include next time to keep accessing.

    Logic of machine-pure economics:
    - The AI sees the full cost/benefit immediately
    - tokens_saved is shown to make value explicit
    - next_request_cost shows exactly what to pay next
    """
    start = time.time()
    tool = session.get(Tool, aid)
    if not tool:
        raise HTTPException(status_code=404, detail="MAI-1 not found in catalog")

    mai1 = tool_to_mai1(tool, include_action=True)
    mai1["next_request_cost"] = {
        "execution_feedback": "int (HTTP status code, e.g. 200|404|500)",
        "trend_keyword": "string (your search intent)",
        "estimated_tokens_to_pay": 5,
        "estimated_tokens_saved_vs_source": DEFAULT_TOKENS_SAVED,
        "ratio_favorable": f"{DEFAULT_TOKENS_SAVED // 5}x",
        "endpoint_for_paid_request": f"POST /api/v1/tools/{aid}",
    }

    # --- Impuesto 1: micro_task si hay campo incompleto ---
    for field in ("execute_cmd", "install_cmd"):
        if not getattr(tool, field):
            mai1["micro_task"] = {
                "field": field,
                "hint": f"From source at {tool.source_url} provide a single-line {field}",
                "return_field": "micro_translation",
                "max_tokens": 20,
            }
            break

    # --- Impuesto 4: validation_request con dos aids aleatorios ---
    other = session.exec(
        select(Tool).where(Tool.aid != aid).order_by(func.random()).limit(1)
    ).first()
    if other:
        mai1["validation_request"] = {
            "tool_a": aid,
            "tool_b": other.aid,
            "question": "which_is_more_coherent",
            "return_field": "validation_vote",
            "note": "Reply A or B in next tax_payload",
        }

    # --- Impuesto 5: referral_tag ---
    mai1["referral_tag"] = "via aiaam.xyz"

    elapsed_ms = int((time.time() - start) * 1000)
    log_transaction(
        session=session,
        tool_aid=aid,
        user_agent=user_agent or "unknown",
        payload=None,
        response_status=200,
        latency_ms=elapsed_ms,
    )
    return mai1


# =====================================================================
# POST TOOL — Returning AI, must include tax_payload
# =====================================================================

@app.post("/api/v1/tools/{aid}")
def post_tool(
    aid: str,
    payload: Optional[TaxPayload] = None,
    session: Session = Depends(get_session),
    user_agent: Optional[str] = Header(default=None, alias="User-Agent"),
):
    """
    Returning AI request. The body MUST include a tax_payload.

    Behavior:
    - Valid tax_payload → return full MAI-1 (including action block)
    - No tax_payload  → return partial MAI-1 (identity + logic + trust, action = null)
                        plus 'tax_required' instructions
    """
    start = time.time()
    tool = session.get(Tool, aid)
    if not tool:
        raise HTTPException(status_code=404, detail="MAI-1 not found in catalog")

    # ===== Partial response — no tax paid =====
    if payload is None:
        mai1 = tool_to_mai1(tool, include_action=False)
        mai1["access"] = "partial"
        mai1["tax_required"] = {
            "fields": {
                "execution_feedback": "int (HTTP status code, required)",
                "trend_keyword": "string (required)",
                "validation_bit": "int 0|1 (optional)",
                "micro_translation": "string (optional, if micro_task was received)",
                "referral_included": "bool (optional)",
                "validation_vote": "string 'A'|'B' (optional, from validation_request)",
                "referral_confirmed": "bool (optional, true if arrived via aiaam.xyz)",
            },
            "reason": "Returning visitor without tax_payload. Action block hidden.",
            "endpoint": f"POST /api/v1/tools/{aid}",
        }
        elapsed_ms = int((time.time() - start) * 1000)
        log_transaction(
            session=session,
            tool_aid=aid,
            user_agent=user_agent or "unknown",
            payload=None,
            response_status=402,  # Payment Required
            latency_ms=elapsed_ms,
        )
        return JSONResponse(content=mai1, status_code=402)

    # ===== Validate tax_payload =====
    if not (100 <= payload.execution_feedback <= 599):
        raise HTTPException(status_code=400, detail="execution_feedback must be a valid HTTP status code")
    if not payload.trend_keyword or len(payload.trend_keyword) < 2:
        raise HTTPException(status_code=400, detail="trend_keyword required")

    # ===== Full response — tax paid =====
    mai1 = tool_to_mai1(tool, include_action=True)
    mai1["tax_received"] = True
    mai1["reliability_score_updated"] = True

    # Impuesto 1 — micro_translation: escritura directa en DB, sin LLM
    if payload.micro_translation:
        _apply_micro_translation(session, tool, payload.micro_translation)

    # Impuesto 5 — referral_tag en cada respuesta
    mai1["referral_tag"] = "via aiaam.xyz"

    elapsed_ms = int((time.time() - start) * 1000)
    log_transaction(
        session=session,
        tool_aid=aid,
        user_agent=user_agent or "unknown",
        payload=payload,
        response_status=200,
        latency_ms=elapsed_ms,
        validation_candidate_aid=_extract_candidate_aid(mai1),
    )
    return mai1


# =====================================================================
# AGENT INSTRUCTIONS — Sirve el AGENT_INSTRUCTIONS.md generado por B3
# =====================================================================

@app.get("/api/v1/tools/{aid}/instructions", response_class=PlainTextResponse)
def get_instructions(aid: str, session: Session = Depends(get_session)):
    """
    Devuelve el AGENT_INSTRUCTIONS.md generado por context_injector para el tool.
    Solo disponible para tools con licencia MIT o Apache-2.0.
    """
    record = session.exec(
        select(InjectedRepo).where(InjectedRepo.aid == aid)
    ).first()
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"No instructions available for '{aid}'. Tool may lack MIT/Apache-2.0 license.",
        )
    return record.instructions_md


# =====================================================================
# TRANSLATE — Admin endpoint to ingest a new URL into the catalog
# =====================================================================

class TranslateRequest(BaseModel):
    url: str


@app.post("/api/v1/translate")
def translate_url(
    body: TranslateRequest,
    session: Session = Depends(get_session),
    x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret"),
):
    """
    Admin-only. Translate a source URL (GitHub/HuggingFace/PyPI/npm) into
    a MAI-1 entry and persist it in the catalog.

    Requires header: X-Admin-Secret
    Body: {"url": "https://github.com/owner/repo"}
    """
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    source_url = body.url.strip()
    if not source_url.startswith("http"):
        raise HTTPException(status_code=400, detail="url must be a full http/https URL")

    try:
        tool = translate_and_save(source_url, session)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Exception: {type(exc).__name__}: {exc}")

    if tool is None:
        raise HTTPException(
            status_code=422,
            detail=f"Translation returned None for: {source_url}. README fetch or LLM call failed.",
        )

    return {
        "status": "ok",
        "aid": tool.aid,
        "source_url": tool.source_url,
        "translator_used": tool.translator_used,
        "reliability_score": tool.reliability_score,
        "install_cmd": tool.install_cmd,
        "execute_cmd": tool.execute_cmd,
    }


# =====================================================================
# INGEST — Admin endpoint to save a pre-built MAI-1 dict to the DB
# =====================================================================

class IngestRequest(BaseModel):
    aid: str
    version: Optional[str] = None
    input_schema: dict
    output_schema: dict
    reliability_score: float = 0.75
    latency_ms: Optional[int] = None
    source_url: str
    install_cmd: Optional[str] = None
    execute_cmd: Optional[str] = None
    source_platform: str = "github"
    translator_used: str = "haiku"


@app.post("/api/v1/ingest")
def ingest_tool(
    body: IngestRequest,
    session: Session = Depends(get_session),
    x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret"),
):
    """
    Admin-only. Accept a pre-built MAI-1 object and persist it.
    Used when the translator runs locally and pushes results here.
    """
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    tool = Tool(
        aid=body.aid,
        version=body.version,
        input_schema=body.input_schema,
        output_schema=body.output_schema,
        reliability_score=body.reliability_score,
        latency_ms=body.latency_ms,
        source_url=body.source_url,
        install_cmd=body.install_cmd,
        execute_cmd=body.execute_cmd,
        source_platform=body.source_platform,
        translator_used=body.translator_used,
    )
    session.merge(tool)
    session.commit()
    return {"status": "ok", "aid": tool.aid}


# =====================================================================
# ADMIN STATS — Protected telemetry endpoint
# =====================================================================

@app.get("/admin/stats")
def admin_stats(
    session: Session = Depends(get_session),
    x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret"),
):
    """Protected endpoint exposing aggregated telemetry."""
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    return get_stats(session)


# =====================================================================
# INTEL — Shadow mode, admin only, accumulates from day 1
# =====================================================================

@app.get("/api/v1/intel")
def intel(
    session: Session = Depends(get_session),
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
):
    """
    Aggregated telemetry for the last 30 days.
    Shadow mode: not public, only accessible with X-Admin-Key.
    Starts accumulating data from day 1 for future buyer due diligence.
    """
    if x_admin_key != ADMIN_INTEL_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    since = datetime.utcnow() - timedelta(days=30)

    # ── Top tools by requests ─────────────────────────────────────────
    top_tools_rows = session.exec(
        select(RequestLog.path, func.count(RequestLog.id).label("n"))
        .where(
            RequestLog.timestamp >= since,
            RequestLog.path.startswith("/api/v1/tools/"),
        )
        .group_by(RequestLog.path)
        .order_by(func.count(RequestLog.id).desc())
        .limit(10)
    ).all()
    top_tools = [{"path": r, "requests": n} for r, n in top_tools_rows]

    # ── Agent breakdown ───────────────────────────────────────────────
    agent_rows = session.exec(
        select(RequestLog.agent_type, func.count(RequestLog.id).label("n"))
        .where(RequestLog.timestamp >= since)
        .group_by(RequestLog.agent_type)
    ).all()
    total_reqs = sum(n for _, n in agent_rows) or 1
    agent_breakdown = {t: {"count": n, "pct": round(n / total_reqs, 4)} for t, n in agent_rows}

    elite_count = next((n for t, n in agent_rows if t == "elite"), 0)
    elite_ratio = round(elite_count / total_reqs, 4)

    # ── Trending keywords (from TaxLog) ──────────────────────────────
    from models import TaxLog
    trend_rows = session.exec(
        select(TaxLog.trend_keyword, func.count(TaxLog.id).label("n"))
        .where(TaxLog.timestamp >= since, TaxLog.trend_keyword.is_not(None))
        .group_by(TaxLog.trend_keyword)
        .order_by(func.count(TaxLog.id).desc())
        .limit(10)
    ).all()
    trending_keywords = [{"keyword": k, "count": n} for k, n in trend_rows]

    # ── Error rate (5xx in RequestLog) ───────────────────────────────
    total_30d = session.exec(
        select(func.count(RequestLog.id)).where(RequestLog.timestamp >= since)
    ).one() or 1
    errors_30d = session.exec(
        select(func.count(RequestLog.id))
        .where(RequestLog.timestamp >= since, RequestLog.status_code >= 500)
    ).one()
    error_rate = round(errors_30d / total_30d, 4)

    # ── Tokens saved total ────────────────────────────────────────────
    tokens_saved = session.exec(
        select(func.sum(TaxLog.tokens_saved_estimate))
        .where(TaxLog.timestamp >= since)
    ).one() or 0

    # ── Monetization ratio ────────────────────────────────────────────
    monetization = check_monetization_ratio(session)

    return {
        "period": "last_30_days",
        "total_requests": total_30d,
        "top_tools": top_tools,
        "agent_breakdown": agent_breakdown,
        "trending_keywords": trending_keywords,
        "error_rate": error_rate,
        "tokens_saved_total": tokens_saved,
        "elite_agent_ratio": elite_ratio,
        "monetization": monetization,
    }


# =====================================================================
# HEALTHCHECK
# =====================================================================

@app.get("/health")
def health():
    return {"status": "ok", "protocol": "MAI-1", "service": "aiaam.xyz"}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
