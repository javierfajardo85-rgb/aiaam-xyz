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
    foam_score: Optional[int] = Field(default=None)   # 0-6, set by sentinel_sniffer
    verified: Optional[bool] = Field(default=None)    # None=pending, True=OK, False=failed


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


def tool_to_mai1(tool: Tool, include_action: bool = True) -> dict:
    """Convert DB Tool to MAI-1 response dict."""
    response = {
        "identity": {
            "aid": tool.aid,
            "version": tool.version,
        },
        "logic": {
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
        },
        "trust": {
            "reliability_score": tool.reliability_score,
            "latency_ms": tool.latency_ms,
        },
    }
    if include_action:
        response["action"] = {
            "source_url": tool.source_url,
            "install_cmd": tool.install_cmd,
            "execute_cmd": tool.execute_cmd,
        }
    return response
