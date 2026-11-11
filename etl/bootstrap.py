"""Load the unchanged assignment generator once, in one psql transaction."""
import os
import subprocess
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from etl.db import PROJECT_ROOT, database_url
from etl.logging import log_event


def bootstrap_source(conn, n_prices: int = 300_000) -> dict:
    if n_prices < 1:
        raise ValueError("N_PRICES must be positive")
    # Session-level lock spans the child psql process as well.
    conn.execute("SELECT pg_advisory_lock(hashtext('smart_logistics.bootstrap'))")
    try:
        marker = conn.execute("SELECT to_regclass('raw.dataset_manifest')").fetchone()[0]
        if marker:
            saved = conn.execute("SELECT n_prices FROM raw.dataset_manifest WHERE id = 1").fetchone()
            if saved:
                if saved[0] != n_prices:
                    raise ValueError(f"Existing source has N_PRICES={saved[0]}, requested {n_prices}; use a new database/volume")
                log_event("source_reused", n_prices=saved[0])
                return {"status": "reused", "n_prices": saved[0]}
        if conn.execute("SELECT to_regclass('raw.prices')").fetchone()[0]:
            raise ValueError("raw.prices exists without dataset manifest; refusing to overwrite an unknown source")

        config = conninfo_to_dict(database_url())
        env = os.environ.copy()
        for key, target in (("host", "PGHOST"), ("port", "PGPORT"), ("dbname", "PGDATABASE"),
                            ("user", "PGUSER"), ("password", "PGPASSWORD"), ("sslmode", "PGSSLMODE")):
            if key in config:
                env[target] = config[key]
        # No DSN/password appears in the process arguments or log.
        cmd = ["psql", "-X", "--set=ON_ERROR_STOP=1", "--single-transaction",
               f"--set=n_prices={n_prices}",
               "-c", "CREATE SCHEMA IF NOT EXISTS raw; SET search_path TO raw, public;",
               "-f", str(PROJECT_ROOT / "sql/source/schema.sql"),
               "-f", str(PROJECT_ROOT / "sql/source/synthetic.sql"),
               "-c", "CREATE TABLE raw.dataset_manifest(id integer PRIMARY KEY CHECK(id=1), "
               "n_prices bigint NOT NULL, snapshot_date date NOT NULL, seed double precision NOT NULL); "
               f"INSERT INTO raw.dataset_manifest VALUES (1, {n_prices}, DATE '2026-10-26', 0.20261026); "
               "CREATE INDEX prices_loading_date_idx ON raw.prices(loading_date); "
               "CREATE INDEX price_dates_date_idx ON raw.price_dates(price_date);"]
        result = subprocess.run(cmd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError(f"Source bootstrap failed:\n{result.stderr[-6000:]}")
        log_event("source_initialized", n_prices=n_prices)
        return {"status": "created", "n_prices": n_prices}
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext('smart_logistics.bootstrap'))")
