"""
push_to_production.py — Sync local SQLite catalog → Railway PostgreSQL
via POST /api/v1/ingest (admin endpoint).

Uso:
    python3 push_to_production.py
    python3 push_to_production.py --dry-run    # preview sin enviar
    python3 push_to_production.py --only-verified  # solo los verified=True
"""

import sys
import time
import argparse
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlmodel import Session, select
from database import engine, init_db
from models import Tool
from dotenv import load_dotenv
import os

load_dotenv()

PRODUCTION_URL = "https://aiaam.xyz"
ADMIN_SECRET   = os.getenv("ADMIN_SECRET", "")


def push_tool(tool: Tool, dry_run: bool = False) -> bool:
    payload = {
        "aid":              tool.aid,
        "version":          tool.version,
        "input_schema":     tool.input_schema,
        "output_schema":    tool.output_schema,
        "reliability_score": tool.reliability_score,
        "latency_ms":       tool.latency_ms,
        "source_url":       tool.source_url,
        "install_cmd":      tool.install_cmd,
        "execute_cmd":      tool.execute_cmd,
        "source_platform":  tool.source_platform,
        "translator_used":  tool.translator_used,
        "foam_score":       tool.foam_score,
        "verified":         tool.verified,
        "status":           tool.status,
        "health_score":     tool.health_score,
        "affiliate_tag":    tool.affiliate_tag,
        "monetizable":      tool.monetizable or False,
    }

    if dry_run:
        print(f"  DRY  {tool.aid} (verified={tool.verified})")
        return True

    try:
        r = requests.post(
            f"{PRODUCTION_URL}/api/v1/ingest",
            json=payload,
            headers={"X-Admin-Secret": ADMIN_SECRET},
            timeout=15,
        )
        if r.status_code == 200:
            print(f"  OK   {tool.aid} (verified={tool.verified})")
            return True
        else:
            print(f"  FAIL {tool.aid} — {r.status_code}: {r.text[:120]}")
            return False
    except Exception as exc:
        print(f"  ERR  {tool.aid} — {exc}")
        return False


def run(dry_run: bool = False, only_verified: bool = False):
    init_db()
    with Session(engine) as session:
        stmt = select(Tool)
        if only_verified:
            stmt = stmt.where(Tool.verified == True)
        tools = session.exec(stmt.order_by(Tool.aid)).all()

    print(f"\n[push] {len(tools)} tools → {PRODUCTION_URL}")
    if dry_run:
        print("[push] DRY RUN — nothing will be sent\n")

    ok = fail = 0
    for tool in tools:
        if push_tool(tool, dry_run=dry_run):
            ok += 1
        else:
            fail += 1
        if not dry_run:
            time.sleep(0.15)  # gentle rate limit

    print(f"\n[push] done — ok={ok} fail={fail}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-verified", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run, only_verified=args.only_verified)
