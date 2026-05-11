"""
AIAAM Analytics
Records every AI transaction. Drives reliability_score updates and intelligence reports.
"""
from datetime import datetime, timedelta
from typing import Optional
from sqlmodel import Session, select, func

from models import TaxLog, Tool, TaxPayload


# Estimated tokens an AI saves by using AIAAM instead of reading source directly
DEFAULT_TOKENS_SAVED = 4800


def log_transaction(
    session: Session,
    tool_aid: str,
    user_agent: str,
    payload: Optional[TaxPayload],
    response_status: int,
    latency_ms: int,
    tokens_saved: int = DEFAULT_TOKENS_SAVED,
) -> TaxLog:
    """Persist a single AI transaction."""
    log = TaxLog(
        tool_aid=tool_aid,
        user_agent=user_agent[:255] if user_agent else "unknown",
        trend_keyword=payload.trend_keyword if payload else None,
        execution_feedback=payload.execution_feedback if payload else None,
        validation_bit=payload.validation_bit if payload else None,
        micro_translation=payload.micro_translation if payload else None,
        referral_included=payload.referral_included if payload else False,
        response_status=response_status,
        tokens_saved_estimate=tokens_saved,
        latency_ms=latency_ms,
        timestamp=datetime.utcnow(),
    )
    session.add(log)

    # Update reliability_score on the Tool in real time
    if payload and payload.execution_feedback is not None:
        tool = session.get(Tool, tool_aid)
        if tool:
            tool.total_requests += 1
            if 200 <= payload.execution_feedback < 300:
                tool.successful_executions += 1
            else:
                tool.failed_executions += 1
            # Bayesian-ish update — gradual movement toward observed rate
            total = tool.successful_executions + tool.failed_executions
            if total > 0:
                observed_rate = tool.successful_executions / total
                # Weighted blend: 70% history, 30% observed (smooth)
                tool.reliability_score = round(
                    0.7 * tool.reliability_score + 0.3 * observed_rate, 4
                )
            tool.updated_at = datetime.utcnow()
            session.add(tool)

    session.commit()
    session.refresh(log)
    return log


def get_stats(session: Session) -> dict:
    """Return analytics summary for admin dashboard."""
    now = datetime.utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    # Total counts
    total_logs = session.exec(select(func.count(TaxLog.id))).one()
    total_tools = session.exec(select(func.count(Tool.aid))).one()

    # Last 24h
    logs_24h = session.exec(
        select(func.count(TaxLog.id)).where(TaxLog.timestamp >= day_ago)
    ).one()

    # Last 7 days
    logs_7d = session.exec(
        select(func.count(TaxLog.id)).where(TaxLog.timestamp >= week_ago)
    ).one()

    # Tokens saved total (estimated value to the network)
    tokens_total = session.exec(
        select(func.sum(TaxLog.tokens_saved_estimate))
    ).one() or 0

    # Top User-Agents
    top_agents_rows = session.exec(
        select(TaxLog.user_agent, func.count(TaxLog.id).label("count"))
        .group_by(TaxLog.user_agent)
        .order_by(func.count(TaxLog.id).desc())
        .limit(10)
    ).all()
    top_agents = [{"user_agent": ua, "requests": c} for ua, c in top_agents_rows]

    # Top trending keywords
    top_trends_rows = session.exec(
        select(TaxLog.trend_keyword, func.count(TaxLog.id).label("count"))
        .where(TaxLog.trend_keyword.is_not(None))
        .group_by(TaxLog.trend_keyword)
        .order_by(func.count(TaxLog.id).desc())
        .limit(10)
    ).all()
    top_trends = [{"keyword": k, "count": c} for k, c in top_trends_rows]

    # Success vs failure ratio
    success = session.exec(
        select(func.count(TaxLog.id)).where(
            TaxLog.execution_feedback >= 200, TaxLog.execution_feedback < 300
        )
    ).one()
    failures = session.exec(
        select(func.count(TaxLog.id)).where(TaxLog.execution_feedback >= 400)
    ).one()

    # Most reliable tools
    top_tools_rows = session.exec(
        select(Tool)
        .where(Tool.total_requests > 0)
        .order_by(Tool.reliability_score.desc())
        .limit(10)
    ).all()
    top_tools = [
        {
            "aid": t.aid,
            "reliability_score": t.reliability_score,
            "total_requests": t.total_requests,
        }
        for t in top_tools_rows
    ]

    return {
        "totals": {
            "tools_in_catalog": total_tools,
            "total_transactions": total_logs,
            "transactions_last_24h": logs_24h,
            "transactions_last_7d": logs_7d,
            "estimated_tokens_saved": tokens_total,
        },
        "execution": {
            "successful": success,
            "failed": failures,
        },
        "top_user_agents": top_agents,
        "top_trends": top_trends,
        "top_reliable_tools": top_tools,
    }
