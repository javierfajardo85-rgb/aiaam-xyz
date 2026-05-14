"""
C4 — Expand catalog to 100+ tools with lightweight pip-verifiable packages.

Injects 30 tools across 5 categories:
  - Developer Tools (tiktoken, rich, typer, pytest, black)
  - Data / Science (scikit-learn, scipy, pillow, openpyxl, pypdf, sympy)
  - Infrastructure (boto3, psycopg2-binary, aiohttp, websockets,
                    prometheus_client, opentelemetry-api, motor)
  - Communication APIs (slack-sdk, stripe, twilio, sendgrid, discord.py)
  - LLM / AI Ecosystem (litellm, instructor, dspy-ai, semantic-kernel,
                         outlines, marvin, guidance)

Also fixes:
  - mongo-python-driver-v1: install_cmd → "pip install pymongo"
  - swarm-v1:               install_cmd → "pip install openai-swarm"

All verified=None so sandbox_sanitizer runs the triple check.
No LLM calls. No cost.
"""

import sys
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool
from sandbox_sanitizer import run as sanitizer_run


def _tool(aid, source_url, install_cmd, input_schema, output_schema,
          reliability_score=0.85, platform="pypi", version="latest"):
    return Tool(
        aid=aid,
        version=version,
        input_schema=input_schema,
        output_schema=output_schema,
        reliability_score=reliability_score,
        latency_ms=None,
        source_url=source_url,
        install_cmd=install_cmd,
        source_platform=platform,
        translator_used="mapped",
        verified=None,
    )


# ── 5 Developer Tools ────────────────────────────────────────────────────
DEVELOPER_TOOLS = [
    _tool(
        "tiktoken-v1",
        "https://pypi.org/project/tiktoken/",
        "pip install tiktoken",
        {"type": "string", "format": "text"},
        {"type": "integer", "format": "token_count"},
        reliability_score=0.95,
    ),
    _tool(
        "rich-v1",
        "https://pypi.org/project/rich/",
        "pip install rich",
        {"type": "any", "format": "python_object"},
        {"type": "string", "format": "terminal_markup"},
        reliability_score=0.95,
    ),
    _tool(
        "typer-v1",
        "https://pypi.org/project/typer/",
        "pip install typer",
        {"type": "function", "format": "python_callable"},
        {"type": "object", "format": "cli_app"},
        reliability_score=0.95,
    ),
    _tool(
        "pytest-v1",
        "https://pypi.org/project/pytest/",
        "pip install pytest",
        {"type": "function", "format": "test_function"},
        {"type": "object", "format": "test_result"},
        reliability_score=0.97,
    ),
    _tool(
        "black-v1",
        "https://pypi.org/project/black/",
        "pip install black",
        {"type": "string", "format": "python_source"},
        {"type": "string", "format": "formatted_python_source"},
        reliability_score=0.97,
    ),
]

# ── 6 Data / Science ─────────────────────────────────────────────────────
DATA_SCIENCE_TOOLS = [
    _tool(
        "scikit-learn-v1",
        "https://pypi.org/project/scikit-learn/",
        "pip install scikit-learn",
        {"type": "array", "format": "numeric_2d"},
        {"type": "object", "format": "model_or_array"},
        reliability_score=0.97,
    ),
    _tool(
        "scipy-v1",
        "https://pypi.org/project/scipy/",
        "pip install scipy",
        {"type": "array", "format": "numeric"},
        {"type": "array", "format": "numeric"},
        reliability_score=0.97,
    ),
    _tool(
        "pillow-v1",
        "https://pypi.org/project/pillow/",
        "pip install pillow",
        {"type": "string", "format": "image_path_or_bytes"},
        {"type": "object", "format": "pil_image"},
        reliability_score=0.97,
    ),
    _tool(
        "openpyxl-v1",
        "https://pypi.org/project/openpyxl/",
        "pip install openpyxl",
        {"type": "string", "format": "xlsx_path"},
        {"type": "object", "format": "workbook"},
        reliability_score=0.95,
    ),
    _tool(
        "pypdf-v1",
        "https://pypi.org/project/pypdf/",
        "pip install pypdf",
        {"type": "string", "format": "pdf_path_or_bytes"},
        {"type": "string", "format": "extracted_text"},
        reliability_score=0.93,
    ),
    _tool(
        "sympy-v1",
        "https://pypi.org/project/sympy/",
        "pip install sympy",
        {"type": "string", "format": "math_expression"},
        {"type": "any", "format": "symbolic_result"},
        reliability_score=0.95,
    ),
]

# ── 7 Infrastructure ─────────────────────────────────────────────────────
INFRA_TOOLS = [
    _tool(
        "boto3-v1",
        "https://pypi.org/project/boto3/",
        "pip install boto3",
        {"type": "object", "format": "aws_config"},
        {"type": "object", "format": "aws_response"},
        reliability_score=0.95,
    ),
    _tool(
        "psycopg2-binary-v1",
        "https://pypi.org/project/psycopg2-binary/",
        "pip install psycopg2-binary",
        {"type": "string", "format": "sql_query"},
        {"type": "array", "format": "query_results"},
        reliability_score=0.93,
    ),
    _tool(
        "aiohttp-v1",
        "https://pypi.org/project/aiohttp/",
        "pip install aiohttp",
        {"type": "string", "format": "url"},
        {"type": "object", "format": "http_response"},
        reliability_score=0.95,
    ),
    _tool(
        "websockets-v1",
        "https://pypi.org/project/websockets/",
        "pip install websockets",
        {"type": "string", "format": "ws_url"},
        {"type": "object", "format": "websocket_connection"},
        reliability_score=0.93,
    ),
    _tool(
        "prometheus-client-v1",
        "https://pypi.org/project/prometheus-client/",
        "pip install prometheus-client",
        {"type": "object", "format": "metric_definition"},
        {"type": "string", "format": "prometheus_exposition"},
        reliability_score=0.93,
    ),
    _tool(
        "opentelemetry-api-v1",
        "https://pypi.org/project/opentelemetry-api/",
        "pip install opentelemetry-api",
        {"type": "object", "format": "span_config"},
        {"type": "object", "format": "trace"},
        reliability_score=0.93,
    ),
    _tool(
        "motor-v1",
        "https://pypi.org/project/motor/",
        "pip install motor",
        {"type": "object", "format": "mongodb_query"},
        {"type": "object", "format": "document"},
        reliability_score=0.93,
    ),
]

# ── 5 Communication APIs ─────────────────────────────────────────────────
COMM_TOOLS = [
    _tool(
        "slack-sdk-v1",
        "https://pypi.org/project/slack-sdk/",
        "pip install slack-sdk",
        {"type": "object", "format": "slack_message"},
        {"type": "object", "format": "slack_response"},
        reliability_score=0.93,
    ),
    _tool(
        "stripe-v1",
        "https://pypi.org/project/stripe/",
        "pip install stripe",
        {"type": "object", "format": "payment_intent"},
        {"type": "object", "format": "payment_result"},
        reliability_score=0.95,
    ),
    _tool(
        "twilio-v1",
        "https://pypi.org/project/twilio/",
        "pip install twilio",
        {"type": "object", "format": "sms_or_call"},
        {"type": "object", "format": "message_sid"},
        reliability_score=0.93,
    ),
    _tool(
        "sendgrid-v1",
        "https://pypi.org/project/sendgrid/",
        "pip install sendgrid",
        {"type": "object", "format": "email_message"},
        {"type": "object", "format": "send_confirmation"},
        reliability_score=0.93,
    ),
    _tool(
        "discord.py-v1",
        "https://pypi.org/project/discord.py/",
        "pip install discord.py",
        {"type": "object", "format": "discord_message"},
        {"type": "object", "format": "post_confirmation"},
        reliability_score=0.90,
    ),
]

# ── 7 LLM / AI Ecosystem ─────────────────────────────────────────────────
LLM_TOOLS = [
    _tool(
        "litellm-v1",
        "https://pypi.org/project/litellm/",
        "pip install litellm",
        {"type": "string", "format": "prompt"},
        {"type": "string", "format": "completion"},
        reliability_score=0.93,
    ),
    _tool(
        "instructor-v1",
        "https://pypi.org/project/instructor/",
        "pip install instructor",
        {"type": "object", "format": "prompt_and_schema"},
        {"type": "object", "format": "structured_output"},
        reliability_score=0.93,
    ),
    _tool(
        "dspy-ai-v1",
        "https://pypi.org/project/dspy-ai/",
        "pip install dspy-ai",
        {"type": "object", "format": "dspy_signature"},
        {"type": "object", "format": "prediction"},
        reliability_score=0.90,
    ),
    _tool(
        "semantic-kernel-v1",
        "https://pypi.org/project/semantic-kernel/",
        "pip install semantic-kernel",
        {"type": "string", "format": "prompt_template"},
        {"type": "string", "format": "completion"},
        reliability_score=0.90,
    ),
    _tool(
        "outlines-v1",
        "https://pypi.org/project/outlines/",
        "pip install outlines",
        {"type": "object", "format": "schema_and_model"},
        {"type": "string", "format": "structured_text"},
        reliability_score=0.88,
    ),
    _tool(
        "marvin-v1",
        "https://pypi.org/project/marvin/",
        "pip install marvin",
        {"type": "function", "format": "ai_annotated_function"},
        {"type": "any", "format": "ai_result"},
        reliability_score=0.88,
    ),
    _tool(
        "guidance-v1",
        "https://pypi.org/project/guidance/",
        "pip install guidance",
        {"type": "string", "format": "guidance_template"},
        {"type": "string", "format": "completion"},
        reliability_score=0.88,
    ),
]

ALL_NEW_TOOLS = (
    DEVELOPER_TOOLS
    + DATA_SCIENCE_TOOLS
    + INFRA_TOOLS
    + COMM_TOOLS
    + LLM_TOOLS
)


def step0_fix_install_cmds(session: Session) -> None:
    """Fix known bad install_cmds and reset idealTree npm failures to None."""
    print("\n[C4] STEP 0 — fix install_cmds + reset npm idealTree failures")

    # Fix install_cmds that didn't match sandbox allowlist
    cmd_fixes = {
        "mongo-python-driver-v1": "pip install pymongo",
        "swarm-v1":               "pip install openai-swarm",
        # Remove --save flag that conflicts with --no-save in Docker command
        "lmstudio-js-v1":         "npm install @lmstudio/sdk",
    }
    for aid, new_cmd in cmd_fixes.items():
        tool = session.get(Tool, aid)
        if tool and tool.install_cmd != new_cmd:
            tool.install_cmd = new_cmd
            tool.verified = None
            tool.updated_at = datetime.utcnow()
            session.add(tool)
            print(f"  {aid}: install_cmd → {new_cmd}")
        elif tool:
            print(f"  {aid}: already correct")

    # Reset npm tools that failed due to idealTree (not a real install failure).
    # They will now be verified with --ignore-scripts which skips the nested npm
    # postinstall call that triggers the error.
    npm_idealTree_failures = [
        "playwright-v1",
        "puppeteer-v1",
        "letta-v1",
        "n8n-v1",
        "flowise-v1",
        "npm-anthropic-ai-sdk-v1",
    ]
    for aid in npm_idealTree_failures:
        tool = session.get(Tool, aid)
        if tool and tool.verified is False:
            tool.verified = None
            tool.updated_at = datetime.utcnow()
            session.add(tool)
            print(f"  {aid}: verified=False → None (idealTree reset)")

    session.commit()


def step1_inject_tools(session: Session) -> dict:
    """Inject 30 new tools. Skip if aid already exists."""
    print("\n[C4] STEP 1 — inject 30 lightweight tools")
    results = {"added": 0, "skipped": 0}

    for tool in ALL_NEW_TOOLS:
        existing = session.get(Tool, tool.aid)
        if existing:
            print(f"  SKIP {tool.aid} — already in catalog")
            results["skipped"] += 1
            continue
        tool.created_at = datetime.utcnow()
        tool.updated_at = datetime.utcnow()
        session.add(tool)
        print(f"  + {tool.aid}")
        results["added"] += 1

    session.commit()
    return results


def step2_sanitize() -> dict:
    """Triple validation on all verified=None tools."""
    print("\n[C4] STEP 2 — sandbox sanitizer on verified=None")
    return sanitizer_run(reverify_all=False, dry_run=False)


def run(skip_sanitize: bool = False):
    init_db()
    print("=" * 60)
    print("C4 — EXPAND CATALOG TO 100+ TOOLS")
    print("=" * 60)

    with Session(engine) as session:
        step0_fix_install_cmds(session)
        r1 = step1_inject_tools(session)

    print(f"\n[C4] injection — added={r1['added']} skipped={r1['skipped']}")

    if skip_sanitize:
        print("[C4] --skip-sanitize: skipping Docker sandbox phase")
        return

    r2 = step2_sanitize()

    with Session(engine) as session:
        from sqlmodel import select as sel
        all_t = session.exec(sel(Tool)).all()
        verified = [t for t in all_t if t.verified is True]
        print(f"\n{'='*60}")
        print(f"C4 RESULT")
        print(f"  Total tools:        {len(all_t)}")
        print(f"  verified=True:      {len(verified)}")
        print(f"  sandbox passed:     {r2['passed']}")
        print(f"  sandbox failed:     {r2['failed']}")
        print(f"  sandbox unver:      {r2['unverifiable']}")
        print(f"{'='*60}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-sanitize", action="store_true")
    args = parser.parse_args()
    run(skip_sanitize=args.skip_sanitize)
