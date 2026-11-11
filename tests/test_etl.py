"""DQ and reconciliation tests exercising core.load_prices on PostgreSQL 17.

Fixtures live in July 2099, outside the supplied snapshot, and use explicit
reserved IDs (including serial-backed source tables). Everything is rolled
back, including source corrections and the dimension refresh. All ordinary
cases are loaded together; mutation tests use rollback savepoints.
"""

from datetime import date, datetime
from decimal import Decimal
import os

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.rows import dict_row


pytestmark = pytest.mark.integration
START = date(2099, 7, 1)
END = date(2099, 8, 1)
LOADING = date(2099, 7, 10)
PRICE_BASE = 8_000_000_000_000
CITY_BASE = 2_100_000_000


def load(conn, start=START, end=END):
    return conn.execute(
        "SELECT core.load_prices(%s, %s) AS stats", (start, end)
    ).fetchone()["stats"]


def snapshot(conn):
    result = {}
    for table, order in [
        ("prices", "price_id"),
        ("dq_quarantine_prices", "price_id"),
        ("price_body_groups", "price_id, body_group_id"),
        ("price_dates", "price_id, price_date"),
    ]:
        result[table] = conn.execute(
            f"SELECT * FROM core.{table} WHERE price_id > %s AND price_id < %s ORDER BY {order}",
            (PRICE_BASE, PRICE_BASE + 10_000),
        ).fetchall()
    result["dim_cities"] = conn.execute(
        "SELECT * FROM core.dim_cities WHERE gc_id BETWEEN %s AND %s ORDER BY gc_id",
        (CITY_BASE, CITY_BASE + 10),
    ).fetchall()
    return result


@pytest.fixture(scope="module")
def etl_data():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('smart_logistics.pipeline'))")
        if conn.execute("SELECT to_regprocedure('core.load_prices(date,date)') AS name").fetchone()["name"] is None:
            pytest.fail("Initialize raw/core schemas and the ETL function before integration tests")

        # Insert the duplicate BEFORE the canonical record: insertion order must
        # not choose the city; the smallest source id must choose it.
        cities = [
            (PRICE_BASE + 102, CITY_BASE + 1, "Вариант города, Другой регион"),
            (PRICE_BASE + 101, CITY_BASE + 1, "Канонический город, Канонический регион"),
            (PRICE_BASE + 103, CITY_BASE + 2, "Город назначения, Регион назначения"),
            (PRICE_BASE + 104, CITY_BASE + 3, "Город без региона"),
        ]
        for source_id, gc_id, name in cities:
            conn.execute(
                """INSERT INTO raw.cities
                   (id, gc_id, name, full_name_local, full_name_gc, latitude, longitude)
                   VALUES (%s, %s, %s, %s, %s, 55.5, 37.5)""",
                (source_id, gc_id, name.split(",")[0], name, name),
            )
        for body_id, std_id in [(56, 56), (57, 56), (61, 56), (1, 15), (15, 15), (4, 4), (3, 3)]:
            conn.execute(
                """INSERT INTO raw.body_types (id, name, "id_from_1C", std_id)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                (body_id, f"Test body {body_id}", body_id, std_id),
            )

        rates = [
            ("840", 80.0, 2), ("840", 80.0, 2),  # identical duplicates are safe
            ("840", 80.0, 3), ("840", 82.0, 3),  # contradictory exact quotes
            ("978", 99.0, 4),
            ("840", 90.0, 5), ("840", 95.0, 7),
            ("840", 100.0, 10),  # would be an incorrect loading-date fallback
            ("978", 0.0, 8), ("978", -1.0, 9),
            ("978", float("nan"), 11), ("978", float("inf"), 12),
            ("840", 0.000001, 13),
            ("643", 0.0, 3), ("643", 100.0, 3),  # RUB is exactly 1 independently
        ]
        for offset, (code, value, day) in enumerate(rates, 1):
            conn.execute(
                """INSERT INTO raw.exchange_rates (id, num_code, value, setting_date)
                   VALUES (%s, %s, %s, %s)""",
                (PRICE_BASE + offset, code, value, date(2099, 7, day)),
            )

        ids, expected_reasons, expected_categories = {}, {}, {}
        counter = 0
        bridge_counter = 0
        date_counter = 0

        def add(name, *, reasons=(), bodies=None, dates=None, **overrides):
            nonlocal counter, bridge_counter, date_counter
            counter += 1
            price_id = PRICE_BASE + counter
            row = dict(
                id=price_id, created_at=datetime(2099, 7, 1, 12),
                updated_at=datetime(2099, 7, 1, 12), subscriber_id=101,
                organization_id=100101, loading_date=LOADING,
                loading_city_gcid=CITY_BASE + 1, unloading_city_gcid=CITY_BASE + 2,
                distance=100.0, total_weight=20.0, total_volume=80.0,
                body_type=56, price=20_000.0, currency_code="643", complex=False,
                price_type=3, is_fact=True, nearest_rate_date=date(2099, 7, 2),
                end_loading_date=LOADING, outlier_mark=False,
                weight_category=None, volume_category=None,
            )
            row.update(overrides)
            if "end_loading_date" not in overrides:
                row["end_loading_date"] = row["loading_date"]
            conn.execute(
                sql.SQL("INSERT INTO raw.prices ({}) VALUES ({})").format(
                    sql.SQL(", ").join(map(sql.Identifier, row)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in row),
                ),
                list(row.values()),
            )
            for body in bodies or []:
                bridge_counter += 1
                conn.execute(
                    "INSERT INTO raw.price_body_types (id, price_id, body_type_id) VALUES (%s, %s, %s)",
                    (PRICE_BASE + bridge_counter, price_id, body),
                )
            for pd in (dates if dates is not None else [row["loading_date"]]):
                date_counter += 1
                conn.execute(
                    "INSERT INTO raw.price_dates (id, price_id, price_date) VALUES (%s, %s, %s)",
                    (PRICE_BASE + date_counter, price_id, pd),
                )
            ids[name] = price_id
            if START <= row["loading_date"] < END:
                expected_reasons[name] = set(reasons)
            return price_id

        add("valid")
        add("zero_price", price=0, reasons=["NONPOSITIVE_PRICE"])
        add("negative_price", price=-100, reasons=["NONPOSITIVE_PRICE"])
        add("rub_low", price=999.99, reasons=["RUB_PRICE_LE_1000"])
        add("rub_boundary", price=1000, reasons=["RUB_PRICE_LE_1000"])
        add("rub_just_above", price=1000.01)
        add("zero_distance", distance=0, reasons=["NONPOSITIVE_DISTANCE"])
        add("negative_distance", distance=-1, reasons=["NONPOSITIVE_DISTANCE"])
        add("negative_weight_before_rounding", total_weight=-0.01, reasons=["NEGATIVE_WEIGHT"])
        add("negative_volume", total_volume=-0.01, reasons=["NEGATIVE_VOLUME"])
        for field, reason in [
            ("price", "NONFINITE_PRICE"), ("distance", "NONFINITE_DISTANCE"),
            ("total_weight", "NONFINITE_WEIGHT"), ("total_volume", "NONFINITE_VOLUME"),
        ]:
            add(f"nan_{field}", **{field: float("nan")}, reasons=[reason])
            add(f"inf_{field}", **{field: float("inf")}, reasons=[reason])
        add("missing_city", unloading_city_gcid=CITY_BASE + 99, reasons=["UNKNOWN_CITY"])
        add("same_city", unloading_city_gcid=CITY_BASE + 1)
        add("no_region", unloading_city_gcid=CITY_BASE + 3)
        add("multiple_reasons", price=0, distance=0, total_weight=-1, total_volume=-1,
            unloading_city_gcid=CITY_BASE + 99,
            reasons=["NONPOSITIVE_PRICE", "NONPOSITIVE_DISTANCE", "NEGATIVE_WEIGHT", "NEGATIVE_VOLUME", "UNKNOWN_CITY"])

        add("usd_small_valid", price=150.0, currency_code="840")
        add("eur_rounding", price=150.125, currency_code="978", nearest_rate_date=date(2099, 7, 4))
        add("missing_rate_date", currency_code="840", nearest_rate_date=None, reasons=["MISSING_RATE_DATE"])
        add("missing_exact_rate", currency_code="840", nearest_rate_date=date(2099, 7, 6),
            reasons=["MISSING_EXACT_FX_RATE"])
        add("conflicting_rates", currency_code="840", nearest_rate_date=date(2099, 7, 3),
            reasons=["CONFLICTING_FX_RATES"])
        add("unsupported_currency", currency_code="392", reasons=["UNSUPPORTED_CURRENCY"])
        add("rub_no_rate_date", nearest_rate_date=None)
        add("rub_ignores_source_quotes", nearest_rate_date=date(2099, 7, 3))
        for day in [8, 9, 11, 12]:
            add(f"invalid_rate_{day}", currency_code="978", nearest_rate_date=date(2099, 7, day),
                reasons=["INVALID_FX_RATE"])
        add("normalizes_to_zero", price=0.0001, currency_code="840", nearest_rate_date=date(2099, 7, 13),
            reasons=["NONPOSITIVE_NORMALIZED_PRICE"])

        for weight, category in [
            (0, "OTH"), (0.44, "OTH"), (0.45, "L5T"), (0.5, "L5T"),
            (2.44, "L5T"), (2.45, "5T"), (2.5, "5T"),
            (4.94, "5T"), (4.95, "10T"), (5, "10T"),
            (9.94, "10T"), (9.95, "FTL"), (10, "FTL"),
            (29.94, "FTL"), (29.95, "G20T"), (30, "G20T"),
        ]:
            name = f"weight_{weight}"
            add(name, total_weight=weight)
            expected_categories[name] = (category, "FTL")
        for volume, category in [(0, "OTH"), (0.1, "FTL"), (120, "FTL"), (120.0001, "OTH")]:
            name = f"volume_{volume}"
            add(name, total_volume=volume)
            expected_categories[name] = ("FTL", category)
        add("preserved_categories", total_volume=130, weight_category="L5T", volume_category="FTL")
        expected_categories["preserved_categories"] = ("L5T", "FTL")
        add("blank_categories", total_volume=0, weight_category=" ", volume_category="")
        expected_categories["blank_categories"] = ("FTL", "OTH")

        add("body_unknown", body_type=None)
        add("body_child_fallback", body_type=57)
        add("body_same_group", bodies=[57, 61])
        add("body_cross_group", bodies=[57, 61, 1, 15])
        add("body_bridge_overrides_primary", body_type=4, bodies=[57])
        add("body_other", body_type=3)
        add("preserved_outlier", outlier_mark=True)
        add("preserved_complex", complex=True)
        add("preserved_nonfact", is_fact=False, price_type=6)
        add("window_start", loading_date=START)
        add("window_last_day", loading_date=date(2099, 7, 31))
        add("outside_window", loading_date=END)
        add("recovery", price=0, reasons=["NONPOSITIVE_PRICE"], bodies=[57, 61])
        add("delete_case", bodies=[57, 1])
        add("move_case", bodies=[57, 61])

        stats = load(conn)
        yield {"conn": conn, "ids": ids, "reasons": expected_reasons,
               "categories": expected_categories, "stats": stats}
    finally:
        conn.rollback()
        conn.close()


def test_dq_batch_has_exact_quarantine_reasons_and_no_silent_losses(etl_data):
    conn, ids, reasons = etl_data["conn"], etl_data["ids"], etl_data["reasons"]
    quarantine = {r["price_id"]: set(r["reason_codes"]) for r in conn.execute(
        "SELECT price_id, reason_codes FROM core.dq_quarantine_prices WHERE price_id > %s AND price_id < %s",
        (PRICE_BASE, PRICE_BASE + 10_000),
    ).fetchall()}
    accepted = {r["price_id"] for r in conn.execute(
        "SELECT price_id FROM core.prices WHERE price_id > %s AND price_id < %s",
        (PRICE_BASE, PRICE_BASE + 10_000),
    ).fetchall()}
    for name, codes in reasons.items():
        if codes:
            assert quarantine[ids[name]] == codes, name
            assert ids[name] not in accepted
        else:
            assert ids[name] in accepted, name
            assert ids[name] not in quarantine
    assert len(accepted) + len(quarantine) == len(reasons)
    stats = etl_data["stats"]
    assert stats["source_rows"] == len(reasons)
    assert stats["accepted_rows"] == len(accepted)
    assert stats["quarantined_rows"] == len(quarantine)
    assert stats["dq_reasons"]["UNKNOWN_CITY"] == 2


def test_fx_uses_exact_quote_and_rounds_rubles_once(etl_data):
    conn, ids = etl_data["conn"], etl_data["ids"]
    usd = conn.execute("SELECT fx_rate, price_rub FROM core.prices WHERE price_id = %s", (ids["usd_small_valid"],)).fetchone()
    assert usd == {"fx_rate": Decimal("80.0"), "price_rub": Decimal("12000.00")}
    eur = conn.execute("SELECT price_rub FROM core.prices WHERE price_id = %s", (ids["eur_rounding"],)).fetchone()
    assert eur["price_rub"] == Decimal("14862.38")
    for name in ["rub_no_rate_date", "rub_ignores_source_quotes"]:
        assert conn.execute("SELECT fx_rate FROM core.prices WHERE price_id = %s", (ids[name],)).fetchone()["fx_rate"] == 1
    # Quotes before/after nearest_rate_date, plus a quote on loading_date, exist;
    # none may be substituted for the missing exact source date.
    assert conn.execute("SELECT count(*) AS n FROM core.prices WHERE price_id = %s", (ids["missing_exact_rate"],)).fetchone()["n"] == 0


def test_city_uses_smallest_source_id_and_missing_region_is_explicit(etl_data):
    conn = etl_data["conn"]
    row = conn.execute("SELECT * FROM core.dim_cities WHERE gc_id = %s", (CITY_BASE + 1,)).fetchone()
    assert row["source_city_id"] == PRICE_BASE + 101
    assert row["name"] == "Канонический город"
    assert row["region"] == "Канонический регион"
    assert conn.execute("SELECT region FROM core.dim_cities WHERE gc_id = %s", (CITY_BASE + 3,)).fetchone()["region"] == "Не указан"


def test_categories_use_rounded_weight_and_preserve_supplied_values(etl_data):
    conn, ids = etl_data["conn"], etl_data["ids"]
    for name, expected in etl_data["categories"].items():
        row = conn.execute(
            "SELECT weight_category, volume_category FROM core.prices WHERE price_id = %s", (ids[name],)
        ).fetchone()
        assert (row["weight_category"], row["volume_category"]) == expected, name


def test_body_bridge_fallback_deduplication_and_distinct_groups(etl_data):
    conn, ids = etl_data["conn"], etl_data["ids"]
    for name, expected in [
        ("body_unknown", {-1}), ("body_child_fallback", {56}),
        ("body_same_group", {56}), ("body_cross_group", {56, 15}),
        ("body_bridge_overrides_primary", {56}), ("body_other", {0}),
    ]:
        rows = conn.execute("SELECT body_group_id FROM core.price_body_groups WHERE price_id = %s", (ids[name],)).fetchall()
        assert {r["body_group_id"] for r in rows} == expected, name
        assert len(rows) == len(expected), name


def test_core_keeps_same_city_outlier_complex_and_nonfact_rows(etl_data):
    conn, ids = etl_data["conn"], etl_data["ids"]
    row = conn.execute("SELECT loading_city_gcid, unloading_city_gcid FROM core.prices WHERE price_id = %s", (ids["same_city"],)).fetchone()
    assert row["loading_city_gcid"] == row["unloading_city_gcid"]
    for name, field, expected in [
        ("preserved_outlier", "outlier_mark", True),
        ("preserved_complex", "complex", True),
        ("preserved_nonfact", "is_fact", False),
    ]:
        row = conn.execute(sql.SQL("SELECT {} FROM core.prices WHERE price_id = %s").format(sql.Identifier(field)), (ids[name],)).fetchone()
        assert row[field] is expected


def test_window_is_start_inclusive_end_exclusive(etl_data):
    conn, ids = etl_data["conn"], etl_data["ids"]
    for name in ["window_start", "window_last_day"]:
        assert conn.execute("SELECT count(*) AS n FROM core.prices WHERE price_id = %s", (ids[name],)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM core.prices WHERE price_id = %s", (ids["outside_window"],)).fetchone()["n"] == 0


def test_unchanged_rerun_is_exactly_idempotent(etl_data):
    conn = etl_data["conn"]
    before = snapshot(conn)
    stats = load(conn)
    assert snapshot(conn) == before
    assert stats == etl_data["stats"]


def test_quarantine_recovery_and_regression_remove_stale_core_rows(etl_data):
    conn, price_id = etl_data["conn"], etl_data["ids"]["recovery"]
    with conn.transaction(force_rollback=True):
        for amount, accepted in [(20_000, True), (0, False), (25_000, True)]:
            conn.execute("UPDATE raw.prices SET price = %s WHERE id = %s", (amount, price_id))
            load(conn)
            core_n = conn.execute("SELECT count(*) AS n FROM core.prices WHERE price_id = %s", (price_id,)).fetchone()["n"]
            quarantine_n = conn.execute("SELECT count(*) AS n FROM core.dq_quarantine_prices WHERE price_id = %s", (price_id,)).fetchone()["n"]
            bridge_n = conn.execute("SELECT count(*) AS n FROM core.price_body_groups WHERE price_id = %s", (price_id,)).fetchone()["n"]
            assert (core_n, quarantine_n, bridge_n) == ((1, 0, 1) if accepted else (0, 1, 0))


def test_source_deletion_removes_core_and_bridges(etl_data):
    conn, price_id = etl_data["conn"], etl_data["ids"]["delete_case"]
    with conn.transaction(force_rollback=True):
        conn.execute("DELETE FROM raw.prices WHERE id = %s", (price_id,))
        stats = load(conn)
        assert stats["source_deleted_rows"] == 1
        for table in ["prices", "dq_quarantine_prices", "price_body_groups", "price_dates"]:
            assert conn.execute(f"SELECT count(*) AS n FROM core.{table} WHERE price_id = %s", (price_id,)).fetchone()["n"] == 0
        assert load(conn)["source_deleted_rows"] == 0


@pytest.mark.parametrize("name", ["move_case", "recovery"])
def test_moved_date_is_reconciled_via_old_core_or_quarantine_window(etl_data, name):
    conn, price_id = etl_data["conn"], etl_data["ids"][name]
    moved_date = date(2099, 8, 10)
    with conn.transaction(force_rollback=True):
        conn.execute(
            "UPDATE raw.prices SET loading_date = %s, end_loading_date = %s, price = 40000 WHERE id = %s",
            (moved_date, moved_date, price_id),
        )
        conn.execute("UPDATE raw.price_dates SET price_date = %s WHERE price_id = %s", (moved_date, price_id))
        load(conn)  # Re-run the OLD July window, although the source is in August.
        row = conn.execute("SELECT loading_date, price_rub FROM core.prices WHERE price_id = %s", (price_id,)).fetchone()
        assert row == {"loading_date": moved_date, "price_rub": Decimal("40000.00")}
        assert conn.execute("SELECT count(*) AS n FROM core.dq_quarantine_prices WHERE price_id = %s", (price_id,)).fetchone()["n"] == 0
        assert conn.execute("SELECT price_date FROM core.price_dates WHERE price_id = %s", (price_id,)).fetchone()["price_date"] == moved_date
        before = snapshot(conn)
        load(conn)
        assert snapshot(conn) == before


def test_deleted_raw_city_is_not_rescued_by_stale_core_dimension(etl_data):
    conn, price_id = etl_data["conn"], etl_data["ids"]["valid"]
    with conn.transaction(force_rollback=True):
        conn.execute("DELETE FROM raw.cities WHERE gc_id = %s", (CITY_BASE + 1,))
        load(conn)
        assert conn.execute("SELECT count(*) AS n FROM core.prices WHERE price_id = %s", (price_id,)).fetchone()["n"] == 0
        assert "UNKNOWN_CITY" in conn.execute(
            "SELECT reason_codes FROM core.dq_quarantine_prices WHERE price_id = %s", (price_id,)
        ).fetchone()["reason_codes"]


def test_empty_window_is_safe_and_invalid_window_fails(etl_data):
    conn = etl_data["conn"]
    before = snapshot(conn)
    stats = load(conn, date(2099, 12, 1), date(2100, 1, 1))
    assert stats == {"source_rows": 0, "accepted_rows": 0, "quarantined_rows": 0,
                     "source_deleted_rows": 0, "dq_reasons": {}}
    assert snapshot(conn) == before
    with pytest.raises(psycopg.Error, match="non-empty"):
        with conn.transaction():
            load(conn, END, START)
