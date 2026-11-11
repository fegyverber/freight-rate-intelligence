"""CLI: python -m etl.pipeline all|etl|mart|ml|check|verify-idempotency."""
import argparse
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from etl.bootstrap import bootstrap_source
from etl.checks import check_invariants, table_fingerprints
from etl.db import apply_migrations, connect
from etl.logging import log_event


def load_and_refresh(conn, start_date: date, end_date: date, as_of: date) -> dict:
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        result = conn.execute("SELECT core.load_prices(%s, %s)", (start_date, end_date)).fetchone()[0]
        result["mart_rows"] = conn.execute("SELECT mart.refresh_market_corridor(%s)", (as_of,)).fetchone()[0]
    log_event("etl_completed", start_date=start_date, end_date=end_date, **result)
    return result


def resolve_window(conn, start_date=None, end_date=None):
    if (start_date is None) != (end_date is None):
        raise ValueError("Specify both --start-date and --end-date; end date is exclusive")
    if start_date is None:
        lo, hi = conn.execute("""SELECT min(d), max(d) FROM (
            SELECT loading_date AS d FROM raw.prices
            UNION ALL SELECT loading_date FROM core.prices
            UNION ALL SELECT loading_date FROM core.dq_quarantine_prices) dates""").fetchone()
        if lo is None:
            raise ValueError("Source is empty")
        start_date, end_date = lo, hi + timedelta(days=1)
    if start_date >= end_date:
        raise ValueError("Expected start_date < end_date")
    return start_date, end_date


def atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    temp.replace(path)


def run_once(args) -> dict:
    run_id = uuid.uuid4()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        version = int(conn.execute("SHOW server_version_num").fetchone()[0])
        if version // 10000 != 17:
            raise RuntimeError(f"This assignment requires PostgreSQL 17, found server_version_num={version}")
        conn.execute("SELECT pg_advisory_lock(hashtext('smart_logistics.pipeline'))")
        try:
            if args.command in ("all", "init"):
                bootstrap_source(conn, args.n_prices)
            apply_migrations(conn)
            conn.execute("""INSERT INTO ops.pipeline_runs
              (run_id,command,start_date,end_date,as_of,started_at,status)
              VALUES (%s,%s,%s,%s,%s,%s,'RUNNING')""",
              (run_id,args.command,args.start_date,args.end_date,args.as_of,datetime.now(timezone.utc)))
            result = {"run_id": str(run_id), "command": args.command, "as_of": str(args.as_of)}
            if args.command in ("all", "etl", "verify-idempotency"):
                start, end = resolve_window(conn, args.start_date, args.end_date)
                result["window"] = {"start_date": str(start), "end_date_exclusive": str(end)}
                result["etl"] = load_and_refresh(conn, start, end, args.as_of)
                if args.command == "verify-idempotency":
                    if args.include_ml:
                        from ml.pipeline import run_ml
                        result["ml"] = run_ml(conn, args.as_of, output_dir,
                                              args.seed, args.iterations, args.thread_count)
                    before = table_fingerprints(conn, include_ml=args.include_ml)
                    load_and_refresh(conn, start, end, args.as_of)
                    if args.include_ml:
                        result["ml"] = run_ml(conn, args.as_of, output_dir,
                                              args.seed, args.iterations, args.thread_count)
                    after = table_fingerprints(conn, include_ml=args.include_ml)
                    if before != after:
                        raise AssertionError("Business table fingerprints changed on identical rerun")
                    result["idempotency"] = {"passed": True, "includes_ml": args.include_ml,
                                             "fingerprints": after}
            if args.command == "mart":
                with conn.transaction():
                    result["mart_rows"] = conn.execute("SELECT mart.refresh_market_corridor(%s)", (args.as_of,)).fetchone()[0]
            if args.command in ("all", "ml"):
                from ml.pipeline import run_ml
                result["ml"] = run_ml(conn, as_of=args.as_of, output_dir=output_dir,
                                       seed=args.seed, iterations=args.iterations,
                                       thread_count=args.thread_count)
            if args.command in ("all", "check", "verify-idempotency"):
                result["checks"] = check_invariants(conn, args.as_of)
                if args.command == "all" and args.start_date is None and result["checks"]["raw_accounted_for"]:
                    raise AssertionError("Full load did not account for every source price")
            conn.execute("""UPDATE ops.pipeline_runs SET finished_at=now(),status='SUCCEEDED',result=%s
              WHERE run_id=%s""", (Jsonb(result, dumps=lambda obj: json.dumps(obj, default=str)), run_id))
            atomic_json(output_dir / f"{args.command}_result.json", result)
            log_event("pipeline_succeeded", **result)
            return result
        except Exception as exc:
            if conn.execute("SELECT to_regclass('ops.pipeline_runs')").fetchone()[0]:
                conn.execute("""UPDATE ops.pipeline_runs SET finished_at=now(),status='FAILED',error=%s
                  WHERE run_id=%s""", (str(exc)[:4000], run_id))
            log_event("pipeline_failed", run_id=str(run_id), error=str(exc))
            raise
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('smart_logistics.pipeline'))")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="all",
                        choices=["all", "init", "etl", "mart", "ml", "check", "verify-idempotency"])
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat, help="Exclusive end of the loading_date window")
    parser.add_argument("--as-of", type=date.fromisoformat, default=os.getenv("AS_OF", "2026-10-26"))
    parser.add_argument("--n-prices", type=int, default=int(os.getenv("N_PRICES", "300000")))
    parser.add_argument("--seed", type=int, default=int(os.getenv("RANDOM_SEED", "20261026")))
    parser.add_argument("--iterations", type=int, default=int(os.getenv("ML_ITERATIONS", "500")))
    parser.add_argument("--thread-count", type=int, default=int(os.getenv("ML_THREAD_COUNT", "2")))
    parser.add_argument("--output-dir", default=os.getenv("ARTIFACT_DIR", "artifacts"))
    parser.add_argument("--interval-seconds", type=int, default=int(os.getenv("RUN_INTERVAL_SECONDS", "0")))
    parser.add_argument("--include-ml", action="store_true",
                        help="Also rerun and fingerprint ML when verifying idempotency")
    args = parser.parse_args()
    if args.interval_seconds < 0:
        parser.error("--interval-seconds must be non-negative")
    if args.include_ml and args.command != "verify-idempotency":
        parser.error("--include-ml is only valid with verify-idempotency")
    while True:
        run_once(args)
        if not args.interval_seconds:
            break
        log_event("waiting_for_next_run", interval_seconds=args.interval_seconds)
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
