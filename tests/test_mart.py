"""Numerical/invariant tests against the actual PostgreSQL mart implementation.

Set TEST_DATABASE_URL to an initialized test database. Fixtures use reserved
IDs and a transaction rollback: neither the source nor the persisted mart is
changed when the tests finish. The common advisory lock prevents overlap with
a concurrently running ETL. The production data is never truncated.
"""

from datetime import date
from decimal import Decimal
import math
import os

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row


pytestmark = pytest.mark.integration
AS_OF = date(2026, 10, 26)
CITY_BASE = 2_000_000_000
PRICE_BASE = 9_000_000_000_000


@pytest.fixture(scope="module")
def mart_db():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    conn = psycopg.connect(dsn, row_factory=dict_row)
    try:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('smart_logistics.pipeline'))")
        if conn.execute("SELECT to_regclass('core.prices') AS name").fetchone()["name"] is None:
            pytest.fail("Initialize core and mart schemas before integration tests")

        for offset in range(1, 16):
            city_id = CITY_BASE + offset
            conn.execute(
                """INSERT INTO core.dim_cities
                   (gc_id, source_city_id, name, full_name_local, region, latitude, longitude)
                   VALUES (%s, %s, %s, %s, %s, 55.0, 37.0)""",
                (city_id, PRICE_BASE + city_id, f"Test city {offset}",
                 f"Test city {offset}, Test region", "Test region"),
            )

        counter = 0

        def add_price(
            route, amount, loading=date(2026, 10, 12), *, distance=100,
            organization=101, price_type=3, is_fact=True, complex=False,
            outlier=False, price_dates=None, groups=None, weight="FTL", volume="FTL",
        ):
            nonlocal counter
            counter += 1
            price_id = PRICE_BASE + counter
            conn.execute(
                """INSERT INTO core.prices (
                     price_id, loading_date, end_loading_date,
                     loading_city_gcid, unloading_city_gcid,
                     distance, total_weight, total_volume, body_type,
                     price, currency_code, fx_rate, price_rub, price_type, is_fact,
                     complex, outlier_mark, weight_category, volume_category,
                     organization_id, source_created_at
                   ) VALUES (
                     %s, %s, %s, %s, %s, %s, %s, %s, 56,
                     %s, '643', 1, %s, %s, %s, %s, %s, %s, %s, %s, %s
                   )""",
                (
                    price_id, loading, max(price_dates or [loading]),
                    CITY_BASE + 1, CITY_BASE + route, distance,
                    20 if weight == "FTL" else 7,
                    90 if volume == "FTL" else 130,
                    amount, Decimal(str(amount)), price_type, is_fact,
                    complex, outlier, weight, volume, organization, loading,
                ),
            )
            for body_id, body_name in (groups if groups is not None else [(56, "Тент")]):
                conn.execute(
                    """INSERT INTO core.price_body_groups
                       (price_id, body_group_id, body_group) VALUES (%s, %s, %s)""",
                    (price_id, body_id, body_name),
                )
            for pd in (price_dates if price_dates is not None else [loading]):
                conn.execute(
                    "INSERT INTO core.price_dates (price_id, price_date) VALUES (%s, %s)",
                    (price_id, pd),
                )
            return price_id

        # Hand-computable quartiles. Different distances detect the incorrect
        # shortcut percentile(price) / avg(distance). Two dates in one month,
        # two volume categories and cross-group membership must not inflate N.
        add_price(2, 10_000, organization=101, distance=100,
                  price_dates=[date(2026, 10, 12), date(2026, 10, 20)],
                  groups=[(56, "Тент"), (15, "Борт")])
        add_price(2, 20_000, organization=101, distance=200)
        add_price(2, 30_000, organization=102, distance=100, price_type=9, volume="OTH")
        add_price(2, 40_000, organization=103, distance=400, price_type=1, volume="OTH")

        # August exists, September does not. LAG over existing rows would be wrong.
        add_price(3, 10_000, date(2025, 10, 10))
        add_price(3, 20_000, date(2026, 8, 10))
        add_price(3, 40_000, date(2026, 10, 10))

        # A long-term source price contributes once in each historical month.
        add_price(4, 50_000, date(2026, 8, 12), price_dates=[
            date(2026, 8, 12), date(2026, 9, 12),
            date(2026, 10, 12), date(2026, 11, 12),
        ])

        # Filtering must use the exact price_date before month truncation.
        add_price(5, 15_000)
        add_price(5, 70_000, outlier=True)
        add_price(5, 80_000, complex=True)
        add_price(5, 90_000, price_type=6, is_fact=False)
        add_price(5, 100_000, price_type=6, is_fact=True)
        add_price(5, 110_000, price_type=3, is_fact=False)
        add_price(5, 120_000, date(2026, 10, 27))
        add_price(5, 130_000, date(2026, 11, 10))

        # Trust thresholds, including the exact 30-day freshness boundary.
        for route, count, carriers, pd, pt in [
            (6, 30, 5, date(2026, 10, 26), 3),
            (7, 30, 4, date(2026, 10, 26), 3),
            (8, 10, 5, date(2026, 10, 26), 1),
            (9, 9, 5, date(2026, 10, 26), 3),
            (10, 30, 5, date(2026, 9, 25), 3),
            (11, 30, 5, date(2026, 9, 26), 3),
        ]:
            for i in range(count):
                add_price(route, 15_000 + 10 * i, pd,
                          organization=201 + i % carriers, price_type=pt)

        add_price(12, 10_000, groups=[(-1, "Не указан")])
        add_price(12, 20_000, groups=[(0, "Прочие")])
        add_price(13, 10_000, date(2026, 9, 12))
        add_price(13, 20_000, date(2026, 10, 12))
        # A different weight category is not the comparison cell.
        add_price(14, 10_000, date(2026, 9, 12), weight="FTL")
        add_price(14, 20_000, date(2026, 10, 12), weight="10T")

        conn.execute("SELECT mart.refresh_market_corridor(%s)", (AS_OF,))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def cell(conn, route, month=date(2026, 10, 1), *, body=56, weight="FTL"):
    result = conn.execute(
        """SELECT * FROM mart.mart_market_corridor
           WHERE loading_city_gcid = %s AND unloading_city_gcid = %s
             AND month = %s AND body_group_id = %s AND weight_category = %s""",
        (CITY_BASE + 1, CITY_BASE + route, month, body, weight),
    ).fetchone()
    assert result is not None, "Expected test market cell is missing"
    return result


def test_trip_and_km_percentiles_are_computed_from_observations(mart_db):
    row = cell(mart_db, 2)
    assert [row["p25_trip"], row["median_trip"], row["p75_trip"]] == [17_500, 25_000, 32_500]
    assert row["avg_trip"] == 25_000
    assert [row["p25_km"], row["median_km"], row["p75_km"]] == [100, 100, 150]
    expected_cv = math.sqrt(sum((x - 25_000) ** 2 for x in [10_000, 20_000, 30_000, 40_000]) / 3) / 25_000
    assert row["cv_rate"] == pytest.approx(expected_cv)


def test_month_dates_and_volume_categories_do_not_duplicate_prices(mart_db):
    row = cell(mart_db, 2)
    assert row["trips_count"] == 4
    assert row["carriers_count"] == 2  # Customer type 1 is not a carrier.
    assert row["latest_price_date"] == date(2026, 10, 20)


def test_cross_group_price_belongs_once_to_each_group(mart_db):
    tent = cell(mart_db, 2)
    flatbed = cell(mart_db, 2, body=15)
    assert tent["trips_count"] == 4
    assert flatbed["trips_count"] == 1
    assert flatbed["median_trip"] == 10_000
    assert flatbed["cv_rate"] is None


def test_mom_yoy_use_exact_calendar_cells(mart_db):
    row = cell(mart_db, 3)
    assert row["mom_pct"] is None
    assert row["yoy_pct"] == pytest.approx(300)
    assert cell(mart_db, 3, date(2026, 8, 1))["yoy_pct"] is None
    assert cell(mart_db, 13)["mom_pct"] == pytest.approx(100)
    assert cell(mart_db, 14, weight="10T")["mom_pct"] is None


def test_long_term_price_is_present_in_each_historical_month(mart_db):
    rows = mart_db.execute(
        """SELECT month, trips_count, median_trip FROM mart.mart_market_corridor
           WHERE loading_city_gcid = %s AND unloading_city_gcid = %s ORDER BY month""",
        (CITY_BASE + 1, CITY_BASE + 4),
    ).fetchall()
    assert [r["month"] for r in rows] == [date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)]
    assert all(r["trips_count"] == 1 and r["median_trip"] == 50_000 for r in rows)


def test_future_dates_auction_complex_and_outliers_are_excluded(mart_db):
    row = cell(mart_db, 5)
    assert row["trips_count"] == 1
    assert row["median_trip"] == 15_000
    assert row["cv_rate"] is None
    assert mart_db.execute(
        "SELECT count(*) AS n FROM mart.mart_market_corridor WHERE month > %s",
        (date(2026, 10, 1),),
    ).fetchone()["n"] == 0


@pytest.mark.parametrize(
    "route,month,level,carriers",
    [
        (6, date(2026, 10, 1), "HIGH", 5),
        (7, date(2026, 10, 1), "MEDIUM", 4),
        (8, date(2026, 10, 1), "MEDIUM", 0),
        (9, date(2026, 10, 1), "LOW_DATA", 5),
        (10, date(2026, 9, 1), "MEDIUM", 5),
        (11, date(2026, 9, 1), "HIGH", 5),
    ],
)
def test_reliability_thresholds(mart_db, route, month, level, carriers):
    row = cell(mart_db, route, month)
    assert row["reliability_level"] == level
    assert row["carriers_count"] == carriers


def test_unknown_and_other_bodies_remain_separate(mart_db):
    assert cell(mart_db, 12, body=-1)["body_group"] == "Не указан"
    assert cell(mart_db, 12, body=0)["body_group"] == "Прочие"


def test_repeat_refresh_is_exactly_idempotent(mart_db):
    sql = """SELECT * FROM mart.mart_market_corridor
             WHERE loading_city_gcid = %s
             ORDER BY month, unloading_city_gcid, body_group_id, weight_category"""
    before = mart_db.execute(sql, (CITY_BASE + 1,)).fetchall()
    total_before = mart_db.execute("SELECT count(*) AS n FROM mart.mart_market_corridor").fetchone()["n"]
    rebuilt = mart_db.execute("SELECT mart.refresh_market_corridor(%s) AS n", (AS_OF,)).fetchone()["n"]
    after = mart_db.execute(sql, (CITY_BASE + 1,)).fetchall()
    assert after == before
    assert rebuilt == total_before
