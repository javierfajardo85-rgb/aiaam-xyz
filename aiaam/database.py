"""
AIAAM Database Connection
SQLite for dev, ready to switch to PostgreSQL via DATABASE_URL.
"""
import os
from sqlmodel import SQLModel, create_engine, Session
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///aiaam.db")

# SQLite-specific args
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
