"""
Push compiled_apis from local SQLite → Railway PostgreSQL
via POST /admin/ingest-compiled-api.

Uso:
    python3 scripts/push_compiled_apis.py
    python3 scripts/push_compiled_apis.py --dry-run
"""
import sys, argparse, time, requests
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
import os
load_dotenv()

from sqlmodel import Session, select
from database import engine, init_db
from models import CompiledAPI

PRODUCTION_URL = "https://aiaam.xyz"
ADMIN_SECRET   = os.getenv("ADMIN_SECRET", "")


def main(dry_run=False):
    init_db()
    with Session(engine) as session:
        records = session.exec(select(CompiledAPI).order_by(CompiledAPI.compiled_at)).all()

    print(f"[push] {len(records)} compiled_apis → {PRODUCTION_URL}")
    if not ADMIN_SECRET:
        print("[push] ERROR: ADMIN_SECRET not set"); return

    ok = failed = 0
    for r in records:
        if dry_run:
            print(f"  DRY  {r.service_name} ({r.category})")
            ok += 1
            continue
        try:
            resp = requests.post(
                f"{PRODUCTION_URL}/admin/ingest-compiled-api",
                json={
                    "service_name":      r.service_name,
                    "category":          r.category,
                    "source_url":        r.source_url,
                    "manifest":          r.manifest,
                    "reliability_score": r.reliability_score,
                    "tokens_used":       r.tokens_used,
                    "verified":          r.verified,
                },
                headers={"X-Admin-Secret": ADMIN_SECRET},
                timeout=15,
            )
            if resp.status_code == 200:
                action = resp.json().get("action", "?")
                print(f"  ✅  {r.service_name} ({action})")
                ok += 1
            else:
                print(f"  ❌  {r.service_name} — {resp.status_code}: {resp.text[:100]}")
                failed += 1
        except Exception as e:
            print(f"  ❌  {r.service_name} — {e}")
            failed += 1
        time.sleep(0.3)

    print(f"\n[push] done — ok={ok} failed={failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
