"""Data invariants and deterministic business-table fingerprints."""
import hashlib
from pathlib import Path

from etl.db import PROJECT_ROOT


def check_invariants(conn, as_of) -> dict:
    statements = {
        "raw_accounted_for": """SELECT count(*) FROM raw.prices p
            LEFT JOIN core.prices c ON c.price_id=p.id
            LEFT JOIN core.dq_quarantine_prices q ON q.price_id=p.id
            WHERE c.price_id IS NULL AND q.price_id IS NULL""",
        "core_quarantine_disjoint": """SELECT count(*) FROM core.prices c
            JOIN core.dq_quarantine_prices q USING(price_id)""",
        "all_core_have_body_group": """SELECT count(*) FROM core.prices c
            WHERE NOT EXISTS (SELECT 1 FROM core.price_body_groups b WHERE b.price_id=c.price_id)""",
        "known_fact_flags": """SELECT count(*) FROM core.prices
            WHERE is_fact IS DISTINCT FROM (price_type IN (1,2,3,4,5,9))""",
    }
    result = {name: conn.execute(sql).fetchone()[0] for name, sql in statements.items()}
    # raw_accounted_for is a full-run assertion, not appropriate during a first
    # intentionally partial backfill; callers decide whether to enforce it.
    for name, violations in result.items():
        if name != "raw_accounted_for" and violations:
            raise AssertionError(f"{name}: {violations} violations")
    conn.execute((PROJECT_ROOT / "sql/090_checks.sql").read_text())
    if conn.execute("SELECT to_regclass('mart.ml_price_predictions')").fetchone()[0]:
        future_errors = conn.execute("""SELECT count(*) FROM mart.ml_price_predictions
            WHERE loading_date > as_of AND
            (actual_price_rub IS NOT NULL OR abs_error_rub IS NOT NULL OR ape_pct IS NOT NULL)""").fetchone()[0]
        if future_errors:
            raise AssertionError(f"Future labels leaked: {future_errors}")
        result["future_labels_null"] = future_errors
    return result


def table_fingerprints(conn, include_ml: bool = False) -> dict:
    """Stream ordered rows through SHA-256. Audit timestamps are intentionally separate."""
    tables = {
        "core.dim_cities": "gc_id",
        "core.prices": "price_id",
        "core.price_body_groups": "price_id, body_group_id",
        "core.price_dates": "price_id, price_date",
        "core.dq_quarantine_prices": "price_id",
        "mart.mart_market_corridor": "month, loading_city_gcid, unloading_city_gcid, body_group_id, weight_category",
    }
    if include_ml:
        tables.update({
            "mart.ml_models": "model_id",
            "mart.ml_evaluations": "as_of, model_id",
            "mart.ml_price_predictions": "as_of, price_id",
        })
    result = {}
    with conn.transaction():
        for index, (table, order) in enumerate(tables.items()):
            digest, count = hashlib.sha256(), 0
            with conn.cursor(name=f"fingerprint_{index}") as cur:
                cur.execute(f"SELECT row_to_json(t)::text FROM (SELECT * FROM {table} ORDER BY {order}) t")
                for (row,) in cur:
                    digest.update(row.encode("utf-8")); digest.update(b"\n"); count += 1
            result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result
