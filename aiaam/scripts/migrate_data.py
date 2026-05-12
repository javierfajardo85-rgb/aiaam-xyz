"""
AIAAM — SQLite → PostgreSQL migration script.

Reads all Tool and TaxLog rows from the local SQLite database and inserts
them into the remote PostgreSQL database (Render / Railway).

Usage:
    PRODUCTION_DATABASE_URL="postgresql://user:pass@host/db" \\
        python scripts/migrate_data.py

    Or with a .env.production file:
        python scripts/migrate_data.py --env .env.production

Safety guarantees:
  • Reads are done on the local SQLite engine (never touches prod for reads).
  • Inserts use ON CONFLICT DO NOTHING so re-running is idempotent.
  • Dry-run mode prints a summary without writing anything.
"""
import sys
import os
import argparse
from pathlib import Path

# Make sure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv


def parse_args():
    parser = argparse.ArgumentParser(description="Migrate AIAAM data from SQLite to PostgreSQL")
    parser.add_argument(
        "--env",
        default=None,
        help="Path to an env file that contains PRODUCTION_DATABASE_URL (default: use shell env)",
    )
    parser.add_argument(
        "--sqlite-path",
        default=str(Path(__file__).resolve().parent.parent / "aiaam.db"),
        help="Path to local SQLite file (default: aiaam/aiaam.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be migrated without touching the remote DB",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Load extra env file if supplied
    if args.env:
        load_dotenv(args.env, override=True)

    # ── Source: local SQLite ──────────────────────────────────────────────────
    sqlite_url = f"sqlite:///{args.sqlite_path}"
    if not Path(args.sqlite_path).exists():
        print(f"ERROR: SQLite file not found: {args.sqlite_path}")
        sys.exit(1)

    # ── Destination: production PostgreSQL ────────────────────────────────────
    prod_url = os.getenv("PRODUCTION_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not prod_url:
        print(
            "ERROR: Set PRODUCTION_DATABASE_URL (or DATABASE_URL) in the environment "
            "or pass --env <file>."
        )
        sys.exit(1)

    if prod_url.startswith("postgres://"):
        prod_url = prod_url.replace("postgres://", "postgresql://", 1)

    if prod_url.startswith("sqlite"):
        print("ERROR: PRODUCTION_DATABASE_URL must point to PostgreSQL, not SQLite.")
        sys.exit(1)

    # ── Import DB objects AFTER env is configured ────────────────────────────
    from sqlmodel import Session, create_engine, select, SQLModel
    from models import Tool, TaxLog  # noqa: F401 — needed for metadata

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}AIAAM Migration: SQLite → PostgreSQL")
    print(f"  Source : {args.sqlite_path}")
    print(f"  Dest   : {prod_url[:40]}…\n")

    src_engine = create_engine(sqlite_url, echo=False, connect_args={"check_same_thread": False})

    # ── Read source data ──────────────────────────────────────────────────────
    with Session(src_engine) as src:
        tools = src.exec(select(Tool)).all()
        logs  = src.exec(select(TaxLog)).all()

    print(f"Found {len(tools)} Tool rows and {len(logs)} TaxLog rows in SQLite.")

    if args.dry_run:
        for t in tools:
            print(f"  [tool] {t.aid} | score={t.reliability_score} | translator={t.translator_used}")
        print("\nDry run complete — nothing written.")
        return

    # ── Create tables if they don't exist in the remote DB ───────────────────
    dst_engine = create_engine(prod_url, echo=False)
    SQLModel.metadata.create_all(dst_engine)

    # ── Write to destination (idempotent) ─────────────────────────────────────
    migrated_tools = 0
    migrated_logs  = 0

    with Session(dst_engine) as dst:
        for tool in tools:
            existing = dst.get(Tool, tool.aid)
            if existing is None:
                dst.add(Tool(**tool.dict()))
                migrated_tools += 1

        for log in logs:
            existing = dst.get(TaxLog, log.id)
            if existing is None:
                dst.add(TaxLog(**log.dict()))
                migrated_logs += 1

        dst.commit()

    print(f"\nMigration complete.")
    print(f"  Tools  : {migrated_tools}/{len(tools)} inserted ({len(tools) - migrated_tools} already existed)")
    print(f"  TaxLogs: {migrated_logs}/{len(logs)} inserted ({len(logs) - migrated_logs} already existed)")
    print("\nVerify at: GET /admin/stats with X-Admin-Secret header.")


if __name__ == "__main__":
    main()
