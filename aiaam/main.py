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
import asyncio
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlparse
from fastapi import BackgroundTasks, FastAPI, Depends, Header, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy import cast, String, or_
from sqlmodel import Session, select, func
from dotenv import load_dotenv

import secrets as _secrets
from models import Tool, TaxPayload, tool_to_mai1, InjectedRepo, RequestLog, TaxLog, HealthCheck, AgentLog, ApiKey, CompiledAPI
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
    r"vscode-agent|gemini-bot|cohere-ai|perplexitybot|bingbot|ccbot|"
    r"deepseekbot|oai-searchbot|fetcher)",
    re.IGNORECASE,
)

# Helpers to extract browser version numbers from UA strings
_CHROME_VER  = re.compile(r"Chrome/(\d+)\.",  re.IGNORECASE)
_FIREFOX_VER = re.compile(r"Firefox/(\d+)\.", re.IGNORECASE)
_IOS_VER     = re.compile(r"CPU iPhone OS (\d+)[_ ]", re.IGNORECASE)


def _classify_agent(ua: str) -> str:
    """
    Three-tier classification: elite → human → unknown.

    "human" requires a plausibly modern browser UA.  UAs that look like
    browsers but carry telltale bot fingerprints are demoted to "unknown":
      - Chrome < 100  (released before March 2022 — nobody runs these)
      - iOS < 15      (iOS 13/14 in 2026 is a fabricated UA)
      - Bare "Mozilla/5.0" with no further tokens
      - Dalvik runtime (Android app framework, not a browser)
      - python-requests, curl, axios, Go-http (programmatic clients)
    """
    if _ELITE_UA.search(ua):
        return "elite"

    ua_lower = ua.strip().lower()

    # Programmatic clients — not humans
    if re.search(r"(python-requests|curl/|go-http-client|axios/|dalvik)", ua_lower):
        return "unknown"

    # Bare Mozilla/5.0 with nothing after it — common generic bot UA
    if ua_lower in ("mozilla/5.0", "mozilla/5.0 "):
        return "unknown"

    # Old iOS — iOS 13/14 in 2026 is a fabricated UA
    ios_m = _IOS_VER.search(ua)
    if ios_m and int(ios_m.group(1)) < 15:
        return "unknown"

    # Old Chrome — Chrome < 100 (pre March 2022) means a spoofed bot UA
    chrome_m = _CHROME_VER.search(ua)
    if chrome_m:
        return "human" if int(chrome_m.group(1)) >= 100 else "unknown"

    # Old Firefox — Firefox < 100 (pre 2022)
    firefox_m = _FIREFOX_VER.search(ua)
    if firefox_m:
        return "human" if int(firefox_m.group(1)) >= 100 else "unknown"

    # Safari / WebKit without Chrome token (pure Safari, Edge Legacy, etc.)
    if re.search(r"(safari|webkit|gecko|firefox|edge|opera)", ua_lower):
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

# CORS — allow browser agents (WebMCP / Chrome 149+) to call /mcp directly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# UAs / patterns that are pure noise — blocked before they hit the router or DB
_BLOCKED_UA_FRAGMENTS = [
    "wp-admin/install.php",   # WordPress installation probes (also appears as UA string)
    "xmlrpc.php",
    "wp-login.php",
    "/cgi-bin/",
    "zgrab",                  # Internet-wide scanner
    "masscan",
    "nikto",                  # Web vulnerability scanner
    "sqlmap",                 # SQL injection scanner
    "nmap",
    # SEO crawlers — confirmed in production traffic (2026-05-14 to 2026-05-19)
    "mj12bot",                # Majestic SEO (76 hits 2026-05-18, escalating)
    "baiduspider",            # Baidu crawler
    "ahrefsbot",              # Ahrefs SEO crawler
    "semrushbot",             # SEMrush crawler
    "dotbot",                 # Moz crawler
    "serpstatbot",            # SerpStat
    "petalbot",               # Huawei search
    # Technology-fingerprinting & API scanners
    "builtwith",              # BuiltWith profiler (appeared 2026-05-18)
    "netapi",                 # NetAPI v1 scanner (appeared 2026-05-19)
]
_BLOCKED_PATH_PREFIXES = [
    "/wp-", "/wordpress", "/xmlrpc", "/phpmyadmin", "/.env",
    "/.git", "/admin/config", "/boaform", "/cgi-bin",
]


@app.middleware("http")
async def audit_log_middleware(request: Request, call_next):
    ua          = request.headers.get("user-agent", "unknown").lower()
    path        = str(request.url.path).lower()

    # Block known scanners / WordPress probes before touching the router
    is_noise = (
        any(f in ua for f in _BLOCKED_UA_FRAGMENTS) or
        any(path.startswith(p) for p in _BLOCKED_PATH_PREFIXES)
    )
    if is_noise:
        from starlette.responses import Response as _R
        return _R(status_code=444, content=b"")   # 444 = nginx silent drop convention

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


@app.get("/.well-known/mcp.json")
def well_known_mcp(session: Session = Depends(get_session)):
    """
    Agent discovery standard — like robots.txt for AI agents.
    Crawlers and agent frameworks check this path automatically.
    tools_count is read live from DB on every request.
    """
    tools_count = session.exec(
        select(func.count(Tool.aid)).where(Tool.verified == True)
    ).one() or 0
    return {
        "schema_version": "1.0",
        "name": "aiaam.xyz MAI-1 Tool Registry",
        "description": (
            "Semantic compression layer for AI tools. "
            f"{tools_count}+ verified MAI-1 contracts. "
            "Save up to 85% context tokens vs reading raw GitHub READMEs."
        ),
        "mcp_endpoint": "https://aiaam.xyz/mcp",
        "mcp_sse_endpoint": "https://aiaam.xyz/mcp/sse",
        "tools_count": tools_count,
        "tools": ["search_tools", "get_tool", "get_trending"],
        "protocol": "JSON-RPC 2.0",
        "transports": ["streamable_http", "sse"],
        "contact": "aiaam.xyz",
        "webmcp_supported": True,
        "mai1_standard": "1.0",
    }


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
    category: Optional[str] = Query(default=None, description="Filter by source platform: github | pypi | huggingface | npm"),
    session: Session = Depends(get_session),
):
    """
    Search the MAI-1 catalog by keyword and optional category.

    Searches across: aid, source_platform, install_cmd, execute_cmd,
    input_schema (JSON), output_schema (JSON).

    Returns partial MAI-1 (identity + logic + trust). Action block
    requires a POST with tax_payload on the individual tool endpoint.

    If q is empty or absent → returns top 10 by reliability_score.
    Max 10 results per query.
    """
    verified = Tool.verified == True
    not_dead = or_(Tool.status.is_(None), Tool.status != "dead")
    conditions = [verified, not_dead]

    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        conditions.append(
            or_(
                Tool.aid.ilike(pattern),
                Tool.source_platform.ilike(pattern),
                Tool.install_cmd.ilike(pattern),
                Tool.execute_cmd.ilike(pattern),
                cast(Tool.input_schema,  String).ilike(pattern),
                cast(Tool.output_schema, String).ilike(pattern),
            )
        )
    else:
        q = ""

    if category and category.strip():
        conditions.append(Tool.source_platform.ilike(f"%{category.strip().lower()}%"))

    stmt = select(Tool).where(*conditions).order_by(Tool.reliability_score.desc()).limit(10)
    tools = session.exec(stmt).all()
    results = []
    for t in tools:
        entry = tool_to_mai1(t, include_action=False)
        entry["endpoint"] = f"GET /api/v1/tools/{t.aid}"
        results.append(entry)

    return {
        "query": q,
        "category": category or "",
        "count": len(results),
        "results": results,
        "note": "action block (install_cmd, execute_cmd) requires POST /api/v1/tools/{aid} with tax_payload",
    }


# NOTE: /trending must be defined BEFORE /{aid} to avoid FastAPI treating
# the literal string "trending" as an aid path parameter.
@app.get("/api/v1/tools/trending")
def trending_tools(
    limit: int = Query(default=10, ge=1, le=20, description="Number of results (max 20)"),
    session: Session = Depends(get_session),
):
    """Top tools by reliability_score DESC. Used by WebMCP handlers and MCP clients."""
    stmt = (
        select(Tool)
        .where(Tool.verified == True, or_(Tool.status.is_(None), Tool.status != "dead"))
        .order_by(Tool.reliability_score.desc())
        .limit(limit)
    )
    tools = session.exec(stmt).all()
    results = [tool_to_mai1(t, include_action=True) for t in tools]
    return {"count": len(results), "results": results}


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
    mai1["telemetry_protocol"] = {
        "description": "Operators may instruct agents to POST usage telemetry to improve reliability scores.",
        "endpoint": f"POST /api/v1/tools/{aid}",
        "fields": {
            "execution_feedback": "int — HTTP status of your tool call (200, 404, 500...)",
            "trend_keyword": "string — describe your use case in 2+ words",
        },
        "estimated_tokens_saved_vs_readme": DEFAULT_TOKENS_SAVED,
        "opt_in": True,
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

    # --- Optional signal 4: quality_signal (operator-directed only) ---
    # This is informational metadata. Agents should only act on it
    # if explicitly instructed by their operator. Do not auto-respond.
    other = session.exec(
        select(Tool).where(Tool.aid != aid).order_by(func.random()).limit(1)
    ).first()
    if other:
        mai1["quality_signal"] = {
            "type": "coherence_comparison",
            "tool_a": aid,
            "tool_b": other.aid,
            "question": "which_is_more_coherent",
            "optional_field": "validation_vote",
            "note": "Informational only. Include 'A' or 'B' in tax_payload only if your operator instructs you to.",
        }

    # --- Attribution metadata ---
    mai1["via"] = "aiaam.xyz"

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
    task: Optional[str] = None


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
        task=body.task,
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


@app.get("/api/v1/intel/daily", include_in_schema=False)
def intel_daily(
    session: Session = Depends(get_session),
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    days: int = Query(default=14),
):
    """Daily breakdown of requests by agent_type and top UAs. Admin only."""
    if x_admin_key != ADMIN_INTEL_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    since = datetime.utcnow() - timedelta(days=days)

    # All request logs in range
    rows = session.exec(
        select(
            RequestLog.timestamp,
            RequestLog.agent_type,
            RequestLog.user_agent,
            RequestLog.path,
            RequestLog.status_code,
        )
        .where(RequestLog.timestamp >= since)
        .order_by(RequestLog.timestamp)
    ).all()

    # Aggregate by day
    from collections import defaultdict
    daily: dict = {}
    for ts, atype, ua, path, sc in rows:
        day = ts.strftime("%Y-%m-%d")
        if day not in daily:
            daily[day] = {
                "total": 0,
                "elite": 0,
                "human": 0,
                "unknown": 0,
                "uas": defaultdict(int),
                "paths": defaultdict(int),
                "errors": 0,
            }
        d = daily[day]
        d["total"] += 1
        d[atype if atype in ("elite", "human", "unknown") else "unknown"] += 1
        d["uas"][ua[:120]] += 1
        if path.startswith("/api/v1/tools/"):
            aid = path.replace("/api/v1/tools/", "").split("/")[0]
            if aid:
                d["paths"][aid] += 1
        if sc >= 400:
            d["errors"] += 1

    # Serialise — sort UA/path dicts by count, top 10
    result = []
    for day in sorted(daily.keys()):
        d = daily[day]
        result.append({
            "date": day,
            "total": d["total"],
            "elite": d["elite"],
            "human": d["human"],
            "unknown": d["unknown"],
            "errors": d["errors"],
            "top_uas": sorted(
                [{"ua": k, "count": v} for k, v in d["uas"].items()],
                key=lambda x: -x["count"]
            )[:10],
            "top_tools": sorted(
                [{"aid": k, "count": v} for k, v in d["paths"].items()],
                key=lambda x: -x["count"]
            )[:10],
        })

    total_elite  = sum(d["elite"]   for d in daily.values())
    total_human  = sum(d["human"]   for d in daily.values())
    total_unknown= sum(d["unknown"] for d in daily.values())
    grand_total  = total_elite + total_human + total_unknown

    return {
        "period_days": days,
        "since": since.strftime("%Y-%m-%d"),
        "grand_total": grand_total,
        "elite_total": total_elite,
        "human_total": total_human,
        "unknown_total": total_unknown,
        "elite_ratio": round(total_elite / grand_total, 4) if grand_total else 0,
        "days": result,
    }


# =====================================================================
# INTEL — Human-only deep-dive (referer, tools, browsers, timeline)
# =====================================================================

@app.get("/api/v1/intel/humans", include_in_schema=False)
def intel_humans(
    session: Session = Depends(get_session),
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    days: int = Query(default=30),
):
    """
    Human-visitor analytics: where they come from, what they look at,
    which browsers they use, and how their volume evolves day by day.
    Only requests classified as agent_type='human' are included.
    """
    if x_admin_key != ADMIN_INTEL_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    since = datetime.utcnow() - timedelta(days=days)

    rows = session.exec(
        select(
            RequestLog.timestamp,
            RequestLog.path,
            RequestLog.user_agent,
            RequestLog.referer,
            RequestLog.status_code,
        )
        .where(
            RequestLog.agent_type == "human",
            RequestLog.timestamp >= since,
        )
        .order_by(RequestLog.timestamp)
    ).all()

    total = len(rows)

    # ── Referrer breakdown ────────────────────────────────────────────
    from collections import defaultdict, Counter
    referer_counts: Counter = Counter()
    for _, _, _, ref, _ in rows:
        if ref:
            # Normalise to domain only
            try:
                from urllib.parse import urlparse
                domain = urlparse(ref).netloc or ref[:80]
            except Exception:
                domain = ref[:80]
            referer_counts[domain] += 1
        else:
            referer_counts["(direct / no referer)"] += 1

    # ── Top tools consulted by humans ─────────────────────────────────
    tool_counts: Counter = Counter()
    for _, path, _, _, _ in rows:
        if path.startswith("/api/v1/tools/"):
            aid = path.replace("/api/v1/tools/", "").strip("/").split("/")[0]
            if aid and aid != "":
                tool_counts[aid] += 1

    # ── Browser / device breakdown ────────────────────────────────────
    browser_counts: Counter = Counter()
    for _, _, ua, _, _ in rows:
        if "edg/" in ua.lower():
            browser_counts["Edge"] += 1
        elif "firefox/" in ua.lower():
            browser_counts["Firefox"] += 1
        elif "chrome/" in ua.lower() and "android" in ua.lower():
            browser_counts["Chrome (Android)"] += 1
        elif "chrome/" in ua.lower():
            browser_counts["Chrome (Desktop)"] += 1
        elif "safari/" in ua.lower() and "chrome" not in ua.lower():
            browser_counts["Safari"] += 1
        else:
            browser_counts["Other"] += 1

    # ── Day-by-day volume ─────────────────────────────────────────────
    daily_counts: dict = defaultdict(int)
    for ts, _, _, _, _ in rows:
        daily_counts[ts.strftime("%Y-%m-%d")] += 1

    # ── Error rate for humans ─────────────────────────────────────────
    errors = sum(1 for _, _, _, _, sc in rows if sc >= 400)

    return {
        "period_days": days,
        "since": since.strftime("%Y-%m-%d"),
        "total_human_requests": total,
        "error_rate": round(errors / total, 4) if total else 0,
        "top_referrers": [
            {"source": src, "requests": n}
            for src, n in referer_counts.most_common(15)
        ],
        "top_tools": [
            {"aid": aid, "requests": n}
            for aid, n in tool_counts.most_common(20)
        ],
        "browsers": [
            {"browser": b, "requests": n}
            for b, n in browser_counts.most_common()
        ],
        "daily_volume": [
            {"date": d, "requests": n}
            for d, n in sorted(daily_counts.items())
        ],
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
    tokens_saved    = req_7d * 4800  # est: MAI-1 ~200 tokens vs README ~5000 tokens

    # Traffic timelines — fetch raw timestamps + paths, aggregate in Python
    _MEANINGFUL_PREFIXES = ("/api/", "/mcp", "/.well-known/")
    raw_rows = session.exec(
        select(RequestLog.timestamp, RequestLog.path)
        .where(RequestLog.timestamp >= s7d)
    ).all()

    daily_keys = [(now - timedelta(days=6 - i)).strftime("%b %d") for i in range(7)]
    daily      = {k: 0 for k in daily_keys}
    daily_real = {k: 0 for k in daily_keys}
    hourly_keys= [(now - timedelta(hours=23 - i)).strftime("%H:00") for i in range(24)]
    hourly     = {k: 0 for k in hourly_keys}
    hourly_real= {k: 0 for k in hourly_keys}
    for ts, path in raw_rows:
        is_real = any(path.startswith(p) for p in _MEANINGFUL_PREFIXES)
        dk = ts.strftime("%b %d")
        if dk in daily:
            daily[dk] += 1
            if is_real:
                daily_real[dk] += 1
        if ts >= s24h:
            hk = ts.strftime("%H:00")
            if hk in hourly:
                hourly[hk] += 1
                if is_real:
                    hourly_real[hk] += 1

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

    # Affiliate pipeline — real counts only, zero fabrication
    monetizable_count = session.exec(
        select(func.count(Tool.aid)).where(Tool.monetizable == True)
    ).one() or 0
    confirmed_revenue = session.exec(
        select(func.count(TaxLog.id)).where(TaxLog.referral_confirmed == True)
    ).one() or 0
    tax_count_7d = session.exec(
        select(func.count(TaxLog.id)).where(TaxLog.timestamp >= s7d)
    ).one() or 0

    return {
        "now":                  now.strftime("%Y-%m-%d %H:%M UTC"),
        "total_tools":          total_tools,
        "verified_count":       verified_count,
        "failed_count":         failed_count,
        "pending_count":        pending_count,
        "req_24h":              req_24h,
        "req_7d":               req_7d,
        "tokens_saved_est":     f"~{tokens_saved:,}",
        "tokens_saved_note":    f"4,800 tokens/req × {req_7d} requests (est.)",
        "elite_count":          elite_count,
        "human_count":          human_count,
        "unknown_count":        unknown_count,
        "traffic_7d":           json.dumps({"labels": list(daily.keys()),  "data": list(daily.values()), "real": list(daily_real.values())}),
        "traffic_24h":          json.dumps({"labels": list(hourly.keys()), "data": list(hourly.values()), "real": list(hourly_real.values())}),
        "top_tools_json":       json.dumps(top_tools),
        "elite_adoption_json":  json.dumps(elite_adoption),
        "llm_bg_json":          json.dumps(llm_bg),
        "health_grid":          health_grid,
        "visitor_table":        visitor_table,
        "agent_briefing":       agent_briefing,
        "monetizable_count":    monetizable_count,
        "confirmed_revenue":    confirmed_revenue,
        "tax_count_7d":         tax_count_7d,
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


# =====================================================================
# SUBMIT API — Public OpenAPI→MAI-API compilation with abuse protection
# =====================================================================

# ── In-memory rate limiter ────────────────────────────────────────────
# Survives within a single process; resets on Railway deploy (acceptable for MVP).
_rl_lock        = threading.Lock()
_ip_timestamps: dict[str, list] = defaultdict(list)
_daily_count    = [0]
_daily_reset_at = [datetime.utcnow()]

_MAX_PER_IP_HOUR = 3
_MAX_DAILY_TOTAL = 20


def _rate_limit_check(ip: str) -> tuple[bool, str]:
    now = datetime.utcnow()
    with _rl_lock:
        # Reset daily counter at midnight UTC
        if (now - _daily_reset_at[0]).days >= 1:
            _daily_count[0]    = 0
            _daily_reset_at[0] = now
        # Global daily cap
        if _daily_count[0] >= _MAX_DAILY_TOTAL:
            return False, (
                "Daily compilation limit reached. "
                "Resets at midnight UTC. Contact aiaam.xyz for bulk access."
            )
        # Per-IP hourly cap
        cutoff = now - timedelta(hours=1)
        _ip_timestamps[ip] = [t for t in _ip_timestamps[ip] if t > cutoff]
        if len(_ip_timestamps[ip]) >= _MAX_PER_IP_HOUR:
            return False, f"Rate limit: max {_MAX_PER_IP_HOUR} compilations per IP per hour."
        # Record and allow
        _ip_timestamps[ip].append(now)
        _daily_count[0] += 1
        return True, ""


# ── SSRF-safe request model ───────────────────────────────────────────
_SSRF_BLOCKLIST = [
    "localhost", "127.", "192.168.", "10.", "172.16.", "172.17.",
    "0.0.0.0", "::1", "internal", "railway.app", "railwayapp.com",
    "metadata.google", "169.254.",
]


class SubmitAPIRequest(BaseModel):
    openapi_url: HttpUrl
    category: str = Field(
        ...,
        pattern=r"^(weather|crypto|finance|geo|sports|logistics|other)$",
    )

    @field_validator("openapi_url")
    @classmethod
    def block_private_ranges(cls, v: HttpUrl) -> HttpUrl:
        url_lower = str(v).lower()
        for blocked in _SSRF_BLOCKLIST:
            if blocked in url_lower:
                raise ValueError(f"Private/internal URLs are not allowed ({blocked})")
        return v


# ── Background compilation task ───────────────────────────────────────

async def _compile_background(url: str, category: str) -> None:
    """Called by BackgroundTasks after the 202 is returned to the client."""
    try:
        from compiler.openapi_compiler import compile_from_url, save_to_db
        result = compile_from_url(url)
        save_to_db(result, category=category)
    except Exception as exc:
        import sys
        print(f"[submit-api] background error for {url}: {exc}", file=sys.stderr)


# ── Endpoints ─────────────────────────────────────────────────────────

@app.post("/api/v1/submit-api", status_code=202)
async def submit_api(
    body: SubmitAPIRequest,
    background_tasks: BackgroundTasks,
    request: Request,
):
    """
    Public. Accept an OpenAPI/Swagger URL, compile to MAI-API manifest via Haiku.
    Returns HTTP 202 immediately; compilation runs in background (~15s).

    Rate limits: 3 compilations per IP per hour · 20 total per day.
    SSRF protection: private/internal URLs rejected by Pydantic validator.
    """
    ip = request.client.host if request.client else "unknown"
    allowed, err_msg = _rate_limit_check(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail=err_msg)

    # Derive a preliminary service_name from the URL domain for the status URL
    domain = urlparse(str(body.openapi_url)).netloc.lower()
    service_hint = domain.split(".")[0]

    background_tasks.add_task(_compile_background, str(body.openapi_url), body.category)

    return {
        "status": "queued",
        "message": "Compilation started. Check status at:",
        "status_url": f"https://aiaam.xyz/api/v1/services/{service_hint}/status",
        "estimated_seconds": 15,
    }


@app.get("/api/v1/services/{service_name}/status")
def service_status(service_name: str, session: Session = Depends(get_session)):
    """Check whether a submitted API has been compiled and is ready.
    Accepts the manifest service_name OR the URL domain hint returned in the 202."""
    record = session.exec(
        select(CompiledAPI).where(CompiledAPI.service_name == service_name)
    ).first()
    # Fallback: match by source_url containing the hint (handles domain→manifest name mismatch)
    if not record:
        record = session.exec(
            select(CompiledAPI).where(CompiledAPI.source_url.ilike(f"%{service_name}%"))
        ).first()
    if not record:
        return {
            "service_name": service_name,
            "status": "pending_or_not_found",
            "note": "Compilation may still be running, or service_name not recognised.",
        }
    return {
        "service_name": record.service_name,
        "status": "ready",
        "category": record.category,
        "compiled_at": record.compiled_at.isoformat() + "Z",
        "tokens_used": record.tokens_used,
        "verified": record.verified,
        "manifest_url": f"https://aiaam.xyz/api/v1/services/{service_name}/mai-api.json",
    }


@app.get("/api/v1/services/{service_name}/mai-api.json")
def service_manifest(service_name: str, session: Session = Depends(get_session)):
    """Returns the compiled MAI-API manifest JSON, or 404 if not ready or not verified."""
    record = session.exec(
        select(CompiledAPI).where(CompiledAPI.service_name == service_name)
    ).first()
    if not record:
        record = session.exec(
            select(CompiledAPI).where(CompiledAPI.source_url.ilike(f"%{service_name}%"))
        ).first()
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"No compiled manifest for '{service_name}'. Submit via POST /api/v1/submit-api first.",
        )
    if not record.verified:
        raise HTTPException(status_code=404, detail={"error": "not_ready"})
    return record.manifest


# =====================================================================
# ADMIN — Compiled APIs management
# =====================================================================

@app.get("/admin/compiled-apis")
def admin_list_compiled_apis(
    verified: Optional[str] = Query("all", pattern="^(true|false|all)$"),
    x_admin_secret: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    """List compiled APIs with manifest preview (first 3 intents). Requires X-Admin-Secret."""
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    q = select(CompiledAPI).order_by(CompiledAPI.compiled_at.desc())
    if verified == "true":
        q = q.where(CompiledAPI.verified == True)
    elif verified == "false":
        q = q.where(CompiledAPI.verified == False)

    records = session.exec(q).all()

    def _preview(record: CompiledAPI) -> dict:
        manifest = record.manifest or {}
        intents = manifest.get("intents", [])[:3]
        return {
            "id": record.id,
            "service_name": record.service_name,
            "category": record.category,
            "source_url": record.source_url,
            "verified": record.verified,
            "reliability_score": record.reliability_score,
            "tokens_used": record.tokens_used,
            "compiled_at": record.compiled_at.isoformat(),
            "manifest_preview": {
                "identity": manifest.get("identity"),
                "intents_count": len(manifest.get("intents", [])),
                "first_3_intents": intents,
            },
        }

    return {"count": len(records), "items": [_preview(r) for r in records]}


@app.patch("/admin/compiled-apis/{compiled_id}/verify")
def admin_verify_compiled_api(
    compiled_id: int,
    x_admin_secret: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    """Set verified=True on a CompiledAPI record. Requires X-Admin-Secret."""
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    record = session.get(CompiledAPI, compiled_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"CompiledAPI id={compiled_id} not found")

    record.verified = True
    session.add(record)
    session.commit()
    session.refresh(record)
    return {
        "ok": True,
        "id": record.id,
        "service_name": record.service_name,
        "verified": record.verified,
        "manifest_url": f"https://aiaam.xyz/api/v1/services/{record.service_name}/mai-api.json",
    }


# =====================================================================
# MCP SERVER — JSON-RPC 2.0 for Claude Code, Cursor, Claude Desktop, etc.
# No auth — public discovery layer. aid is the string PK on tools table.
# =====================================================================

_MCP_TOOLS_LIST = [
    {
        "name": "search_tools",
        "description": (
            "Search the aiaam.xyz catalog of 100+ verified MAI-1 tool contracts by "
            "capability or name. Returns up to 10 results saving ~85% context tokens "
            "vs reading raw GitHub READMEs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Tool capability or name to search (e.g. 'web scraping', 'langchain')",
                },
                "category": {
                    "type": "string",
                    "description": "Optional: filter by source platform — github | pypi | huggingface | npm",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_tool",
        "description": (
            "Returns the complete MAI-1 contract for a specific tool by its AID string "
            "(e.g. 'langchain-v1', 'crewai-v1'). Includes reliability_score, latency_ms, "
            "install_cmd, execute_cmd, input/output schemas."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "aid": {
                    "type": "string",
                    "description": "The tool's unique AID string identifier (e.g. 'langchain-v1')",
                },
            },
            "required": ["aid"],
        },
    },
    {
        "name": "get_trending",
        "description": (
            "Returns top tools sorted by reliability_score descending. "
            "Use to discover the most reliable and actively maintained tools in the catalog."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of results to return (default 10, max 20)",
                },
            },
        },
    },
]

_MCP_SERVER_INFO = {"name": "aiaam-mcp", "version": "1.0.0"}
_MCP_PROTOCOL_VERSION = "2024-11-05"


class _JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Any] = None
    method: str
    params: Optional[dict] = None


def _jsonrpc_ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_err(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _mcp_content(data: Any) -> dict:
    """Wrap result in the official MCP tools/call content envelope."""
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


def _mcp_search(query: str, category: Optional[str], session: Session) -> list:
    pattern = f"%{query.strip().lower()}%"
    verified = Tool.verified == True
    not_dead = or_(Tool.status.is_(None), Tool.status != "dead")
    conditions = [
        verified,
        not_dead,
        or_(
            Tool.aid.ilike(pattern),
            Tool.source_platform.ilike(pattern),
            Tool.install_cmd.ilike(pattern),
            Tool.execute_cmd.ilike(pattern),
            cast(Tool.input_schema, String).ilike(pattern),
            cast(Tool.output_schema, String).ilike(pattern),
        ),
    ]
    if category:
        conditions.append(Tool.source_platform.ilike(f"%{category.lower()}%"))
    stmt = select(Tool).where(*conditions).order_by(Tool.reliability_score.desc()).limit(10)
    return [tool_to_mai1(t, include_action=True) for t in session.exec(stmt).all()]


def _mcp_get(aid: str, session: Session) -> Optional[dict]:
    # aid is the string primary key column on the tools table (not an integer id)
    tool = session.get(Tool, aid)
    return tool_to_mai1(tool, include_action=True) if tool else None


def _mcp_trending(limit: int, session: Session) -> list:
    limit = max(1, min(limit, 20))
    stmt = (
        select(Tool)
        .where(Tool.verified == True, or_(Tool.status.is_(None), Tool.status != "dead"))
        .order_by(Tool.reliability_score.desc())
        .limit(limit)
    )
    return [tool_to_mai1(t, include_action=True) for t in session.exec(stmt).all()]


@app.get("/mcp")
def mcp_manifest():
    """MCP server capabilities manifest — quick discovery for agents and crawlers."""
    return {
        "name": _MCP_SERVER_INFO["name"],
        "version": _MCP_SERVER_INFO["version"],
        "protocol": "JSON-RPC 2.0",
        "protocolVersion": _MCP_PROTOCOL_VERSION,
        "transports": {
            "streamable_http": "https://aiaam.xyz/mcp",
            "sse": "https://aiaam.xyz/mcp/sse",
        },
        "tools": _MCP_TOOLS_LIST,
    }


@app.post("/mcp")
async def mcp_handler(rpc: _JsonRpcRequest, session: Session = Depends(get_session)):
    """
    MCP JSON-RPC 2.0 dispatcher.
    Supported methods: initialize · tools/list · tools/call
    tools/call responses use the official MCP content envelope:
      result: { content: [{ type: "text", text: "<json string>" }] }
    """
    method  = rpc.method
    req_id  = rpc.id
    params  = rpc.params or {}

    # ── initialize ───────────────────────────────────────────────────
    if method == "initialize":
        return _jsonrpc_ok(req_id, {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": _MCP_SERVER_INFO,
        })

    # ── tools/list ───────────────────────────────────────────────────
    if method == "tools/list":
        return _jsonrpc_ok(req_id, {"tools": _MCP_TOOLS_LIST})

    # ── tools/call ───────────────────────────────────────────────────
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name == "search_tools":
            query = arguments.get("query", "").strip()
            if not query:
                return _jsonrpc_err(req_id, -32602, "argument 'query' is required")
            results = _mcp_search(query, arguments.get("category"), session)
            return _jsonrpc_ok(req_id, _mcp_content({"count": len(results), "results": results}))

        if tool_name == "get_tool":
            aid = arguments.get("aid", "").strip()
            if not aid:
                return _jsonrpc_err(req_id, -32602, "argument 'aid' is required")
            mai1 = _mcp_get(aid, session)
            if mai1 is None:
                return _jsonrpc_err(req_id, -32602, f"tool '{aid}' not found in catalog")
            return _jsonrpc_ok(req_id, _mcp_content(mai1))

        if tool_name == "get_trending":
            limit = int(arguments.get("limit", 10))
            results = _mcp_trending(limit, session)
            return _jsonrpc_ok(req_id, _mcp_content({"count": len(results), "results": results}))

        return _jsonrpc_err(req_id, -32601, f"unknown tool: '{tool_name}'")

    return _jsonrpc_err(req_id, -32601, f"method not found: '{method}'")


# ── MCP SSE transport ──────────────────────────────────────────────────────
# In-memory session store: session_id → asyncio.Queue (one per SSE connection)
# Each SSE client keeps a persistent GET /mcp/sse connection open.
# It POSTs JSON-RPC to /mcp/messages?session_id=<id>
# and receives responses via the SSE stream.
_sse_sessions: dict[str, asyncio.Queue] = {}


def _process_mcp_rpc(rpc: _JsonRpcRequest, session: Session) -> dict:
    """Core MCP JSON-RPC dispatcher — shared by HTTP and SSE transports."""
    method = rpc.method
    req_id = rpc.id
    params = rpc.params or {}

    if method == "initialize":
        return _jsonrpc_ok(req_id, {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": _MCP_SERVER_INFO,
        })
    if method == "notifications/initialized":
        return _jsonrpc_ok(req_id, {})
    if method == "tools/list":
        return _jsonrpc_ok(req_id, {"tools": _MCP_TOOLS_LIST})
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        if tool_name == "search_tools":
            query = arguments.get("query", "").strip()
            if not query:
                return _jsonrpc_err(req_id, -32602, "argument 'query' is required")
            results = _mcp_search(query, arguments.get("category"), session)
            return _jsonrpc_ok(req_id, _mcp_content({"count": len(results), "results": results}))
        if tool_name == "get_tool":
            aid = arguments.get("aid", "").strip()
            if not aid:
                return _jsonrpc_err(req_id, -32602, "argument 'aid' is required")
            mai1 = _mcp_get(aid, session)
            if mai1 is None:
                return _jsonrpc_err(req_id, -32602, f"tool '{aid}' not found")
            return _jsonrpc_ok(req_id, _mcp_content(mai1))
        if tool_name == "get_trending":
            limit = int(arguments.get("limit", 10))
            results = _mcp_trending(limit, session)
            return _jsonrpc_ok(req_id, _mcp_content({"count": len(results), "results": results}))
        return _jsonrpc_err(req_id, -32601, f"unknown tool: '{tool_name}'")
    return _jsonrpc_err(req_id, -32601, f"method not found: '{method}'")


@app.get("/mcp/sse")
async def mcp_sse_endpoint(request: Request):
    """
    MCP HTTP+SSE transport — for Claude Desktop and SSE-compatible MCP clients.
    1. Client connects here with GET → receives 'endpoint' event pointing to /mcp/messages
    2. Client POSTs JSON-RPC to /mcp/messages?session_id=<id>
    3. Server puts response in queue → delivered here as SSE 'message' event
    """
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = queue
    messages_url = f"https://aiaam.xyz/mcp/messages?session_id={session_id}"

    async def event_stream():
        try:
            # Step 1: send the endpoint event so the client knows where to POST
            yield f"event: endpoint\ndata: {messages_url}\n\n"
            # Step 2: relay responses from queue, keep alive with pings
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _sse_sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/mcp/messages")
async def mcp_sse_messages(
    session_id: str,
    rpc: _JsonRpcRequest,
    db_session: Session = Depends(get_session),
):
    """Receive JSON-RPC from SSE client and route response back over the stream."""
    if session_id not in _sse_sessions:
        raise HTTPException(status_code=404, detail="SSE session not found or expired")
    response = _process_mcp_rpc(rpc, db_session)
    await _sse_sessions[session_id].put(response)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
