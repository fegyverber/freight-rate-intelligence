"""Connection configuration for both Compose and an existing local PostgreSQL."""
import os
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def database_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    password = os.environ.get("PGPASSWORD")
    password_file = os.environ.get("POSTGRES_PASSWORD_FILE")
    if password_file:
        password = Path(password_file).read_text().strip()
    args = {"host": os.environ.get("PGHOST", "db"),
            "port": os.environ.get("PGPORT", "5432"),
            "dbname": os.environ.get("PGDATABASE", "deals_bigdata"),
            "user": os.environ.get("PGUSER", "logistics"),
            "connect_timeout": "15"}
    if password is not None:
        args["password"] = password
    return make_conninfo(**args)


def connect():
    return psycopg.connect(database_url(), autocommit=True)


def apply_migrations(conn) -> None:
    """DDL is repeatable; transaction boundaries prevent half-created schemas."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('smart_logistics.pipeline'))")
        for name in ("001_core.sql", "010_load_core.sql", "020_mart.sql"):
            conn.execute((PROJECT_ROOT / "sql" / name).read_text())
