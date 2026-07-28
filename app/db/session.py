"""
Database Session and Engine Initialization Module.

Manages SQLAlchemy database engine creation, session factory configuration,
and PostgreSQL connection string resolution from environment variables.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker


def _database_url() -> str:
    """Construct and normalize the PostgreSQL database connection URL.

    Checks the `DATABASE_URL` environment variable and ensures the SQLAlchemy driver
    prefix `postgresql+psycopg2://` is specified. If `DATABASE_URL` is unset, constructs
    a fallback URL using individual PostgreSQL connection variables (`POSTGRES_USER`,
    `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`).

    Returns:
        str: Fully qualified PostgreSQL connection URI for SQLAlchemy.
    """
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        # Standardize postgres protocol prefixes to explicitly request psycopg2 driver
        if database_url.startswith("postgres://"):
            return database_url.replace("postgres://", "postgresql+psycopg2://", 1)
        if database_url.startswith("postgresql://") and "+psycopg2" not in database_url:
            return database_url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return database_url

    # Fallback connection string construction from standard environment variables
    return (
        "postgresql+psycopg2://"
        f"{os.getenv('POSTGRES_USER', 'postgres')}:{os.getenv('POSTGRES_PASSWORD', 'postgres')}"
        f"@{os.getenv('POSTGRES_HOST', 'localhost')}:{os.getenv('POSTGRES_PORT', '5432')}"
        f"/{os.getenv('POSTGRES_DB', 'postgres')}"
    )


# Resolved database URL for application engine initialization
DATABASE_URL = _database_url()

# SQLAlchemy engine instance configured with connection pre-ping health checks
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

# Thread-local session factory for database transaction management
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=Session)

# Base declarative class for all database ORM models
Base = declarative_base()

