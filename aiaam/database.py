"""
AIAAM Database Connection
SQLite for dev, PostgreSQL for production (Render / Railway).

DATABASE_URL resolution order:
  1. DATABASE_URL env var (set by Render automatically for PostgreSQL)
  2. Falls back to local SQLite (aiaam.db)

Render injects postgres:// URLs; SQLAlchemy 2.x requires postgresql://.
We normalise here so the rest of the code never has to think about it.
"""
import os
from sqlmodel import SQLModel, create_engine, Session
from dotenv import load_dotenv

load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///aiaam.db")

# Render (and older Heroku) emit postgres:// — SQLAlchemy 2.x requires postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# SQLite needs check_same_thread=False; PostgreSQL needs nothing extra
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args,
)


def init_db():
    """Create all tables and apply incremental column migrations."""
    SQLModel.metadata.create_all(engine)
    _migrate_columns()


def _migrate_columns():
    """
    Adds new columns to existing tables without Alembic.
    Each ALTER TABLE is wrapped in a try/except so it is safe to run
    repeatedly — the DB engine raises an error if the column already exists,
    which we silently swallow.
    """
    is_sqlite = DATABASE_URL.startswith("sqlite")
    migrations = [
        # table,            column,                    sql_type
        ("tools",           "foam_score",              "INTEGER"),
        ("tools",           "verified",                "BOOLEAN"),
        ("tools",           "suggested_workflow",      "JSON"),
        ("tools",           "status",                  "VARCHAR"),
        ("tax_logs",        "validation_vote",         "VARCHAR"),
        ("tax_logs",        "validation_candidate_aid","VARCHAR"),
        ("tax_logs",        "referral_confirmed",      "BOOLEAN"),
        # injected_repos se crea completa vía create_all; no necesita ALTER TABLE
    ]
    with engine.begin() as conn:
        for table, column, sql_type in migrations:
            try:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                )
            except Exception:
                pass  # column already exists — safe to ignore


def get_session():
    """FastAPI dependency for DB sessions."""
    with Session(engine) as session:
        yield session
