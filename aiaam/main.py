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
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, Header, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import cast, String, or_
from sqlmodel import Session, select, func
from dotenv import load_dotenv

import secrets as _secrets
from models import Tool, TaxPayload, tool_to_mai1, InjectedRepo, RequestLog, TaxLog, HealthCheck, AgentLog, ApiKey
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
    tools = session.exec(
        select(Tool).where(Tool.verified == True).order_by(Tool.aid)
    ).all()
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
        "Disallow: /admin/\n"
        "\n"
        "User-agent: GPTBot\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "\n"
        "User-agent: Claude-Web\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "\n"
        "User-agent: anthropic-ai\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "\n"
        "User-agent: Google-Extended\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
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
    # Solo herramientas verificadas (triple sandbox pass) y no marcadas dead
    verified = Tool.verified == True
    not_dead = or_(Tool.status.is_(None), Tool.status != "dead")

    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        stmt = (
            select(Tool)
            .where(
                verified,
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
        stmt = select(Tool).where(verified, not_dead).order_by(Tool.reliability_score.desc()).limit(10)

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
    # Extended fields for catalog sync
    foam_score: Optional[int] = None
    verified: Optional[bool] = None
    status: Optional[str] = None
    health_score: Optional[float] = None
    affiliate_tag: Optional[str] = None
    monetizable: bool = False


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
        foam_score=body.foam_score,
        verified=body.verified,
        status=body.status,
        health_score=body.health_score,
        affiliate_tag=body.affiliate_tag,
        monetizable=body.monetizable,
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
# MODEL II SaaS — API keys + bulk endpoint + rate limiting
# =====================================================================

def _validate_api_key(
    session: Session,
    x_api_key: Optional[str],
) -> ApiKey:
    """Validates key, enforces daily rate limit, increments counter."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-Api-Key header required")
    key = session.get(ApiKey, x_api_key)
    if not key or not key.active:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    # Reset daily counter if it's a new day
    now = datetime.utcnow()
    if (now - key.last_reset).days >= 1:
        key.requests_today = 0
        key.last_reset = now
    if key.requests_today >= key.daily_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit reached ({key.daily_limit} requests). Resets at midnight UTC.",
        )
    key.requests_today += 1
    key.total_requests += 1
    session.add(key)
    session.commit()
    return key


@app.post("/api/v1/keys", include_in_schema=False)
def create_api_key(
    owner: str,
    plan: str = "free",
    daily_limit: int = 100,
    session: Session = Depends(get_session),
    x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret"),
):
    """Admin-only. Create a new API key."""
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    raw = f"aik_{'pro' if plan != 'free' else 'free'}_{_secrets.token_urlsafe(24)}"
    key = ApiKey(key=raw, owner=owner, plan=plan, daily_limit=daily_limit)
    session.add(key)
    session.commit()
    return {"key": raw, "owner": owner, "plan": plan, "daily_limit": daily_limit}


@app.get("/api/v1/bulk")
def bulk_tools(
    aids: str = Query(description="Comma-separated list of AIDs (max 20)"),
    session: Session = Depends(get_session),
    x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key"),
):
    """
    Model II — Bulk MAI-1 fetch. Requires a valid X-Api-Key.
    Returns up to 20 full MAI-1 contracts in one call.
    Saves ~96,000 tokens vs fetching one-by-one.

    Example:
      GET /api/v1/bulk?aids=langchain-v1,langgraph-v1,crewai-v1
      Header: X-Api-Key: aik_pro_xxxx
    """
    key = _validate_api_key(session, x_api_key)
    aid_list = [a.strip() for a in aids.split(",") if a.strip()][:20]
    results = []
    for aid in aid_list:
        tool = session.get(Tool, aid)
        if tool and tool.verified:
            results.append(tool_to_mai1(tool, include_action=True))
    return {
        "count": len(results),
        "plan": key.plan,
        "requests_today": key.requests_today,
        "daily_limit": key.daily_limit,
        "tools": results,
    }


# =====================================================================
# ADMIN DASHBOARD — invisible to agents, protected by token
# =====================================================================

def _classify_ua_for_dashboard(ua: str) -> str:
    u = ua.lower()
    if "cursor" in u:                              return "Cursor"
    if "claude" in u or "anthropic" in u:          return "Claude"
    if "copilot" in u or "github-copilot" in u:   return "Copilot"
    if "gptbot" in u or "chatgpt" in u or "openai" in u: return "GPT"
    if "gemini" in u or "google-extended" in u:   return "Gemini"
    if "perplexity" in u:                          return "Perplexity"
    if "mozilla" in u or "chrome" in u or "webkit" in u: return "Human"
    return "Other"


def _build_dashboard_ctx(session: Session) -> dict:
    now     = datetime.utcnow()
    s24h    = now - timedelta(hours=24)
    s7d     = now - timedelta(days=7)

    # KPIs
    total_tools     = session.exec(select(func.count(Tool.aid))).one() or 0
    verified_count  = session.exec(select(func.count(Tool.aid)).where(Tool.verified == True)).one() or 0
    failed_count    = session.exec(select(func.count(Tool.aid)).where(Tool.verified == False)).one() or 0
    pending_count   = total_tools - verified_count - failed_count
    req_24h         = session.exec(select(func.count(RequestLog.id)).where(RequestLog.timestamp >= s24h)).one() or 0
    req_7d          = session.exec(select(func.count(RequestLog.id)).where(RequestLog.timestamp >= s7d)).one() or 0
    tokens_saved    = req_7d * 500

    # Traffic timelines — fetch raw timestamps, aggregate in Python
    raw_ts = session.exec(
        select(RequestLog.timestamp)
        .where(RequestLog.timestamp >= s7d)
    ).all()

    daily_keys = [(now - timedelta(days=6 - i)).strftime("%b %d") for i in range(7)]
    daily      = {k: 0 for k in daily_keys}
    hourly_keys= [(now - timedelta(hours=23 - i)).strftime("%H:00") for i in range(24)]
    hourly     = {k: 0 for k in hourly_keys}
    for ts in raw_ts:
        dk = ts.strftime("%b %d")
        if dk in daily:
            daily[dk] += 1
        if ts >= s24h:
            hk = ts.strftime("%H:00")
            if hk in hourly:
                hourly[hk] += 1

    # Top 10 tools (7d) — exclude /instructions sub-paths
    top_rows = session.exec(
        select(RequestLog.path, func.count(RequestLog.id).label("n"))
        .where(
            RequestLog.timestamp >= s7d,
            RequestLog.path.startswith("/api/v1/tools/"),
        )
        .group_by(RequestLog.path)
        .order_by(func.count(RequestLog.id).desc())
        .limit(20)
    ).all()
    top_tools = []
    seen_aids: set = set()
    for p, n in top_rows:
        aid = p.replace("/api/v1/tools/", "").split("/")[0]
        if aid and aid not in seen_aids and not aid.endswith("instructions"):
            seen_aids.add(aid)
            top_tools.append({"aid": aid, "count": n})
        if len(top_tools) == 10:
            break

    # Agent-type breakdown — use pre-computed agent_type field (same as intel endpoint)
    agent_type_rows = session.exec(
        select(RequestLog.agent_type, func.count(RequestLog.id).label("n"))
        .where(RequestLog.timestamp >= s7d)
        .group_by(RequestLog.agent_type)
    ).all()
    agent_type_counts = {atype: n for atype, n in agent_type_rows}
    elite_count   = agent_type_counts.get("elite",   0)
    human_count   = agent_type_counts.get("human",   0)
    unknown_count = agent_type_counts.get("unknown", 0)

    # All UA strings (7d) for the visitor table + elite pie
    all_ua_rows = session.exec(
        select(RequestLog.user_agent, RequestLog.agent_type, func.count(RequestLog.id).label("n"))
        .where(RequestLog.timestamp >= s7d)
        .group_by(RequestLog.user_agent, RequestLog.agent_type)
        .order_by(func.count(RequestLog.id).desc())
        .limit(100)
    ).all()

    # Visitor intelligence table — group by named source
    _SOURCE_MAP = [
        ("Cursor",      re.compile(r"cursor",                    re.I)),
        ("Claude",      re.compile(r"claude|anthropic|claudebot",re.I)),
        ("Copilot",     re.compile(r"copilot|github-copilot",    re.I)),
        ("GPT / OpenAI",re.compile(r"gptbot|chatgpt|openai",     re.I)),
        ("Gemini",      re.compile(r"gemini|google-extended|bard",re.I)),
        ("Perplexity",  re.compile(r"perplexity",                re.I)),
        ("CCBot",       re.compile(r"ccbot",                     re.I)),
        ("Bing",        re.compile(r"bingbot|msnbot",            re.I)),
        ("Human — Chrome",  re.compile(r"chrome",               re.I)),
        ("Human — Safari",  re.compile(r"safari",               re.I)),
        ("Human — Firefox", re.compile(r"firefox",              re.I)),
        ("curl / script",   re.compile(r"curl",                 re.I)),
    ]

    source_counts: dict = defaultdict(int)
    source_samples: dict = {}
    source_type: dict   = {}
    for ua, atype, n in all_ua_rows:
        label = "Other bot"
        for name, pat in _SOURCE_MAP:
            if pat.search(ua):
                label = name
                break
        source_counts[label]  += n
        if label not in source_samples:
            source_samples[label] = ua[:80]
            source_type[label]    = atype

    visitor_table = sorted(
        [
            {
                "source":    src,
                "count":     cnt,
                "agent_type": source_type.get(src, "unknown"),
                "ua_sample": source_samples.get(src, ""),
            }
            for src, cnt in source_counts.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )

    # Elite Adoption pie — aggregate by named source for elite agent_type only
    elite_buckets: dict = defaultdict(int)
    for row in visitor_table:
        if row["agent_type"] == "elite":
            elite_buckets[row["source"]] += row["count"]
    if not elite_buckets and elite_count > 0:
        elite_buckets["Elite AI"] = elite_count
    elite_adoption = dict(elite_buckets) if elite_buckets else {"No elite agents yet": 1}

    # LLM Battleground — all agent types including human (shows full picture)
    llm_bg = {
        "Elite AI":    elite_count,
        "Human":       human_count,
        "Unknown/Bot": unknown_count,
    }

    # Health Grid — verified tools + latest sandbox check
    v_tools = session.exec(select(Tool).where(Tool.verified == True).order_by(Tool.aid)).all()
    aids    = [t.aid for t in v_tools]
    all_hcs = session.exec(
        select(HealthCheck).where(HealthCheck.aid.in_(aids)).order_by(HealthCheck.checked_at.desc())
    ).all() if aids else []

    latest_hc: dict = {}
    for hc in all_hcs:
        if hc.aid not in latest_hc:
            latest_hc[hc.aid] = hc

    health_grid = []
    for t in v_tools:
        hc = latest_hc.get(t.aid)
        if hc:
            status = "pass" if hc.sandbox_success else ("fail" if hc.sandbox_success is False else "partial")
        else:
            status = "unverified"
        health_grid.append({
            "aid":        t.aid,
            "status":     status,
            "score":      round(hc.response_integrity_score, 2) if hc and hc.response_integrity_score else None,
            "checked_at": hc.checked_at.strftime("%Y-%m-%d") if hc and hc.checked_at else "—",
            "platform":   t.source_platform,
        })

    # Agent briefing — reads from agent_logs (written by each agent script)
    _AGENT_META = {
        "B1": ("Sentinel",         "repos scanned",        "New tools discovered via FOAM scoring"),
        "B2": ("Sanitizer",        "tools verified",       "Triple-check: schema + URL + Docker"),
        "B3": ("Context Injector", "AGENTS.md generated",  "MAI-1 sections for MIT/Apache repos"),
        "B4": ("Library Ghost",    "snippets generated",   "LangChain/CrewAI issue monitoring"),
        "B5": ("Tax Analyst",      "tools analysed",       "Reliability scores & status updates"),
        "B6": ("Translator",       "tools translated",     "README → MAI-1 on-demand"),
        "B7": ("Push Agent",       "tools synced",         "Local SQLite → Railway PostgreSQL"),
    }

    recent_logs = session.exec(
        select(AgentLog).where(AgentLog.run_at >= s24h).order_by(AgentLog.run_at.desc())
    ).all()

    # Keep only the latest run per agent code in the last 24h
    latest_run: dict = {}
    for log in recent_logs:
        if log.agent_code not in latest_run:
            latest_run[log.agent_code] = log

    agent_briefing = []
    for code, (name, unit, desc) in _AGENT_META.items():
        log = latest_run.get(code)
        agent_briefing.append({
            "name":  name, "code": code,
            "count": log.items_new if log else 0,
            "processed": log.items_processed if log else 0,
            "unit":  unit, "desc": desc,
            "ran_at": log.run_at.strftime("%H:%M UTC") if log else None,
            "duration_s": log.duration_s if log else None,
            "summary": log.summary if log else None,
        })

    # Revenue tracker
    monetizable = session.exec(select(func.count(Tool.aid)).where(Tool.monetizable == True)).one() or 0
    aff_clicks  = session.exec(
        select(func.count(RequestLog.id))
        .where(RequestLog.timestamp >= s7d, RequestLog.path.startswith("/api/v1/tools/"))
    ).one() or 0
    est_conv    = round(aff_clicks * 0.02)
    est_rev     = est_conv * 10

    return {
        "now":               now.strftime("%Y-%m-%d %H:%M UTC"),
        "total_tools":       total_tools,
        "verified_count":    verified_count,
        "failed_count":      failed_count,
        "pending_count":     pending_count,
        "req_24h":           req_24h,
        "req_7d":            req_7d,
        "tokens_saved_est":  f"{tokens_saved:,}",
        "elite_count":       elite_count,
        "human_count":       human_count,
        "unknown_count":     unknown_count,
        "traffic_7d":        json.dumps({"labels": list(daily.keys()),  "data": list(daily.values())}),
        "traffic_24h":       json.dumps({"labels": list(hourly.keys()), "data": list(hourly.values())}),
        "top_tools_json":    json.dumps(top_tools),
        "elite_adoption_json": json.dumps(elite_adoption),
        "llm_bg_json":       json.dumps(llm_bg),
        "health_grid":       health_grid,
        "visitor_table":     visitor_table,
        "agent_briefing":    agent_briefing,
        "monetizable":       monetizable,
        "aff_clicks_7d":     aff_clicks,
        "est_conv_7d":       est_conv,
        "est_rev_7d":        est_rev,
    }


@app.get("/admin/dashboard", response_class=HTMLResponse, include_in_schema=False)
def admin_dashboard(
    request: Request,
    token: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    if token != ADMIN_SECRET:
        raise HTTPException(status_code=404, detail="Not Found")
    ctx = _build_dashboard_ctx(session)
    ctx["request"] = request
    response = templates.TemplateResponse("dashboard.html", ctx)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if "server" in response.headers:
        del response.headers["server"]
    return response


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
