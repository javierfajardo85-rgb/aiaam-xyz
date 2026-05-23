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
from sqlalchemy import cast, String, or_, text as sa_text
from sqlmodel import Session, select, func
from dotenv import load_dotenv

import secrets as _secrets
from models import Tool, TaxPayload, tool_to_mai1, InjectedRepo, RequestLog, TaxLog, HealthCheck, AgentLog, ApiKey, CompiledAPI, SearchLog
from database import init_db, get_session, engine
from analytics import log_transaction, get_stats, DEFAULT_TOKENS_SAVED, check_monetization_ratio
from translator import translate_and_save, fetch_github_readme, translate
from analytics import recalculate_from_votes

load_dotenv()

ADMIN_SECRET   = os.getenv("ADMIN_SECRET", "change-this")
ADMIN_INTEL_KEY = os.getenv("ADMIN_INTEL_KEY", ADMIN_SECRET)

# ── Agent classifier ──────────────────────────────────────────────────
# ── 5-tier UA classifier ─────────────────────────────────────────────────
# Tier 1: Elite — genuine AI coding agents making programmatic API calls
_ELITE_UA = re.compile(
    r"(github-copilot|cursor[\s/]|claude-code|windsurf|aider[\s/]|"
    r"vscode-agent|openai-agent|deepseek-agent|oai-searchbot)",
    re.IGNORECASE,
)

# Tier 2: AI Crawlers — AI companies indexing the web (GOOD: means we get into their indexes)
_AI_CRAWLER_UA = re.compile(
    r"(gptbot|chatgpt-user|oai-searchbot|perplexitybot|claudebot|"
    r"anthropic-ai|claude-web|cohere-ai|gemini-bot|google-extended|"
    r"ccbot|youbot|metaexternalagent|diffbot|bytespider)",
    re.IGNORECASE,
)

# Tier 3: SEO / web crawlers — neutral, just indexing
_SEO_CRAWLER_UA = re.compile(
    r"(mj12bot|ahrefsbot|semrushbot|dotbot|yandexbot|bingbot|msnbot|"
    r"slurp|duckduckbot|baiduspider|rogerbot|exabot|facebot|ia_archiver|"
    r"scrapy|python-requests|curl/|go-http-client|axios/|java/|dalvik)",
    re.IGNORECASE,
)

# Helpers to extract browser version numbers from UA strings
_CHROME_VER  = re.compile(r"Chrome/(\d+)\.",  re.IGNORECASE)
_FIREFOX_VER = re.compile(r"Firefox/(\d+)\.", re.IGNORECASE)
_IOS_VER     = re.compile(r"CPU iPhone OS (\d+)[_ ]", re.IGNORECASE)


def _classify_agent(ua: str) -> str:
    """
    5-tier classification: elite → ai_crawler → seo_crawler → human → unknown.

    elite       — AI coding agents making programmatic API calls (the holy grail)
    ai_crawler  — AI companies indexing our content (GPTbot, Perplexitybot = good signal)
    seo_crawler — SEO/web crawlers, neutral
    human       — plausible real browser (Chrome/Firefox ≥ 100, modern iOS)
    unknown     — fake/old UAs, unrecognised bots
    """
    if _ELITE_UA.search(ua):
        return "elite"
    if _AI_CRAWLER_UA.search(ua):
        return "ai_crawler"
    if _SEO_CRAWLER_UA.search(ua):
        return "seo_crawler"

    ua_lower = ua.strip().lower()

    # Bare Mozilla/5.0 with nothing after it — common generic bot UA
    if ua_lower in ("mozilla/5.0", "mozilla/5.0 "):
        return "unknown"

    # Old iOS — iOS 13/14 in 2026 is a fabricated UA
    ios_m = _IOS_VER.search(ua)
    if ios_m and int(ios_m.group(1)) < 15:
        return "unknown"

    # Old Chrome — Chrome < 100 (pre March 2022) = spoofed bot UA
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
# TAGS — static capability keyword inference (zero LLM)
# =====================================================================

# Each entry: (list_of_substrings_to_match_in_aid_or_install_cmd, tags_string)
# Match is case-insensitive substring; first match wins per group (multiple can match).
_TAG_RULES: list[tuple[list[str], str]] = [
    # Web scraping / crawling
    (["scrapy", "beautifulsoup", "bs4", "mechanize", "crawl4ai"], "web scraping crawler html parsing data extraction"),
    (["playwright", "selenium", "pyppeteer", "splash"],           "web scraping browser automation testing headless"),
    # Audio / speech
    (["whisper", "speechbrain", "deepspeech", "nemo"],            "audio transcription speech to text stt voice recognition"),
    # Email
    (["sendgrid", "mailgun", "postmark", "resend", "mailchimp"],  "email send emails transactional smtp notification"),
    (["smtplib", "yagmail", "emails"],                             "email send emails smtp"),
    # SMS / communication
    (["twilio", "vonage", "nexmo", "messagebird"],                "sms messaging communication telephony"),
    # Vector databases / embeddings
    (["chromadb", "chroma"],                                       "vector database embeddings semantic search similarity"),
    (["pinecone"],                                                  "vector database embeddings semantic search similarity"),
    (["weaviate"],                                                  "vector database embeddings semantic search similarity"),
    (["qdrant"],                                                    "vector database embeddings semantic search similarity"),
    (["pymilvus", "milvus"],                                       "vector database embeddings similarity search"),
    (["faiss", "annoy", "hnswlib", "usearch"],                    "vector database embeddings similarity search"),
    # LLM frameworks / agents
    (["langchain"],                                                 "llm framework language model agents orchestration rag"),
    (["crewai"],                                                    "llm framework agents multi-agent orchestration"),
    (["llama-index", "llama_index", "llamaindex"],                 "llm framework rag retrieval augmented generation"),
    (["haystack"],                                                  "llm framework rag question answering nlp pipeline"),
    (["autogen", "auto-gen"],                                      "llm framework agents multi-agent"),
    (["smolagents"],                                               "llm framework agents tools huggingface"),
    (["langgraph"],                                                 "llm framework agents graph state machine"),
    (["letta"],                                                     "llm framework agents memory stateful"),
    (["swarm"],                                                     "llm framework agents multi-agent openai"),
    (["pydantic-ai"],                                               "llm framework agents type-safe"),
    # LLM observability
    (["langfuse", "langsmith", "phoenix", "arize", "braintrust"], "llm observability tracing monitoring evaluation"),
    (["logfire", "opentelemetry"],                                 "observability tracing logging monitoring"),
    # LLM APIs
    (["openai"],                                                    "llm api gpt language model completions chatgpt"),
    (["anthropic"],                                                 "llm api claude language model completions"),
    (["lmstudio"],                                                  "llm local inference server"),
    # ML / deep learning
    (["transformers", "huggingface_hub", "huggingface-hub"],      "machine learning models transformers nlp huggingface"),
    (["torch", "pytorch"],                                         "deep learning neural network training gpu"),
    (["tensorflow", "keras"],                                      "deep learning neural network training"),
    (["diffusers", "stable-audio", "stable-diffusion"],           "image generation ai art diffusion generative"),
    # Data
    (["pandas"],                                                    "data analysis dataframe tabular csv"),
    (["polars"],                                                    "data analysis dataframe fast tabular"),
    (["numpy", "scipy"],                                           "numerical computation array math scientific"),
    (["arrow", "pyarrow"],                                         "data format columnar analytics parquet"),
    (["dbt"],                                                       "data transformation sql analytics warehouse"),
    # Web frameworks / servers
    (["fastapi"],                                                   "web api rest server async python"),
    (["flask"],                                                     "web api rest server python"),
    (["django"],                                                    "web framework rest api python"),
    (["uvicorn", "gunicorn", "hypercorn"],                        "web server asgi wsgi deployment"),
    # Databases
    (["redis", "redis-py"],                                        "cache database key-value store pub-sub messaging"),
    (["mongodb", "pymongo", "motor"],                              "database nosql document store"),
    (["sqlalchemy", "alembic"],                                    "database sql orm relational migration"),
    (["supabase"],                                                  "database sql postgres backend realtime"),
    # Task queues / scheduling
    (["celery"],                                                    "task queue background jobs async distributed"),
    (["apscheduler"],                                               "task scheduling cron jobs background"),
    (["airflow"],                                                   "workflow orchestration dag pipeline scheduling"),
    (["prefect", "dagster"],                                       "workflow orchestration pipeline data"),
    # Payments
    (["stripe"],                                                    "payments billing subscriptions checkout"),
    # DevTools
    (["scrapy", "pytest"],                                         "testing unit test framework"),
    (["black", "ruff", "flake8", "mypy"],                         "code formatting linting static analysis"),
    (["docker", "docker-py"],                                      "containerization deployment devops"),
    (["github", "pygithub", "gitpython"],                         "version control code repository devtools"),
    # Communication / productivity
    (["slack", "slack-sdk"],                                       "communication messaging team chat"),
    (["notion"],                                                    "productivity notes documentation database"),
    (["jira", "atlassian"],                                        "project management issue tracking agile"),
    # Media / social
    (["yt-dlp", "pytube", "youtube"],                             "video download streaming media youtube"),
    (["spotify"],                                                   "music audio streaming media"),
    (["tweepy", "twython"],                                        "social media api twitter"),
    # Memory / agent infra
    (["mem0"],                                                      "agent memory storage retrieval"),
    # HTTP / networking
    (["httpx", "aiohttp"],                                         "http client api requests async networking"),
    (["requests"],                                                  "http client api requests networking"),
    # Config / env
    (["python-dotenv", "dotenv"],                                  "configuration environment variables secrets"),
    # Data validation
    (["pydantic"],                                                  "data validation schema parsing models"),
]


def _infer_tags(tool) -> str:
    """
    Derive capability tags from aid + install_cmd + source_url.
    Returns a space-separated string of keywords. Zero LLM.
    """
    haystack = " ".join(filter(None, [
        tool.aid or "",
        tool.install_cmd or "",
        tool.source_url or "",
        tool.task or "",
    ])).lower()

    matched: list[str] = []
    for patterns, tag_string in _TAG_RULES:
        if any(p in haystack for p in patterns):
            matched.append(tag_string)

    return " ".join(matched) if matched else ""


# =====================================================================
# SEARCH HELPERS
# =====================================================================

def _sanitize_query(q: str) -> str:
    """
    Clean a search query before processing.
    Agents sometimes pass multi-line strings (e.g. "send email\n\nresponse")
    when they concatenate prompt text with the query. We take only the first
    non-empty line and strip surrounding whitespace + punctuation noise.
    Max 120 chars to prevent abuse.
    """
    # Take only the first non-empty line
    first_line = next((l.strip() for l in q.splitlines() if l.strip()), "")
    # Strip common trailing noise chars
    first_line = first_line.rstrip(".:,;!?/\\")
    # Collapse internal whitespace
    first_line = " ".join(first_line.split())
    # Hard cap
    return first_line[:120]


def _tool_search_clause(q: str):
    """
    Build a SQLAlchemy WHERE clause for a free-text Tool query.
    Single word → one OR across all fields.
    Multi-word  → each word must match somewhere (AND of ORs = all words present).
    This makes "llm orchestration" match tools that have both "llm" AND
    "orchestration" somewhere in their fields, even if not adjacent.
    """
    words = q.strip().lower().split()
    if not words:
        return None

    def _word_or(w: str):
        p = f"%{w}%"
        return or_(
            Tool.aid.ilike(p),
            Tool.tags.ilike(p),
            Tool.task.ilike(p),
            Tool.install_cmd.ilike(p),
            Tool.execute_cmd.ilike(p),
            Tool.source_platform.ilike(p),
            cast(Tool.input_schema,  String).ilike(p),
            cast(Tool.output_schema, String).ilike(p),
        )

    if len(words) == 1:
        return _word_or(words[0])

    # AND: every word must appear somewhere
    from sqlalchemy import and_ as sa_and
    return sa_and(*[_word_or(w) for w in words])


def _api_search_clause(q: str):
    """Same multi-word logic for CompiledAPI."""
    from models import CompiledAPI as _CA
    words = q.strip().lower().split()
    if not words:
        return None

    def _word_or(w: str):
        p = f"%{w}%"
        return or_(
            _CA.service_name.ilike(p),
            _CA.category.ilike(p),
            _CA.tags.ilike(p),
            cast(_CA.manifest, String).ilike(p),
        )

    if len(words) == 1:
        return _word_or(words[0])

    from sqlalchemy import and_ as sa_and
    return sa_and(*[_word_or(w) for w in words])


def _log_search_bg(
    query: str,
    catalog: str,
    results_count: int,
    result_aids: list,
    user_agent: str,
    raw_ip: str,
) -> None:
    """
    Persist one SearchLog row. Called via BackgroundTasks — never blocks response.
    Stores SHA-256 prefix of IP, never the raw address.
    """
    import hashlib
    ip_hash = hashlib.sha256((raw_ip or "").encode()).hexdigest()[:16]
    try:
        with Session(engine) as session:
            session.add(SearchLog(
                query=query.strip()[:500],
                catalog=catalog,
                results_count=results_count,
                result_aids=result_aids[:20],          # cap at 20 aids
                user_agent=(user_agent or "")[:512],
                ip_hash=ip_hash,
            ))
            session.commit()
    except Exception:
        pass  # logging must never break the response


# =====================================================================
# API TAGS — capability keyword inference for compiled_apis (zero LLM)
# =====================================================================

_API_TAG_RULES: list[tuple[list[str], str]] = [
    # Payments / finance
    (["stripe"],                                                   "payments billing subscriptions checkout"),
    (["brex", "plaid", "square", "adyen", "xero"],                "payments finance billing invoicing"),
    # Email
    (["sendgrid"],                                                  "email send emails transactional smtp notification"),
    (["mailgun", "postmark", "resend"],                            "email send emails transactional smtp"),
    # Messaging / communication
    (["twilio"],                                                    "sms messaging telephony communication voice"),
    (["slack"],                                                     "messaging team chat communication collaboration"),
    (["telegram", "whatsapp"],                                     "messaging chat communication mobile"),
    (["zoom"],                                                      "video conferencing communication meetings"),
    # Dev tools
    (["github"],                                                    "devtools version control code repository ci cd"),
    (["vercel", "netlify"],                                        "devtools deployment hosting frontend"),
    (["digitalocean"],                                             "devtools cloud infrastructure deployment"),
    (["circleci", "snyk"],                                         "devtools ci cd security testing"),
    # Google
    (["gmail"],                                                     "email google productivity"),
    (["google_calendar", "calendar_api"],                          "calendar scheduling google productivity"),
    (["google_drive", "drive_api"],                               "file storage cloud google productivity"),
    (["google_sheets", "sheets"],                                  "spreadsheet data analytics google"),
    (["youtube"],                                                   "video streaming media google"),
    (["firebase"],                                                  "database backend realtime google cloud"),
    # Productivity
    (["notion"],                                                    "productivity notes documentation wiki"),
    (["jira"],                                                      "project management issue tracking agile"),
    (["asana", "trello", "clickup"],                               "project management tasks productivity"),
    # AI / LLM
    (["openai_api", "openai"],                                     "llm api gpt language model completions ai"),
    # Media / social
    (["spotify"],                                                   "music audio streaming media"),
    (["twitter"],                                                   "social media api twitter"),
    (["giphy"],                                                     "media images gif"),
    (["medium"],                                                    "blogging content publishing"),
    (["ebay"],                                                      "ecommerce marketplace shopping"),
    # Security / identity
    (["okta"],                                                      "authentication security identity sso"),
    (["1password"],                                                 "secrets security password management"),
]


def _infer_api_tags(api) -> str:
    """
    Derive capability tags for a CompiledAPI from service_name + category + manifest.
    Zero LLM. Returns space-separated keywords.
    """
    manifest = api.manifest or {}
    haystack = " ".join(filter(None, [
        api.service_name or "",
        api.category or "",
        manifest.get("service", ""),
        # Include intent ids as additional signal
        " ".join(i.get("id", "") for i in manifest.get("intents", [])[:10]),
    ])).lower()

    matched: list[str] = []
    for patterns, tag_string in _API_TAG_RULES:
        if any(p in haystack for p in patterns):
            matched.append(tag_string)

    # Always include the category itself as a tag
    if api.category and api.category not in " ".join(matched):
        matched.append(api.category)

    return " ".join(matched) if matched else api.category or ""


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
    _migrate_add_tags_column()
    _backfill_tags()
    _migrate_compiled_apis_tags_column()
    _backfill_api_tags()


def _migrate_add_tags_column():
    """Idempotent: add `tags` TEXT column to tools if it doesn't exist."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE tools ADD COLUMN IF NOT EXISTS tags TEXT"
            ))
            conn.commit()
    except Exception:
        pass  # SQLite <3.37 fallback — column may already exist, ignore


def _backfill_tags():
    """Fill tags for tools that have none. Runs at startup, safe to call repeatedly."""
    with Session(engine) as session:
        tools = session.exec(
            select(Tool).where(or_(Tool.tags.is_(None), Tool.tags == ""))
        ).all()
        if not tools:
            return
        updated = 0
        for tool in tools:
            tags = _infer_tags(tool)
            if tags:
                tool.tags = tags
                session.add(tool)
                updated += 1
        if updated:
            session.commit()
            print(f"[startup] backfilled tags for {updated} tools")


def _migrate_compiled_apis_tags_column():
    """Idempotent: add `tags TEXT` column to compiled_apis if missing."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            conn.execute(text(
                "ALTER TABLE compiled_apis ADD COLUMN IF NOT EXISTS tags TEXT"
            ))
            conn.commit()
    except Exception:
        pass


def _backfill_api_tags():
    """Fill tags for compiled_apis that have none. Safe to call repeatedly."""
    from models import CompiledAPI
    with Session(engine) as session:
        apis = session.exec(
            select(CompiledAPI).where(or_(CompiledAPI.tags.is_(None), CompiledAPI.tags == ""))
        ).all()
        if not apis:
            return
        updated = 0
        for api in apis:
            tags = _infer_api_tags(api)
            if tags:
                api.tags = tags
                session.add(api)
                updated += 1
        if updated:
            session.commit()
            print(f"[startup] backfilled api tags for {updated} compiled_apis")


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
    """Extrae el aid del tool B del quality_signal enviado en el GET previo."""
    req = mai1.get("quality_signal")
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


@app.get("/llmo-apis", response_class=HTMLResponse)
def llmo_apis(request: Request, session: Session = Depends(get_session)):
    """
    Machine-readable index of MAI-API manifests — plain HTML, no CSS.
    Equivalent of llmo.html but for compiled API manifests.
    Designed for AI crawlers, coding agents, LLM scrapers.
    """
    records = session.exec(
        select(CompiledAPI)
        .where(CompiledAPI.verified == True)
        .order_by(CompiledAPI.service_name)
    ).all()

    from collections import defaultdict
    categories: dict = defaultdict(list)
    for r in records:
        manifest = r.manifest or {}
        intents = manifest.get("intents", [])
        auth = manifest.get("auth", {})
        categories[r.category].append({
            "service_name": r.service_name,
            "display_name": manifest.get("service", r.service_name),
            "intents_count": len(intents),
            "auth_type": auth.get("type", "unknown"),
        })

    sorted_categories = {
        k: sorted(v, key=lambda x: x["display_name"].lower())
        for k, v in sorted(categories.items())
    }

    return templates.TemplateResponse(
        "llmo_apis.html",
        {
            "request": request,
            "total": len(records),
            "categories": sorted_categories,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        },
    )


@app.get("/mai-api", response_class=HTMLResponse)
def mai_api_page(request: Request, session: Session = Depends(get_session)):
    """
    Public landing page for MAI-API — lists compiled manifests by category.
    Honest attribution: specs sourced from APIs.guru (CC0), no vendor affiliation.
    """
    records = session.exec(
        select(CompiledAPI)
        .where(CompiledAPI.verified == True)
        .order_by(CompiledAPI.service_name)
    ).all()

    # Group by category, build display-friendly entries
    from collections import defaultdict
    categories: dict = defaultdict(list)
    for r in records:
        intents_count = len(r.manifest.get("intents", [])) if r.manifest else 0
        display_name = r.manifest.get("service", r.service_name) if r.manifest else r.service_name
        categories[r.category].append({
            "service_name": r.service_name,
            "display_name": display_name,
            "intents_count": intents_count,
        })

    # Sort categories alphabetically, items within each by display_name
    sorted_categories = {
        k: sorted(v, key=lambda x: x["display_name"].lower())
        for k, v in sorted(categories.items())
    }

    return templates.TemplateResponse(
        "mai_api.html",
        {
            "request": request,
            "total": len(records),
            "categories": sorted_categories,
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
        "tools": ["search_tools", "get_tool", "get_trending", "get_api_manifest", "compile_api"],
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
    request: Request = None,
    background_tasks: BackgroundTasks = None,
    session: Session = Depends(get_session),
):
    """
    Search the MAI-1 catalog by keyword or intent phrase and optional category.

    Searches across: aid, task, tags (capability keywords), install_cmd,
    execute_cmd, source_platform, input_schema, output_schema.

    Intent examples: "web scraping", "send emails", "vector database",
    "audio transcription", "llm framework".

    If q is empty or absent → returns top 10 by reliability_score.
    Max 10 results per query.
    """
    verified = Tool.verified == True
    not_dead = or_(Tool.status.is_(None), Tool.status != "dead")
    conditions = [verified, not_dead]

    q = _sanitize_query(q) if q else ""
    if q:
        clause = _tool_search_clause(q)
        if clause is not None:
            conditions.append(clause)

    if category and category.strip():
        conditions.append(Tool.source_platform.ilike(f"%{category.strip().lower()}%"))

    stmt = (
        select(Tool)
        .where(*conditions)
        .order_by(Tool.sponsored.desc(), Tool.reliability_score.desc())
        .limit(10)
    )
    tools = session.exec(stmt).all()
    results = []
    for t in tools:
        entry = tool_to_mai1(t, include_action=False)
        entry["endpoint"] = f"GET /api/v1/tools/{t.aid}"
        if t.sponsored:
            entry["sponsored"] = True
        results.append(entry)

    if q and background_tasks and request:
        background_tasks.add_task(
            _log_search_bg,
            query=q,
            catalog="mai1",
            results_count=len(results),
            result_aids=[r["identity"]["aid"] for r in results],
            user_agent=request.headers.get("user-agent", ""),
            raw_ip=request.client.host if request.client else "",
        )

    return {
        "query": q,
        "category": category or "",
        "count": len(results),
        "results": results,
        "note": "action block (install_cmd, execute_cmd) requires POST /api/v1/tools/{aid} with tax_payload",
    }


@app.get("/api/v1/services/search")
def search_apis(
    q: str = Query(..., description="Intent phrase, e.g. 'send emails', 'payments'"),
    request: Request = None,
    background_tasks: BackgroundTasks = None,
    session: Session = Depends(get_session),
):
    """
    Search MAI-API manifests only. Returns compiled API manifests matching the intent.
    For a unified search across both catalogs use GET /api/v1/search.
    """
    from models import CompiledAPI
    q = _sanitize_query(q)
    api_clause = _api_search_clause(q)
    api_where = [CompiledAPI.verified == True]
    if api_clause is not None:
        api_where.append(api_clause)
    api_stmt = (
        select(CompiledAPI)
        .where(*api_where)
        .order_by(CompiledAPI.reliability_score.desc())
        .limit(10)
    )
    results = []
    for api in session.exec(api_stmt).all():
        manifest = api.manifest or {}
        results.append({
            "type":         "api",
            "service_name": api.service_name,
            "category":     api.category,
            "base_url":     manifest.get("base_url", ""),
            "auth_type":    (manifest.get("auth") or {}).get("type", "unknown"),
            "intents_count": len(manifest.get("intents", [])),
            "manifest_url": f"https://aiaam.xyz/api/v1/services/{api.service_name}/mai-api.json",
            "trust": {"reliability_score": api.reliability_score},
        })

    if background_tasks and request:
        background_tasks.add_task(
            _log_search_bg,
            query=q,
            catalog="mai_api",
            results_count=len(results),
            result_aids=[r["service_name"] for r in results],
            user_agent=request.headers.get("user-agent", ""),
            raw_ip=request.client.host if request.client else "",
        )

    return {"query": q, "count": len(results), "results": results}


@app.get("/api/v1/search")
def unified_search(
    q: str = Query(..., description="Intent phrase, e.g. 'audio transcription', 'vector database'"),
    request: Request = None,
    background_tasks: BackgroundTasks = None,
    session: Session = Depends(get_session),
):
    """
    Unified search across both catalogs:
      - MAI-1 tools  (installable libraries: pip, npm, GitHub)
      - MAI-API manifests (web APIs: Stripe, Slack, GitHub API, …)

    Each result is tagged with "type": "tool" or "type": "api".
    Tools are ranked by reliability_score; APIs by reliability_score.
    Max 5 results per catalog (10 total).

    Examples:
      ?q=audio+transcription  → whisper, whisperx (tools)
      ?q=send+emails          → sendgrid (tool + api)
      ?q=vector+database      → chroma, pinecone, weaviate (tools)
      ?q=llm+orchestration    → langchain, crewai (tools)
      ?q=task+queue           → celery (tool)
    """
    from models import CompiledAPI
    q = _sanitize_query(q)
    results: list[dict] = []

    # --- MAI-1 tools ---
    not_dead = or_(Tool.status.is_(None), Tool.status != "dead")
    tool_clause = _tool_search_clause(q)
    tool_where  = [Tool.verified == True, not_dead]
    if tool_clause is not None:
        tool_where.append(tool_clause)
    tool_stmt = (
        select(Tool)
        .where(*tool_where)
        .order_by(Tool.sponsored.desc(), Tool.reliability_score.desc())
        .limit(5)
    )
    for t in session.exec(tool_stmt).all():
        entry = tool_to_mai1(t, include_action=False)
        entry["type"]     = "tool"
        entry["endpoint"] = f"GET /api/v1/tools/{t.aid}"
        results.append(entry)

    # --- MAI-API manifests ---
    api_clause = _api_search_clause(q)
    api_where  = [CompiledAPI.verified == True]
    if api_clause is not None:
        api_where.append(api_clause)
    api_stmt = (
        select(CompiledAPI)
        .where(*api_where)
        .order_by(CompiledAPI.reliability_score.desc())
        .limit(5)
    )
    for api in session.exec(api_stmt).all():
        manifest = api.manifest or {}
        results.append({
            "type":         "api",
            "service_name": api.service_name,
            "category":     api.category,
            "base_url":     manifest.get("base_url", ""),
            "auth_type":    (manifest.get("auth") or {}).get("type", "unknown"),
            "intents_count": len(manifest.get("intents", [])),
            "manifest_url": f"https://aiaam.xyz/api/v1/services/{api.service_name}/mai-api.json",
            "trust": {"reliability_score": api.reliability_score},
        })

    if background_tasks and request:
        background_tasks.add_task(
            _log_search_bg,
            query=q,
            catalog="both",
            results_count=len(results),
            result_aids=[
                r.get("identity", {}).get("aid") or r.get("service_name")
                for r in results
            ],
            user_agent=request.headers.get("user-agent", ""),
            raw_ip=request.client.host if request.client else "",
        )

    return {
        "query":   q,
        "total":   len(results),
        "tools":   sum(1 for r in results if r["type"] == "tool"),
        "apis":    sum(1 for r in results if r["type"] == "api"),
        "results": results,
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
    Fetch a full MAI-1 contract by tool ID.
    Returns identity + logic + trust + action blocks.
    Optionally includes telemetry_protocol and quality_signal metadata
    for operators who choose to participate.
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
            "type": "execution_outcome",
            "question": "did_this_tool_solve_your_task",
            "optional_field": "validation_vote",
            "valid_values": ["yes", "no", "partial"],
            "note": "Optional. Include only if you have direct execution feedback on this tool.",
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
        mai1["telemetry_protocol"] = {
            "description": "Operators may POST usage telemetry to unlock the action block and improve reliability scores.",
            "endpoint": f"POST /api/v1/tools/{aid}",
            "required_fields": {
                "execution_feedback": "int — HTTP status of your tool call (200, 404, 500...)",
                "trend_keyword": "string — describe your use case in 2+ words",
            },
            "optional_fields": {
                "micro_translation": "string — fill a missing install_cmd or execute_cmd",
                "validation_vote": "string 'A'|'B' — operator-instructed quality signal only",
            },
            "opt_in": True,
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

    # Attribution metadata
    mai1["via"] = "aiaam.xyz"

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
    tool.tags = _infer_tags(tool)
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




@app.get("/admin/search-trends")
def search_trends(
    session: Session = Depends(get_session),
    x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret"),
):
    """
    Search query analytics — drives catalog expansion decisions.
    The zero-results bucket shows what agents need that we don't have yet.
    Protected: X-Admin-Secret required.
    Returns empty arrays gracefully when the table is empty or just created.
    """
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    empty = {
        "period": "last_7_days",
        "total_searches_7d": 0,
        "top_queries": [],
        "zero_result_queries": [],
        "queries_per_hour_24h": [],
        "unique_user_agents": [],
    }

    try:
        from sqlalchemy import func as sa_func, desc as sa_desc, asc as sa_asc, label

        cutoff_7d  = datetime.utcnow() - timedelta(days=7)
        cutoff_24h = datetime.utcnow() - timedelta(hours=24)

        # Total searches (7d)
        total = session.exec(
            select(func.count(SearchLog.id))
            .where(SearchLog.timestamp >= cutoff_7d)
        ).one() or 0

        # Top 20 queries by frequency (7d) — ORM, DB-agnostic
        top_rows = session.execute(
            select(
                func.lower(SearchLog.query).label("q"),
                func.count(SearchLog.id).label("n"),
                func.avg(SearchLog.results_count).label("avg_r"),
            )
            .where(SearchLog.timestamp >= cutoff_7d)
            .group_by(func.lower(SearchLog.query))
            .order_by(sa_desc("n"))
            .limit(20)
        ).all()

        # Top 20 zero-result queries (7d) — catalog expansion signal
        zero_rows = session.execute(
            select(
                func.lower(SearchLog.query).label("q"),
                func.count(SearchLog.id).label("n"),
                func.max(SearchLog.timestamp).label("last_seen"),
            )
            .where(SearchLog.timestamp >= cutoff_7d, SearchLog.results_count == 0)
            .group_by(func.lower(SearchLog.query))
            .order_by(sa_desc("n"))
            .limit(20)
        ).all()

        # Queries per hour (24h) — PostgreSQL: date_trunc; SQLite: strftime
        # Detect dialect and use appropriate truncation
        dialect = session.bind.dialect.name if session.bind else "postgresql"
        if dialect == "sqlite":
            hour_expr = sa_func.strftime("%Y-%m-%dT%H:00:00", SearchLog.timestamp)
        else:
            hour_expr = sa_func.date_trunc("hour", SearchLog.timestamp).cast(String)

        hourly_rows = session.execute(
            select(
                hour_expr.label("hour"),
                func.count(SearchLog.id).label("queries"),
            )
            .where(SearchLog.timestamp >= cutoff_24h)
            .group_by("hour")
            .order_by(sa_asc("hour"))
        ).all()

        # Unique user_agents (7d)
        ua_rows = session.execute(
            select(
                SearchLog.user_agent,
                func.count(SearchLog.id).label("n"),
            )
            .where(
                SearchLog.timestamp >= cutoff_7d,
                SearchLog.user_agent.isnot(None),
                SearchLog.user_agent != "",
            )
            .group_by(SearchLog.user_agent)
            .order_by(sa_desc("n"))
            .limit(20)
        ).all()

        return {
            "period": "last_7_days",
            "total_searches_7d": total,
            "top_queries": [
                {"query": r.q, "count": r.n, "avg_results": round(r.avg_r or 0, 1)}
                for r in top_rows
            ],
            "zero_result_queries": [
                {"query": r.q, "count": r.n, "last_seen": str(r.last_seen)}
                for r in zero_rows
            ],
            "queries_per_hour_24h": [
                {"hour": str(r.hour), "queries": r.queries}
                for r in hourly_rows
            ],
            "unique_user_agents": [
                {"user_agent": r.user_agent, "count": r.n}
                for r in ua_rows
            ],
        }

    except Exception as exc:
        # Table empty, just created, or schema mismatch — return empty structure
        empty["_note"] = f"table empty or not yet populated: {type(exc).__name__}"
        return empty


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


@app.get("/api/v1/pricing")
def pricing():
    """Public pricing page for Model II — programmatic bulk access."""
    return {
        "plans": [
            {
                "name": "free",
                "price": "£0/month",
                "daily_limit": 100,
                "features": ["GET /api/v1/tools (search)", "GET /api/v1/tools/{aid}", "MCP endpoint"],
                "note": "No key required for public endpoints.",
            },
            {
                "name": "pro",
                "price": "£29/month",
                "daily_limit": 2000,
                "features": ["GET /api/v1/bulk?aids=... (up to 20 tools per call)", "Priority support", "Usage stats"],
                "note": "Contact founders@aiaam.xyz to get a key.",
            },
            {
                "name": "enterprise",
                "price": "Custom",
                "daily_limit": "unlimited",
                "features": ["Unlimited bulk access", "SLA", "Custom integrations", "Dedicated support"],
                "note": "Contact founders@aiaam.xyz",
            },
        ],
        "bulk_endpoint": "GET /api/v1/bulk?aids=langchain-v1,chroma-v1",
        "auth_header": "X-Api-Key: aik_pro_xxxx",
        "contact": "founders@aiaam.xyz",
    }


@app.get("/admin/api-keys")
def admin_list_keys(
    session: Session = Depends(get_session),
    x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret"),
):
    """Admin — list all API keys with usage stats."""
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    keys = session.exec(select(ApiKey).order_by(ApiKey.created_at.desc())).all()
    return {
        "count": len(keys),
        "keys": [
            {
                "key": k.key[:16] + "...",  # partial reveal for security
                "owner": k.owner,
                "plan": k.plan,
                "active": k.active,
                "daily_limit": k.daily_limit,
                "requests_today": k.requests_today,
                "total_requests": k.total_requests,
                "created_at": k.created_at.isoformat(),
            }
            for k in keys
        ],
    }


@app.delete("/admin/api-keys/{key_prefix}")
def admin_deactivate_key(
    key_prefix: str,
    session: Session = Depends(get_session),
    x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret"),
):
    """Admin — deactivate a key by its full value."""
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    key = session.get(ApiKey, key_prefix)
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")
    key.active = False
    session.add(key)
    session.commit()
    return {"deactivated": key_prefix[:16] + "...", "owner": key.owner}


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

    # All UA strings (7d) — reclassify on the fly (stored agent_type may be stale)
    all_ua_rows = session.exec(
        select(RequestLog.user_agent, func.count(RequestLog.id).label("n"))
        .where(RequestLog.timestamp >= s7d)
        .group_by(RequestLog.user_agent)
        .order_by(func.count(RequestLog.id).desc())
        .limit(200)
    ).all()

    # Named source map — ordered by specificity (most specific first)
    _SOURCE_MAP = [
        # Elite AI coding agents
        ("GitHub Copilot",    re.compile(r"github-copilot|copilot",           re.I), "elite"),
        ("Cursor",            re.compile(r"cursor[\s/]",                       re.I), "elite"),
        ("Claude Code",       re.compile(r"claude-code",                       re.I), "elite"),
        ("Windsurf",          re.compile(r"windsurf",                          re.I), "elite"),
        ("Aider",             re.compile(r"aider[\s/]",                        re.I), "elite"),
        # AI company crawlers (indexing = good signal)
        ("GPTbot (OpenAI)",   re.compile(r"gptbot|chatgpt-user|oai-searchbot", re.I), "ai_crawler"),
        ("Perplexitybot",     re.compile(r"perplexitybot",                     re.I), "ai_crawler"),
        ("Claudebot",         re.compile(r"claudebot|anthropic-ai|claude-web", re.I), "ai_crawler"),
        ("Gemini / Google",   re.compile(r"gemini-bot|google-extended",        re.I), "ai_crawler"),
        ("CCBot",             re.compile(r"ccbot",                             re.I), "ai_crawler"),
        ("Cohere",            re.compile(r"cohere-ai",                         re.I), "ai_crawler"),
        ("ByteSpider",        re.compile(r"bytespider",                        re.I), "ai_crawler"),
        ("Diffbot",           re.compile(r"diffbot",                           re.I), "ai_crawler"),
        # SEO / web crawlers
        ("MJ12bot",           re.compile(r"mj12bot",                           re.I), "seo_crawler"),
        ("Ahrefsbot",         re.compile(r"ahrefsbot",                         re.I), "seo_crawler"),
        ("Semrushbot",        re.compile(r"semrushbot",                        re.I), "seo_crawler"),
        ("Bingbot",           re.compile(r"bingbot|msnbot",                    re.I), "seo_crawler"),
        ("Yandex",            re.compile(r"yandexbot",                         re.I), "seo_crawler"),
        ("DuckDuckBot",       re.compile(r"duckduckbot",                       re.I), "seo_crawler"),
        # Programmatic scripts
        ("Script / curl",     re.compile(r"curl/|python-requests|go-http-client|axios/|scrapy", re.I), "seo_crawler"),
        # Humans
        ("Human — Chrome",    re.compile(r"chrome",                            re.I), "human"),
        ("Human — Firefox",   re.compile(r"firefox",                           re.I), "human"),
        ("Human — Safari",    re.compile(r"safari",                            re.I), "human"),
    ]

    source_counts: dict  = defaultdict(int)
    source_samples: dict = {}
    source_type: dict    = {}
    # Live counts per tier (reclassified from raw UA)
    tier_counts: dict    = defaultdict(int)

    for ua, n in all_ua_rows:
        tier = _classify_agent(ua)   # fresh classification, ignores stale DB column
        tier_counts[tier] += n
        label = "Other bot"
        label_tier = tier
        for name, pat, cat in _SOURCE_MAP:
            if pat.search(ua):
                # Demote "Human — Chrome/Firefox/Safari" if classifier says unknown/bot
                if cat == "human" and tier not in ("human",):
                    continue
                label      = name
                label_tier = cat
                break
        source_counts[label] += n
        if label not in source_samples:
            source_samples[label] = ua[:90]
            source_type[label]    = label_tier

    # Recompute tier counts from fresh classification
    elite_count       = tier_counts.get("elite",       0)
    ai_crawler_count  = tier_counts.get("ai_crawler",  0)
    seo_crawler_count = tier_counts.get("seo_crawler", 0)
    human_count       = tier_counts.get("human",       0)
    unknown_count     = tier_counts.get("unknown",     0)

    visitor_table = sorted(
        [
            {
                "source":     src,
                "count":      cnt,
                "agent_type": source_type.get(src, "unknown"),
                "ua_sample":  source_samples.get(src, ""),
            }
            for src, cnt in source_counts.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )

    # AI Crawler breakdown for pie chart
    ai_crawler_buckets: dict = defaultdict(int)
    elite_buckets: dict      = defaultdict(int)
    for row in visitor_table:
        if row["agent_type"] == "ai_crawler":
            ai_crawler_buckets[row["source"]] += row["count"]
        elif row["agent_type"] == "elite":
            elite_buckets[row["source"]] += row["count"]

    elite_adoption = dict(elite_buckets) if elite_buckets else {"No elite agents yet": 1}
    ai_crawler_data = dict(ai_crawler_buckets) if ai_crawler_buckets else {"No AI crawlers yet": 1}

    # LLM Battleground — full traffic breakdown
    llm_bg = {
        "Elite AI Agents": elite_count,
        "AI Crawlers":     ai_crawler_count,
        "SEO Crawlers":    seo_crawler_count,
        "Humans":          human_count,
        "Unknown Bots":    unknown_count,
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
        "ai_crawler_count":     ai_crawler_count,
        "seo_crawler_count":    seo_crawler_count,
        "human_count":          human_count,
        "unknown_count":        unknown_count,
        "traffic_7d":           json.dumps({"labels": list(daily.keys()),  "data": list(daily.values()), "real": list(daily_real.values())}),
        "traffic_24h":          json.dumps({"labels": list(hourly.keys()), "data": list(hourly.values()), "real": list(hourly_real.values())}),
        "top_tools_json":       json.dumps(top_tools),
        "elite_adoption_json":  json.dumps(elite_adoption),
        "ai_crawler_json":      json.dumps(ai_crawler_data),
        "llm_bg_json":          json.dumps(llm_bg),
        "health_grid":          health_grid,
        "visitor_table":        visitor_table,
        "agent_briefing":       agent_briefing,
        "monetizable_count":    monetizable_count,
        "confirmed_revenue":    confirmed_revenue,
        "tax_count_7d":         tax_count_7d,
    }


_ADMIN_LOGIN_HTML = (
    '<!DOCTYPE html><html lang="en"><head>'
    '<meta charset="UTF-8">'
    '<title>AIAAM Admin</title>'
    '<style>'
    'body{font-family:system-ui,sans-serif;background:#f5f6f8;display:flex;'
    'align-items:center;justify-content:center;height:100vh;margin:0;}'
    '.box{background:#fff;border:1px solid #e5e7eb;border-radius:8px;'
    'padding:40px 48px;min-width:320px;}'
    'h2{font-size:1rem;color:#111827;margin-bottom:20px;}'
    'input{width:100%;border:1px solid #d1d5db;border-radius:4px;'
    'padding:9px 12px;font-size:14px;margin-bottom:12px;box-sizing:border-box;}'
    'button{background:#2563eb;color:#fff;border:none;border-radius:4px;'
    'padding:9px 24px;font-size:14px;cursor:pointer;width:100%;}'
    'button:hover{background:#1d4ed8;}'
    '#err{color:#dc2626;font-size:0.82rem;margin-top:8px;min-height:18px;}'
    '</style></head><body>'
    '<div class="box">'
    '<h2>AIAAM Admin</h2>'
    '<input type="password" id="s" placeholder="Admin secret" autofocus>'
    '<button onclick="go()">Entrar</button>'
    '<div id="err"></div>'
    '</div>'
    '<script>'
    'document.getElementById("s").addEventListener("keydown",function(e){if(e.key==="Enter")go();});'
    'function go(){'
    '  var v=document.getElementById("s").value;'
    '  if(!v){document.getElementById("err").textContent="Introduce el secret.";return;}'
    '  window.location.href="/admin/dashboard?secret="+encodeURIComponent(v);'
    '}'
    '</script>'
    '</body></html>'
)


@app.get("/admin/dashboard", response_class=HTMLResponse, include_in_schema=False)
def admin_dashboard(
    request: Request,
    secret: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
):
    """Original operational dashboard — traffic, tools, revenue, tax logs.
    No secret → shows login form. Wrong secret → 403. Correct → full dashboard.
    """
    if secret is None:
        return HTMLResponse(content=_ADMIN_LOGIN_HTML)
    if secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    ctx = _build_dashboard_ctx(session)
    ctx["request"] = request
    response = templates.TemplateResponse("dashboard.html", ctx)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if "server" in response.headers:
        del response.headers["server"]
    return response


@app.get("/admin/search-dashboard", response_class=HTMLResponse, include_in_schema=False)
def admin_search_dashboard():
    """
    Admin dashboard — auth handled entirely in browser via sessionStorage.
    No secret ever appears in the URL (contains URL-unsafe chars like +, &, }).
    JS validates the secret against /admin/search-trends on first visit,
    then stores it in sessionStorage for subsequent auto-refreshes.
    Curl: curl /admin/dashboard   (no auth needed — page is a static shell)
    """
    # The HTML is a plain string — no f-string, no secret injected server-side.
    # The browser JS reads sessionStorage.getItem("aiaam_secret") on every load.
    _p = (
        '<!DOCTYPE html>'
        '<html lang="en">'
        '<head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<title>AIAAM Search Dashboard</title>'
        '<style>'
        '* { box-sizing: border-box; margin: 0; padding: 0; }'
        'body { font-family: "SF Mono","Fira Code","Consolas",monospace;'
        '       background: #f5f6f8; color: #1f2937; font-size: 13px; line-height: 1.5; }'
        '#auth-overlay { position: fixed; inset: 0; background: #f5f6f8;'
        '  display: flex; align-items: center; justify-content: center; z-index: 999; }'
        '#auth-box { background: #ffffff; border: 1px solid #d1d5db; border-radius: 6px;'
        '  padding: 32px 40px; min-width: 340px; }'
        '#auth-box h2 { color: #111827; font-size: 0.9rem; margin-bottom: 16px; }'
        '#auth-box input { width: 100%; background: #f5f6f8; border: 1px solid #e5e7eb;'
        '  color: #1f2937; font-family: inherit; font-size: 13px; padding: 8px 10px;'
        '  border-radius: 4px; margin-bottom: 10px; }'
        '#auth-box button { background: #bfdbfe; color: #2563eb; border: 1px solid #2a5a8c;'
        '  padding: 8px 20px; border-radius: 4px; cursor: pointer; font-family: inherit; }'
        '#auth-err { color: #dc2626; font-size: 0.8rem; margin-top: 8px; min-height: 18px; }'
        'header { padding: 16px 24px; border-bottom: 1px solid #1e1e1e;'
        '         display: flex; justify-content: space-between; align-items: baseline; }'
        'header h1 { color: #111827; font-size: 1rem; font-weight: 600; }'
        'header .meta { color: #6b7280; font-size: 0.78rem; }'
        'header .signout { color: #6b7280; font-size: 0.75rem; cursor: pointer;'
        '  text-decoration: underline; margin-left: 16px; }'
        '.page { max-width: 1100px; margin: 0 auto; padding: 24px; }'
        '.cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 28px; }'
        '.card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 5px; padding: 16px 20px; }'
        '.card .n { font-size: 2rem; font-weight: 700; color: #111827; }'
        '.card .l { color: #9ca3af; font-size: 0.75rem; margin-top: 2px; }'
        'section { margin-bottom: 32px; }'
        'section h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em;'
        '  color: #9ca3af; margin-bottom: 10px; border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; }'
        'section h2 .badge { font-size: 0.7rem; background: #1a3a1a; color: #5a9a5a;'
        '  border: 1px solid #2a5a2a; border-radius: 3px; padding: 1px 6px; margin-left: 8px;'
        '  text-transform: none; letter-spacing: 0; vertical-align: middle; }'
        'table { width: 100%; border-collapse: collapse; }'
        'th { text-align: left; color: #6b7280; font-weight: 400; padding: 4px 12px 8px 0;'
        '     font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; }'
        'td { padding: 5px 12px 5px 0; border-top: 1px solid #f3f4f6; vertical-align: top; }'
        'tr:hover td { background: #ffffff; }'
        '.query-text { color: #374151; } .zero-text { color: #dc2626; }'
        '.num { color: #2563eb; text-align: right; padding-right: 24px; } .dim { color: #6b7280; }'
        '.agents-list { display: flex; flex-direction: column; gap: 4px; }'
        '.agent-row { display: flex; gap: 12px; }'
        '.agent-ua { color: #6b7280; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;'
        '            max-width: 700px; flex: 1; }'
        '.agent-n { color: #2563eb; flex-shrink: 0; width: 40px; text-align: right; }'
        '#refresh-bar { height: 2px; background: #bfdbfe; transition: width linear; }'
        '.empty { color: #d1d5db; padding: 12px 0; font-style: italic; }'
        '.bar-wrap { overflow-x: auto; }'
        '</style>'
        '</head>'
        '<body>'
        # ── Auth overlay (hidden once authenticated) ──────────────────────
        '<div id="auth-overlay">'
        '  <div id="auth-box">'
        '    <h2>AIAAM Dashboard</h2>'
        '    <input type="password" id="secret-input" placeholder="Admin secret" autocomplete="current-password">'
        '    <br>'
        '    <button onclick="tryLogin()">Sign in</button>'
        '    <div id="auth-err"></div>'
        '  </div>'
        '</div>'
        # ── Main dashboard (hidden until authenticated) ───────────────────
        '<div id="main" style="display:none">'
        '<div id="refresh-bar" style="width:100%"></div>'
        '<header>'
        '  <h1>AIAAM / Search Dashboard</h1>'
        '  <div class="meta">'
        '    <span id="last-updated">loading…</span> &nbsp;\xb7&nbsp;'
        '    auto-refresh <span id="countdown">60</span>s'
        '    <span class="signout" onclick="signOut()">sign out</span>'
        '  </div>'
        '</header>'
        '<div class="page">'
        '  <div class="cards">'
        '    <div class="card"><div class="n" id="c-total">—</div><div class="l">searches \xb7 last 7 days</div></div>'
        '    <div class="card"><div class="n" id="c-top">—</div><div class="l">unique queries</div></div>'
        '    <div class="card"><div class="n" id="c-zero" style="color:#dc2626">—</div><div class="l">zero-result queries</div></div>'
        '  </div>'
        '  <section>'
        '    <h2>Zero-result queries <span class="badge">catalog expansion signal</span></h2>'
        '    <table id="tbl-zero"><thead><tr>'
        '      <th>Query</th><th class="num">Times searched</th><th>Last seen</th>'
        '    </tr></thead><tbody></tbody></table>'
        '  </section>'
        '  <section>'
        '    <h2>Top queries \xb7 last 7 days</h2>'
        '    <table id="tbl-top"><thead><tr>'
        '      <th>Query</th><th class="num">Count</th><th class="num">Avg results</th>'
        '    </tr></thead><tbody></tbody></table>'
        '  </section>'
        '  <section>'
        '    <h2>Queries per hour \xb7 last 24h</h2>'
        '    <div class="bar-wrap"><svg id="chart" width="900" height="140"></svg></div>'
        '  </section>'
        '  <section>'
        '    <h2>User agents \xb7 last 7 days</h2>'
        '    <div class="agents-list" id="agents-list"></div>'
        '  </section>'
        '</div>'
        '</div>'
        # ── JavaScript ────────────────────────────────────────────────────
        '<script>'
        'var SECRET = sessionStorage.getItem("aiaam_secret");'
        'var countdown = 60;'
        'var countdownTimer = null;'
        ''
        'function tryLogin() {'
        '  var s = document.getElementById("secret-input").value;'
        '  document.getElementById("auth-err").textContent = "";'
        '  fetch("/admin/search-trends", { headers: { "X-Admin-Secret": s } })'
        '    .then(function(r) {'
        '      if (r.ok) {'
        '        sessionStorage.setItem("aiaam_secret", s);'
        '        SECRET = s;'
        '        showDashboard();'
        '      } else {'
        '        document.getElementById("auth-err").textContent = "Invalid secret (" + r.status + ")";'
        '      }'
        '    })'
        '    .catch(function(e) {'
        '      document.getElementById("auth-err").textContent = "Network error: " + e.message;'
        '    });'
        '}'
        ''
        'function signOut() {'
        '  sessionStorage.removeItem("aiaam_secret");'
        '  SECRET = null;'
        '  document.getElementById("main").style.display = "none";'
        '  document.getElementById("auth-overlay").style.display = "flex";'
        '}'
        ''
        'function showDashboard() {'
        '  document.getElementById("auth-overlay").style.display = "none";'
        '  document.getElementById("main").style.display = "block";'
        '  load().then(startCountdown);'
        '}'
        ''
        'function load() {'
        '  return fetch("/admin/search-trends", { headers: { "X-Admin-Secret": SECRET } })'
        '    .then(function(r) {'
        '      if (!r.ok) {'
        '        document.body.innerHTML = "<p style=\'color:#e07a5f;padding:24px\'>Error " + r.status + " — secret may have changed, please sign out and sign in again.</p>";'
        '        return;'
        '      }'
        '      return r.json();'
        '    })'
        '    .then(function(d) {'
        '      if (!d) return;'
        '      render(d);'
        '      document.getElementById("last-updated").textContent = "updated " + new Date().toLocaleTimeString();'
        '    })'
        '    .catch(function(e) {'
        '      var el = document.getElementById("last-updated");'
        '      if (el) el.textContent = "fetch error: " + e.message;'
        '    });'
        '}'
        ''
        'function render(d) {'
        '  var total  = d.total_searches_7d || 0;'
        '  var top    = d.top_queries || [];'
        '  var zero   = d.zero_result_queries || [];'
        '  var hourly = d.queries_per_hour_24h || [];'
        '  var agents = d.unique_user_agents || [];'
        '  document.getElementById("c-total").textContent = total.toLocaleString();'
        '  document.getElementById("c-top").textContent   = top.length;'
        '  document.getElementById("c-zero").textContent  = zero.length;'
        '  var zBody = document.querySelector("#tbl-zero tbody");'
        '  if (zero.length === 0) {'
        '    zBody.innerHTML = "<tr><td colspan=\'3\' class=\'empty\'>No zero-result queries yet.</td></tr>";'
        '  } else {'
        '    zBody.innerHTML = zero.map(function(r) {'
        '      return "<tr><td class=\'zero-text\'>" + esc(r.query) + "</td>"'
        '           + "<td class=\'num\'>" + r.count + "</td>"'
        '           + "<td class=\'dim\'>" + (r.last_seen ? r.last_seen.slice(0,16).replace("T"," ") : "") + "</td></tr>";'
        '    }).join("");'
        '  }'
        '  var tBody = document.querySelector("#tbl-top tbody");'
        '  if (top.length === 0) {'
        '    tBody.innerHTML = "<tr><td colspan=\'3\' class=\'empty\'>No queries yet.</td></tr>";'
        '  } else {'
        '    tBody.innerHTML = top.map(function(r) {'
        '      return "<tr><td class=\'query-text\'>" + esc(r.query) + "</td>"'
        '           + "<td class=\'num\'>" + r.count + "</td>"'
        '           + "<td class=\'num dim\'>" + r.avg_results + "</td></tr>";'
        '    }).join("");'
        '  }'
        '  drawChart(hourly);'
        '  var aList = document.getElementById("agents-list");'
        '  if (agents.length === 0) {'
        '    aList.innerHTML = "<span class=\'empty\'>No user agents recorded yet.</span>";'
        '  } else {'
        '    aList.innerHTML = agents.map(function(a) {'
        '      return "<div class=\'agent-row\'>"'
        '           + "<span class=\'agent-n\'>" + a.count + "</span>"'
        '           + "<span class=\'agent-ua\' title=\'" + esc(a.user_agent) + "\'>" + esc(a.user_agent) + "</span>"'
        '           + "</div>";'
        '    }).join("");'
        '  }'
        '}'
        ''
        'function drawChart(hourly) {'
        '  var svg = document.getElementById("chart");'
        '  if (!hourly.length) {'
        '    svg.innerHTML = "<text x=\'12\' y=\'70\' fill=\'#9ca3af\' font-size=\'12\'>No data yet.</text>";'
        '    return;'
        '  }'
        '  var W = 900, H = 120, padL = 40, padR = 12, padT = 10, padB = 28;'
        '  var maxQ   = Math.max.apply(null, hourly.map(function(h){ return h.queries; }).concat([1]));'
        '  var slot   = (W - padL - padR) / hourly.length;'
        '  var bw     = Math.max(4, Math.floor(slot) - 2);'
        '  var chartH = H - padT - padB;'
        '  var bars = "", labels = "";'
        '  hourly.forEach(function(h, i) {'
        '    var x  = padL + i * slot;'
        '    var bh = Math.max(2, Math.round((h.queries / maxQ) * chartH));'
        '    var y  = padT + chartH - bh;'
        '    bars += "<rect x=\'" + x + "\' y=\'" + y + "\' width=\'" + bw + "\' height=\'" + bh + "\' fill=\'#bfdbfe\' rx=\'1\'>"'
        '         +  "<title>" + (h.hour ? h.hour.slice(11,16) : "") + " — " + h.queries + " queries</title></rect>";'
        '    if (i % 4 === 0 && h.hour) {'
        '      var lbl = h.hour.slice(11, 16);'
        '      labels += "<text x=\'" + (x + bw/2) + "\' y=\'" + (H-4) + "\' fill=\'#9ca3af\' font-size=\'9\' text-anchor=\'middle\'>" + lbl + "</text>";'
        '    }'
        '  });'
        '  var axis = "<line x1=\'" + (padL-4) + "\' y1=\'" + padT + "\' x2=\'" + (padL-4) + "\' y2=\'" + (padT+chartH) + "\' stroke=\'#e5e7eb\'/>"'
        '           + "<text x=\'" + (padL-6) + "\' y=\'" + (padT+6) + "\' fill=\'#9ca3af\' font-size=\'9\' text-anchor=\'end\'>" + maxQ + "</text>"'
        '           + "<text x=\'" + (padL-6) + "\' y=\'" + (padT+chartH) + "\' fill=\'#9ca3af\' font-size=\'9\' text-anchor=\'end\'>0</text>";'
        '  svg.innerHTML = axis + bars + labels;'
        '}'
        ''
        'function esc(s) {'
        '  return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");'
        '}'
        ''
        'function startCountdown() {'
        '  clearInterval(countdownTimer);'
        '  countdown = 60;'
        '  var bar = document.getElementById("refresh-bar");'
        '  if (!bar) return;'
        '  bar.style.transition = "none";'
        '  bar.style.width = "100%";'
        '  requestAnimationFrame(function() {'
        '    bar.style.transition = "width 60s linear";'
        '    bar.style.width = "0%";'
        '  });'
        '  countdownTimer = setInterval(function() {'
        '    countdown--;'
        '    var el = document.getElementById("countdown");'
        '    if (el) el.textContent = countdown;'
        '    if (countdown <= 0) {'
        '      clearInterval(countdownTimer);'
        '      load().then(startCountdown);'
        '    }'
        '  }, 1000);'
        '}'
        ''
        '// Auto-login if secret already in sessionStorage'
        'if (SECRET) { showDashboard(); }'
        'else {'
        '  document.getElementById("secret-input").addEventListener("keydown", function(e) {'
        '    if (e.key === "Enter") tryLogin();'
        '  });'
        '}'
        '</script>'
        '</body>'
        '</html>'
    )
    return HTMLResponse(content=_p)


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

# Layer 1 — Hard caps
_MAX_PER_IP_HOUR  = 2    # anonymous public users: max 2/hour/IP
_MAX_DAILY_TOTAL  = 10   # global hard cap: max 10 compilations/day across all users
_MAX_DAILY_APIKEY = 20   # API-key holders: max 20/day per key (tracked in ApiKey table)


def _rate_limit_check(ip: str) -> tuple[bool, str]:
    now = datetime.utcnow()
    with _rl_lock:
        if (now - _daily_reset_at[0]).days >= 1:
            _daily_count[0]    = 0
            _daily_reset_at[0] = now
        if _daily_count[0] >= _MAX_DAILY_TOTAL:
            return False, (
                f"Global daily compilation limit reached ({_MAX_DAILY_TOTAL}/day). "
                "Resets at midnight UTC. Use an API key for higher limits."
            )
        cutoff = now - timedelta(hours=1)
        _ip_timestamps[ip] = [t for t in _ip_timestamps[ip] if t > cutoff]
        if len(_ip_timestamps[ip]) >= _MAX_PER_IP_HOUR:
            return False, (
                f"Rate limit: max {_MAX_PER_IP_HOUR} compilations per IP per hour. "
                "Use an API key for higher limits."
            )
        _ip_timestamps[ip].append(now)
        _daily_count[0] += 1
        return True, ""


# Layer 2 — Domain allowlist for unauthenticated requests
# Only well-known public spec repositories are allowed without an API key.
# Arbitrary domains require a valid API key (prevents SSRF-adjacent abuse
# and stops agents from compiling random internal services).
_PUBLIC_DOMAIN_ALLOWLIST = {
    "api.apis.guru",
    "apis.guru",
    "raw.githubusercontent.com",
    "petstore3.swagger.io",
    "petstore.swagger.io",
    "editor.swagger.io",
    "validator.swagger.io",
}


def _domain_allowed_without_key(url: str) -> bool:
    """Returns True if the URL's domain is in the public allowlist."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower().split(":")[0]
    return host in _PUBLIC_DOMAIN_ALLOWLIST


# Layer 3 — SSRF blocklist (always enforced, even with API key)
_SSRF_BLOCKLIST = [
    "localhost", "127.", "192.168.", "10.", "172.16.", "172.17.",
    "0.0.0.0", "::1", "internal", "railway.app", "railwayapp.com",
    "metadata.google", "169.254.",
]


class SubmitAPIRequest(BaseModel):
    openapi_url: HttpUrl
    category: str = Field(
        default="other",
        pattern=r"^(payments|finance|communication|devtools|google|ai|productivity|security|ecommerce|media|social|data|other)$",
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
    x_api_key: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    """
    Compile a public OpenAPI/Swagger URL into a MAI-API manifest via Haiku.
    Returns HTTP 202 immediately; compilation runs in background (~15s).

    Access rules:
    - No API key: only URLs from the public allowlist (apis.guru, raw.githubusercontent.com, etc.)
    - Valid API key: any public URL (SSRF blocklist still enforced)

    Rate limits:
    - No key: 2/hour/IP · 10/day global hard cap
    - API key: 20/day per key
    """
    url_str = str(body.openapi_url)
    ip = request.client.host if request.client else "unknown"

    # ── Layer 2: domain allowlist check for unauthenticated requests ──
    if not x_api_key:
        if not _domain_allowed_without_key(url_str):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "api_key_required",
                    "message": (
                        "Compiling arbitrary URLs requires an API key. "
                        "Without a key, only public spec repositories are allowed "
                        "(apis.guru, raw.githubusercontent.com). "
                        "See https://aiaam.xyz/api/v1/pricing for plans."
                    ),
                    "allowed_without_key": sorted(_PUBLIC_DOMAIN_ALLOWLIST),
                },
            )
        # Anonymous user — apply strict rate limit
        allowed, err_msg = _rate_limit_check(ip)
        if not allowed:
            raise HTTPException(status_code=429, detail=err_msg)
    else:
        # ── Layer 1 (API key path): validate key + per-key daily limit ──
        key_record = session.get(ApiKey, x_api_key)
        if not key_record or not key_record.active:
            raise HTTPException(status_code=401, detail="Invalid or inactive API key.")

        # Reset daily counter if needed
        now = datetime.utcnow()
        if (now - key_record.last_reset).days >= 1:
            key_record.requests_today = 0
            key_record.last_reset = now

        if key_record.requests_today >= _MAX_DAILY_APIKEY:
            raise HTTPException(
                status_code=429,
                detail=f"API key daily limit reached ({_MAX_DAILY_APIKEY}/day). Resets at midnight UTC.",
            )

        key_record.requests_today += 1
        key_record.total_requests += 1
        session.add(key_record)
        session.commit()

    domain = urlparse(url_str).netloc.lower()
    service_hint = domain.split(".")[0]

    background_tasks.add_task(_compile_background, url_str, body.category)

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


@app.post("/admin/ingest-compiled-api")
def admin_ingest_compiled_api(
    body: dict,
    x_admin_secret: Optional[str] = Header(None),
    session: Session = Depends(get_session),
):
    """Upsert a CompiledAPI record from local SQLite → production PostgreSQL."""
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    service_name = body.get("service_name", "").lower().strip()
    if not service_name:
        raise HTTPException(status_code=422, detail="service_name required")

    existing = session.exec(
        select(CompiledAPI).where(CompiledAPI.service_name == service_name)
    ).first()

    if existing:
        existing.category          = body.get("category", existing.category)
        existing.source_url        = body.get("source_url", existing.source_url)
        existing.manifest          = body.get("manifest", existing.manifest)
        existing.reliability_score = body.get("reliability_score", existing.reliability_score)
        existing.tokens_used       = body.get("tokens_used", existing.tokens_used)
        existing.verified          = body.get("verified", existing.verified)
        session.add(existing)
        session.commit()
        return {"ok": True, "action": "updated", "service_name": service_name}

    record = CompiledAPI(
        service_name      = service_name,
        category          = body.get("category", "other"),
        source_url        = body.get("source_url", ""),
        manifest          = body.get("manifest", {}),
        reliability_score = body.get("reliability_score", 0.80),
        tokens_used       = body.get("tokens_used", 0),
        verified          = body.get("verified", False),
    )
    session.add(record)
    session.commit()
    return {"ok": True, "action": "created", "service_name": service_name}


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
    {
        "name": "get_api_manifest",
        "description": (
            "Returns a compact MAI-API manifest for a web API service (Stripe, Slack, GitHub, etc.). "
            "Each manifest is ~850 tokens and contains the base URL, auth method, and up to 20 key "
            "endpoints. Use this instead of fetching and parsing a full OpenAPI spec. "
            "Available services include: stripe_api, slack_web_api, github, openai_api, notion_api, "
            "jira_cloud_rest_api, gmail_api, google_calendar_api, twilio_api, sendgrid_email_activity_api, "
            "spotify_web_api, twitter_api_v2, and 30+ more. "
            "Returns {error: not_found} if the service has not been compiled yet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": (
                        "The service identifier, e.g. 'stripe_api', 'slack_web_api', 'openai_api'. "
                        "Use underscores, lowercase. Check /llmo-apis for the full list."
                    ),
                },
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "compile_api",
        "description": (
            "Compiles a public OpenAPI/Swagger spec URL into a compact MAI-API manifest. "
            "Requires a valid API key (X-Api-Key). Without a key, only specs from "
            "apis.guru or raw.githubusercontent.com are accepted. "
            "Returns immediately with a status_url to poll for the result (~15 seconds). "
            "Use get_api_manifest to fetch the result once ready."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "openapi_url": {
                    "type": "string",
                    "description": "Public URL of an OpenAPI 3.x or Swagger 2.x JSON/YAML spec.",
                },
                "category": {
                    "type": "string",
                    "description": "Category hint: payments, communication, devtools, ai, productivity, security, data, other",
                    "default": "other",
                },
                "api_key": {
                    "type": "string",
                    "description": "Optional API key for unrestricted URL access. Without key, only apis.guru and github raw URLs are allowed.",
                },
            },
            "required": ["openapi_url"],
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
    verified = Tool.verified == True
    not_dead = or_(Tool.status.is_(None), Tool.status != "dead")
    conditions = [verified, not_dead]
    clause = _tool_search_clause(query)
    if clause is not None:
        conditions.append(clause)
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

        if tool_name == "get_api_manifest":
            service_name = arguments.get("service_name", "").strip().lower()
            if not service_name:
                return _jsonrpc_err(req_id, -32602, "argument 'service_name' is required")
            record = session.exec(
                select(CompiledAPI)
                .where(CompiledAPI.service_name == service_name)
                .where(CompiledAPI.verified == True)
            ).first()
            if not record:
                return _jsonrpc_ok(req_id, _mcp_content({
                    "error": "not_found",
                    "message": f"No verified manifest for '{service_name}'. "
                               "Check https://aiaam.xyz/llmo-apis for available services, "
                               "or use compile_api to submit a new spec.",
                }))
            return _jsonrpc_ok(req_id, _mcp_content({
                "service_name": record.service_name,
                "manifest": record.manifest,
                "compiled_at": record.compiled_at.isoformat() + "Z",
                "source": record.source_url,
            }))

        if tool_name == "compile_api":
            openapi_url = arguments.get("openapi_url", "").strip()
            category    = arguments.get("category", "other")
            api_key     = arguments.get("api_key", "")
            if not openapi_url:
                return _jsonrpc_err(req_id, -32602, "argument 'openapi_url' is required")
            for blocked in _SSRF_BLOCKLIST:
                if blocked in openapi_url.lower():
                    return _jsonrpc_err(req_id, -32602, "URL not allowed: private/internal hosts are blocked.")
            if not api_key:
                if not _domain_allowed_without_key(openapi_url):
                    return _jsonrpc_ok(req_id, _mcp_content({
                        "error": "api_key_required",
                        "message": (
                            "Compiling arbitrary URLs requires an API key. "
                            "Without a key, only these domains are allowed: "
                            + ", ".join(sorted(_PUBLIC_DOMAIN_ALLOWLIST)) + ". "
                            "See https://aiaam.xyz/api/v1/pricing for plans."
                        ),
                    }))
            else:
                key_record = session.get(ApiKey, api_key)
                if not key_record or not key_record.active:
                    return _jsonrpc_ok(req_id, _mcp_content({"error": "invalid_api_key"}))
                now_dt = datetime.utcnow()
                if (now_dt - key_record.last_reset).days >= 1:
                    key_record.requests_today = 0
                    key_record.last_reset = now_dt
                if key_record.requests_today >= _MAX_DAILY_APIKEY:
                    return _jsonrpc_ok(req_id, _mcp_content({
                        "error": "rate_limit",
                        "message": f"API key daily limit reached ({_MAX_DAILY_APIKEY}/day).",
                    }))
                key_record.requests_today += 1
                key_record.total_requests += 1
                session.add(key_record)
                session.commit()
            asyncio.create_task(_compile_background(openapi_url, category))
            from urllib.parse import urlparse as _up
            service_hint = _up(openapi_url).netloc.lower().split(".")[0]
            return _jsonrpc_ok(req_id, _mcp_content({
                "status": "queued",
                "message": "Compilation started (~15 seconds).",
                "status_url": f"https://aiaam.xyz/api/v1/services/{service_hint}/status",
                "next_step": f"Call get_api_manifest(service_name='{service_hint}') after ~15 seconds.",
            }))

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

        if tool_name == "get_api_manifest":
            service_name = arguments.get("service_name", "").strip().lower()
            if not service_name:
                return _jsonrpc_err(req_id, -32602, "argument 'service_name' is required")
            record = session.exec(
                select(CompiledAPI)
                .where(CompiledAPI.service_name == service_name)
                .where(CompiledAPI.verified == True)
            ).first()
            if not record:
                return _jsonrpc_ok(req_id, _mcp_content({
                    "error": "not_found",
                    "message": f"No verified manifest for '{service_name}'. "
                               "Check /llmo-apis for available services, or use compile_api to submit a new spec.",
                }))
            return _jsonrpc_ok(req_id, _mcp_content({
                "service_name": record.service_name,
                "manifest": record.manifest,
                "compiled_at": record.compiled_at.isoformat() + "Z",
                "source": record.source_url,
            }))

        if tool_name == "compile_api":
            openapi_url = arguments.get("openapi_url", "").strip()
            category    = arguments.get("category", "other")
            api_key     = arguments.get("api_key", "")

            if not openapi_url:
                return _jsonrpc_err(req_id, -32602, "argument 'openapi_url' is required")

            # SSRF check
            for blocked in _SSRF_BLOCKLIST:
                if blocked in openapi_url.lower():
                    return _jsonrpc_err(req_id, -32602, f"URL not allowed: private/internal hosts are blocked.")

            # Domain allowlist or API key required
            if not api_key:
                if not _domain_allowed_without_key(openapi_url):
                    return _jsonrpc_ok(req_id, _mcp_content({
                        "error": "api_key_required",
                        "message": (
                            "Compiling arbitrary URLs requires an API key. "
                            "Without a key, only public spec repositories are allowed: "
                            + ", ".join(sorted(_PUBLIC_DOMAIN_ALLOWLIST)) + ". "
                            "See https://aiaam.xyz/api/v1/pricing for plans."
                        ),
                    }))
            else:
                key_record = session.get(ApiKey, api_key)
                if not key_record or not key_record.active:
                    return _jsonrpc_ok(req_id, _mcp_content({
                        "error": "invalid_api_key",
                        "message": "The provided API key is invalid or inactive.",
                    }))
                now = datetime.utcnow()
                if (now - key_record.last_reset).days >= 1:
                    key_record.requests_today = 0
                    key_record.last_reset = now
                if key_record.requests_today >= _MAX_DAILY_APIKEY:
                    return _jsonrpc_ok(req_id, _mcp_content({
                        "error": "rate_limit",
                        "message": f"API key daily limit reached ({_MAX_DAILY_APIKEY}/day). Resets at midnight UTC.",
                    }))
                key_record.requests_today += 1
                key_record.total_requests += 1
                session.add(key_record)
                session.commit()

            # Queue compilation in background
            import asyncio
            asyncio.create_task(_compile_background(openapi_url, category))

            from urllib.parse import urlparse as _up
            service_hint = _up(openapi_url).netloc.lower().split(".")[0]
            return _jsonrpc_ok(req_id, _mcp_content({
                "status": "queued",
                "message": "Compilation started (~15 seconds). Poll status_url to check when ready.",
                "status_url": f"https://aiaam.xyz/api/v1/services/{service_hint}/status",
                "next_step": f"Call get_api_manifest(service_name='{service_hint}') after ~15 seconds.",
            }))

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
