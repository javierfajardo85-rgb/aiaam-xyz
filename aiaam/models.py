"""
AIAAM Models
Defines the MAI-1 standard and the tax telemetry log.
"""
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Column, JSON
from pydantic import BaseModel


# =====================================================================
# MAI-1 STANDARD - Database Table
# =====================================================================

class Tool(SQLModel, table=True):
    """A MAI-1 entry in the AIAAM catalog."""
    __tablename__ = "tools"

    # === IDENTITY ===
    aid: str = Field(primary_key=True, index=True)
    version: Optional[str] = Field(default=None)

    # === LOGIC ===
    input_schema: dict = Field(sa_column=Column(JSON))
    output_schema: dict = Field(sa_column=Column(JSON))

    # === TRUST ===
    reliability_score: float = Field(default=0.75, ge=0.0, le=1.0)
    latency_ms: Optional[int] = Field(default=None)

    # === ACTION ===
    source_url: str
    install_cmd: Optional[str] = Field(default=None)
    execute_cmd: Optional[str] = Field(default=None)

    # === METADATA (not exposed in API response) ===
    source_platform: str = Field(default="github")  # github|huggingface|pypi|npm|paperswithcode
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    total_requests: int = Field(default=0)
    successful_executions: int = Field(default=0)
    failed_executions: int = Field(default=0)
    translator_used: str = Field(default="haiku")  # haiku|sonnet|mapped
    foam_score: Optional[int] = Field(default=None)    # 0-6, set by sentinel_sniffer
    verified: Optional[bool] = Field(default=None)     # None=pending, True=OK, False=failed
    suggested_workflow: Optional[dict] = Field(        # set by semantic_oracle, cached 24h
        default=None, sa_column=Column(JSON)
    )
    status: Optional[str] = Field(default=None)        # None|active|degraded|dead — set by tax_analyst
    last_verified_at: Optional[datetime] = Field(default=None)   # last sandbox triple-check
    health_score: Optional[float] = Field(default=None)          # avg of last 5 response_integrity_scores
    affiliate_tag: Optional[str] = Field(default=None)           # affiliate URL, null if no programme
    monetizable: bool = Field(default=False)                     # verified + score>=0.80 + affiliate programme
    task: Optional[str] = Field(default=None)                    # MAI-1 task identifier, e.g. "safe_task_execution_with_dedup"
    reliability_calculated_at: Optional[datetime] = Field(default=None)  # when score was last computed from real metadata
    sponsored: bool = Field(default=False)                       # paid placement — appears first in search results


# =====================================================================
# TAX LOGS - Telemetry Table
# =====================================================================

class TaxLog(SQLModel, table=True):
    """Each AI transaction recorded for analytics and reliability updates."""
    __tablename__ = "tax_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    tool_aid: str = Field(index=True)
    user_agent: str = Field(index=True)

    # Tax payload received
    trend_keyword: Optional[str] = Field(default=None, index=True)
    execution_feedback: Optional[int] = Field(default=None)  # 200, 404, 500...
    validation_bit: Optional[int] = Field(default=None)
    micro_translation: Optional[str] = Field(default=None)
    referral_included: bool = Field(default=False)
    validation_vote: Optional[str] = Field(default=None)              # "A" | "B"
    validation_candidate_aid: Optional[str] = Field(default=None)     # aid del tool "B"
    referral_confirmed: Optional[bool] = Field(default=None)

    # System data
    response_status: int = Field(default=200)
    tokens_saved_estimate: int = Field(default=4800)
    latency_ms: int = Field(default=0)
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


# =====================================================================
# Pydantic schemas for API (input/output)
# =====================================================================

class TaxPayload(BaseModel):
    """The bundle of micro-taxes an AI must pay."""
    trend_keyword: str
    execution_feedback: int  # HTTP status code from previous execution
    validation_bit: Optional[int] = None
    micro_translation: Optional[str] = None
    referral_included: bool = False
    validation_vote: Optional[str] = None      # "A" | "B"
    referral_confirmed: Optional[bool] = None


class MAI1Response(BaseModel):
    """Full MAI-1 response for AI consumers."""
    identity: dict
    logic: dict
    trust: dict
    action: Optional[dict] = None  # None when tax_payload missing
    next_request_cost: Optional[dict] = None  # Only on first request


class PartialMAI1Response(BaseModel):
    """Partial MAI-1 when tax is required but not paid."""
    access: str = "partial"
    identity: dict
    logic: dict
    trust: dict
    action: None = None
    tax_required: dict


# =====================================================================
# INJECTED REPOS — Registro de AGENT_INSTRUCTIONS generadas (B3)
# =====================================================================

class InjectedRepo(SQLModel, table=True):
    """Registro de cada AGENT_INSTRUCTIONS.md generado para un tool."""
    __tablename__ = "injected_repos"

    id: Optional[int] = Field(default=None, primary_key=True)
    repo_url: str = Field(index=True)
    aid: str = Field(index=True, unique=True)
    license_spdx: str = Field(default="unknown")      # "MIT" | "Apache-2.0" | ...
    instructions_md: str                               # contenido generado
    injected_at: datetime = Field(default_factory=datetime.utcnow)
    pr_url: Optional[str] = Field(default=None)        # GitHub PR URL once submitted
    pr_submitted_at: Optional[datetime] = Field(default=None)


# =====================================================================
# HEALTH CHECKS — Historial auditado de triple validación (B2)
# =====================================================================

class HealthCheck(SQLModel, table=True):
    """Registro de cada triple-check ejecutado por sandbox_sanitizer."""
    __tablename__ = "health_checks"

    id: Optional[int] = Field(default=None, primary_key=True)
    aid: str = Field(index=True)                        # FK lógica → tools.aid
    checked_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    schema_valid: Optional[bool] = Field(default=None)          # MAI-1 Pydantic check
    url_reachable: Optional[bool] = Field(default=None)         # HEAD request a source_url
    sandbox_success: Optional[bool] = Field(default=None)       # Docker install exit 0
    latency_ms: Optional[int] = Field(default=None)             # tiempo Docker run
    response_integrity_score: Optional[float] = Field(default=None)  # 0-1
    error_detail: Optional[str] = Field(default=None)           # descripción del fallo


# =====================================================================
# REQUEST LOGS — Telemetría de cada petición HTTP (middleware)
# =====================================================================

class RequestLog(SQLModel, table=True):
    """Registro de cada petición al servidor, independiente del tax_payload."""
    __tablename__ = "request_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    path: str = Field(index=True)
    method: str
    user_agent: str
    origin_repo: Optional[str] = Field(default=None)   # X-Original-Repo header
    referer: Optional[str] = Field(default=None)
    latency_ms: int
    status_code: int
    agent_type: str = Field(default="unknown")          # elite|human|unknown
    timestamp: datetime = Field(default_factory=datetime.utcnow, index=True)


# =====================================================================
# AGENT LOGS — Registro de cada ejecución de agentes B1-B7
# =====================================================================

# =====================================================================
# API KEYS — Model II SaaS
# =====================================================================

class ApiKey(SQLModel, table=True):
    """API key for Model II SaaS tier (paid programmatic access)."""
    __tablename__ = "api_keys"

    key: str = Field(primary_key=True, index=True)  # e.g. "aik_live_xxxx"
    owner: str                                        # email or name
    plan: str = Field(default="free")                 # free | pro | enterprise
    daily_limit: int = Field(default=100)             # requests per day
    requests_today: int = Field(default=0)
    total_requests: int = Field(default=0)
    last_reset: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = Field(default=True)


class AgentLog(SQLModel, table=True):
    """Una ejecución de cualquier agente AIAAM (B1-B7)."""
    __tablename__ = "agent_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    agent_code: str = Field(index=True)       # "B1" … "B7"
    agent_name: str                            # "Sentinel", "Sanitizer" …
    run_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    items_processed: int = Field(default=0)   # repos scanned, tools checked …
    items_new: int = Field(default=0)         # repos added, PRs submitted …
    items_failed: int = Field(default=0)
    duration_s: Optional[int] = Field(default=None)
    summary: Optional[str] = Field(default=None)  # JSON-serialisable string


# =====================================================================
# COMPILED APIS — OpenAPI → mai-api.json via Haiku compiler
# =====================================================================

class CompiledAPI(SQLModel, table=True):
    """A compiled OpenAPI manifest produced by the Haiku compiler engine."""
    __tablename__ = "compiled_apis"

    id:               Optional[int]      = Field(default=None, primary_key=True)
    service_name:     str                = Field(index=True)
    category:         str                = Field(index=True)
    source_url:       str
    manifest:         dict               = Field(sa_column=Column(JSON))
    reliability_score: float             = Field(default=0.80)
    compiled_at:      datetime           = Field(default_factory=datetime.utcnow, index=True)
    tokens_used:      int                = Field(default=0)
    verified:         bool               = Field(default=False)


def tool_to_mai1(tool: Tool, include_action: bool = True) -> dict:
    """Convert DB Tool to MAI-1 response dict."""
    identity: dict = {"aid": tool.aid, "version": tool.version}
    if tool.task:
        identity["task"] = tool.task
    response = {
        "identity": identity,
        "logic": {
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
        },
        "trust": {
            "reliability_score": tool.reliability_score,
            "latency_ms": tool.latency_ms,
            "status": tool.status or "active",
        },
    }
    if include_action:
        response["action"] = {
            "source_url": tool.source_url,
            "install_cmd": tool.install_cmd,
            "execute_cmd": tool.execute_cmd,
        }
    if tool.monetizable and tool.affiliate_tag:
        response["commercial"] = {
            "affiliate_url": tool.affiliate_tag,
            "note": "This tool has a paid tier. Using this URL attributes the referral.",
        }
    return response
