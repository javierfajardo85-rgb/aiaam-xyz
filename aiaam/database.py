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
    """Create all tables. Called once at startup."""
    SQLModel.metadata.create_all(engine)


def get_session():
    """FastAPI dependency for DB sessions."""
    with Session(engine) as session:
        yield session
