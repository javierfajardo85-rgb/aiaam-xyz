"""
AIAAM Main API
The 6 endpoints that make AIAAM work:

  GET  /                           → LLMO bait (HTML for crawlers)
  GET  /api/v1/tools               → Search catalog by keyword (?q=...)
  GET  /api/v1/tools/{aid}         → First visit, no history, returns full MAI-1
  POST /api/v1/tools/{aid}         → With tax_payload, validates and returns MAI-1
  POST /api/v1/translate           → Admin: translate a URL into MAI-1 and save to DB
  GET  /admin/stats                → Telemetry dashboard (protected)
"""
import os
import time
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Depends, Header, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import cast, String, or_
from sqlmodel import Session, select
from dotenv import load_dotenv

from models import Tool, TaxPayload, tool_to_mai1
from database import init_db, get_session
from analytics import log_transaction, get_stats, DEFAULT_TOKENS_SAVED
from translator import translate_and_save

load_dotenv()

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "change-this")

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


@app.on_event("startup")
def on_startup():
    init_db()


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
    if q and q.strip():
        pattern = f"%{q.strip().lower()}%"
        stmt = (
            select(Tool)
            .where(
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
        stmt = select(Tool).order_by(Tool.reliability_score.desc()).limit(10)

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
                "micro_translation": "string (optional, if catalog incomplete)",
                "referral_included": "bool (optional)",
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

    elapsed_ms = int((time.time() - start) * 1000)
    log_transaction(
        session=session,
        tool_aid=aid,
        user_agent=user_agent or "unknown",
        payload=payload,
        response_status=200,
        latency_ms=elapsed_ms,
    )
    return mai1


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

    tool = translate_and_save(source_url, session)
    if tool is None:
        raise HTTPException(
            status_code=422,
            detail=f"Translation failed for: {source_url}. Check logs.",
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
