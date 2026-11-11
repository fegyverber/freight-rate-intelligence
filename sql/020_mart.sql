-- PostgreSQL 17. One physical row = month x route x body group x weight category.
-- volume_category intentionally does not belong to the grain.
CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS mart.mart_market_corridor (
    month                   date NOT NULL,
    loading_city_gcid        integer NOT NULL,
    unloading_city_gcid      integer NOT NULL,
    body_group_id           integer NOT NULL,
    weight_category         text NOT NULL,
    body_group              text NOT NULL,
    loading_city            text NOT NULL,
    unloading_city          text NOT NULL,
    origin_region           text NOT NULL,
    destination_region      text NOT NULL,
    p25_trip                double precision NOT NULL,
    median_trip             double precision NOT NULL,
    p75_trip                double precision NOT NULL,
    avg_trip                double precision NOT NULL,
    p25_km                  double precision NOT NULL,
    median_km               double precision NOT NULL,
    p75_km                  double precision NOT NULL,
    trips_count             bigint NOT NULL,
    carriers_count          bigint NOT NULL,
    cv_rate                 double precision,
    mom_pct                 double precision,
    yoy_pct                 double precision,
    latest_price_date       date NOT NULL,
    reliability_level       text NOT NULL,
    as_of_date              date NOT NULL,
    PRIMARY KEY (
        month, loading_city_gcid, unloading_city_gcid,
        body_group_id, weight_category
    ),
    CHECK (month = date_trunc('month', month)::date),
    CHECK (p25_trip <= median_trip AND median_trip <= p75_trip),
    CHECK (p25_km <= median_km AND median_km <= p75_km),
    CHECK (trips_count > 0 AND carriers_count BETWEEN 0 AND trips_count),
    CHECK (latest_price_date >= month AND latest_price_date <= as_of_date),
    CHECK (reliability_level IN ('HIGH', 'MEDIUM', 'LOW_DATA'))
);

CREATE INDEX IF NOT EXISTS mart_market_corridor_route_idx
    ON mart.mart_market_corridor (
        loading_city_gcid, unloading_city_gcid,
        body_group_id, weight_category, month DESC
    );

COMMENT ON TABLE mart.mart_market_corridor IS
    'Monthly market corridor in RUB excluding VAT. Counts are not additive across body groups or months.';
COMMENT ON COLUMN mart.mart_market_corridor.latest_price_date IS
    'Maximum eligible price_dates.price_date in THIS monthly cell; freshness is as_of_date minus this date.';
COMMENT ON COLUMN mart.mart_market_corridor.cv_rate IS
    'Sample standard deviation / average trip price, a fraction (0.2 means 20%). NULL for a singleton.';
COMMENT ON COLUMN mart.mart_market_corridor.mom_pct IS
    'Percentage change of median_trip versus the preceding CALENDAR month, not the last observed month.';

-- The same physical data serves a route card and a table covering many routes.
CREATE OR REPLACE VIEW mart.mart_route_card AS
SELECT * FROM mart.mart_market_corridor;

CREATE OR REPLACE FUNCTION mart.refresh_market_corridor(p_as_of date)
RETURNS bigint
LANGUAGE plpgsql
AS $function$
DECLARE
    v_rows bigint;
BEGIN
    IF p_as_of IS NULL THEN
        RAISE EXCEPTION 'as_of must not be NULL';
    END IF;

    -- Shared with ETL: a standalone refresh cannot race with a core replacement.
    PERFORM pg_advisory_xact_lock(hashtext('smart_logistics.pipeline'));

    -- DELETE + INSERT runs in one transaction. Readers see a complete old or new
    -- snapshot; a failed insert rolls back the deletion. No wall-clock columns
    -- are stored, so an unchanged rerun reproduces the same complete row values.
    DELETE FROM mart.mart_market_corridor;

    INSERT INTO mart.mart_market_corridor (
        month, loading_city_gcid, unloading_city_gcid, body_group_id,
        weight_category, body_group, loading_city, unloading_city,
        origin_region, destination_region,
        p25_trip, median_trip, p75_trip, avg_trip,
        p25_km, median_km, p75_km,
        trips_count, carriers_count, cv_rate, mom_pct, yoy_pct,
        latest_price_date, reliability_level, as_of_date
    )
    WITH monthly_dates AS (
        -- A source may contain multiple dates in the same month. One price may
        -- contribute only once to a month; long-term prices can span months.
        -- Filter before truncating: even 2026-10-27 is future at as_of=10-26.
        SELECT
            pd.price_id,
            date_trunc('month', pd.price_date)::date AS month,
            max(pd.price_date) AS latest_price_date
        FROM core.price_dates AS pd
        WHERE pd.price_date <= p_as_of
        GROUP BY pd.price_id, date_trunc('month', pd.price_date)::date
    ), observations AS (
        SELECT
            md.month,
            md.latest_price_date,
            p.price_id,
            p.loading_city_gcid,
            p.unloading_city_gcid,
            bg.body_group_id,
            bg.body_group,
            p.weight_category,
            p.price_rub,
            p.price_rub / NULLIF(p.distance, 0) AS price_km_rub,
            p.organization_id,
            p.price_type
        FROM core.prices AS p
        JOIN monthly_dates AS md USING (price_id)
        -- The core bridge PK(price_id, body_group_id) already collapses all
        -- child body codes in one business group. Cross-group membership is
        -- intentional; it must not be collapsed into an arbitrary first group.
        JOIN core.price_body_groups AS bg USING (price_id)
        WHERE p.is_fact
          AND p.price_type IN (1, 2, 3, 4, 5, 9)
          AND NOT p.complex
          AND NOT p.outlier_mark
          AND p.price > 0
          AND p.distance > 0
          AND p.price_rub > 0
    ), aggregates AS MATERIALIZED (
        SELECT
            month, loading_city_gcid, unloading_city_gcid,
            body_group_id, weight_category,
            min(body_group) AS body_group,
            percentile_cont(ARRAY[0.25, 0.5, 0.75]::double precision[])
                WITHIN GROUP (ORDER BY price_rub::double precision) AS trip_q,
            percentile_cont(ARRAY[0.25, 0.5, 0.75]::double precision[])
                WITHIN GROUP (ORDER BY price_km_rub) AS km_q,
            avg(price_rub)::double precision AS avg_trip,
            count(DISTINCT price_id) AS trips_count,
            count(DISTINCT organization_id)
                FILTER (WHERE price_type IN (3, 9)) AS carriers_count,
            CASE
                WHEN count(*) >= 2 AND avg(price_rub) <> 0
                THEN (stddev_samp(price_rub) / avg(price_rub))::double precision
                ELSE NULL
            END AS cv_rate,
            max(latest_price_date) AS latest_price_date
        FROM observations
        GROUP BY month, loading_city_gcid, unloading_city_gcid,
                 body_group_id, weight_category
    )
    SELECT
        a.month, a.loading_city_gcid, a.unloading_city_gcid,
        a.body_group_id, a.weight_category, a.body_group,
        origin.name, destination.name, origin.region, destination.region,
        a.trip_q[1], a.trip_q[2], a.trip_q[3], a.avg_trip,
        a.km_q[1], a.km_q[2], a.km_q[3],
        a.trips_count, a.carriers_count, a.cv_rate,
        100.0 * (a.trip_q[2] / NULLIF(prev_month.trip_q[2], 0) - 1.0),
        100.0 * (a.trip_q[2] / NULLIF(prev_year.trip_q[2], 0) - 1.0),
        a.latest_price_date,
        CASE
            WHEN a.trips_count >= 30
             AND a.carriers_count >= 5
             AND p_as_of - a.latest_price_date BETWEEN 0 AND 30
            THEN 'HIGH'
            WHEN a.trips_count >= 10 THEN 'MEDIUM'
            ELSE 'LOW_DATA'
        END,
        p_as_of
    FROM aggregates AS a
    -- Join the unique city dimension AFTER aggregation, never raw.cities.
    JOIN core.dim_cities AS origin ON origin.gc_id = a.loading_city_gcid
    JOIN core.dim_cities AS destination ON destination.gc_id = a.unloading_city_gcid
    LEFT JOIN aggregates AS prev_month
      ON prev_month.month = (a.month - INTERVAL '1 month')::date
     AND prev_month.loading_city_gcid = a.loading_city_gcid
     AND prev_month.unloading_city_gcid = a.unloading_city_gcid
     AND prev_month.body_group_id = a.body_group_id
     AND prev_month.weight_category = a.weight_category
    LEFT JOIN aggregates AS prev_year
      ON prev_year.month = (a.month - INTERVAL '1 year')::date
     AND prev_year.loading_city_gcid = a.loading_city_gcid
     AND prev_year.unloading_city_gcid = a.unloading_city_gcid
     AND prev_year.body_group_id = a.body_group_id
     AND prev_year.weight_category = a.weight_category;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    ANALYZE mart.mart_market_corridor;
    RETURN v_rows;
END;
$function$;
