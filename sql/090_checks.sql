-- Read-only SQL smoke checks. A violation raises an exception (ON_ERROR_STOP).
-- Run after ETL and mart.refresh_market_corridor in the same database snapshot.
DO $checks$
DECLARE
    v_as_of date;
    v_expected bigint;
    v_actual bigint;
BEGIN
    IF EXISTS (
        SELECT 1 FROM core.dim_cities GROUP BY gc_id HAVING count(*) <> 1
    ) THEN
        RAISE EXCEPTION 'Duplicate core.dim_cities.gc_id';
    END IF;

    IF EXISTS (
        SELECT 1 FROM core.price_body_groups
        GROUP BY price_id, body_group_id HAVING count(*) <> 1
    ) THEN
        RAISE EXCEPTION 'Duplicate price / body group';
    END IF;

    IF EXISTS (
        SELECT 1 FROM mart.mart_market_corridor
        GROUP BY month, loading_city_gcid, unloading_city_gcid,
                 body_group_id, weight_category
        HAVING count(*) <> 1
    ) THEN
        RAISE EXCEPTION 'Duplicate mart grain';
    END IF;

    IF (SELECT count(DISTINCT as_of_date) FROM mart.mart_market_corridor) > 1 THEN
        RAISE EXCEPTION 'Mixed as_of snapshots in market corridor';
    END IF;

    IF EXISTS (
        SELECT 1 FROM mart.mart_market_corridor
        WHERE NOT (p25_trip <= median_trip AND median_trip <= p75_trip)
           OR NOT (p25_km <= median_km AND median_km <= p75_km)
           OR trips_count <= 0 OR carriers_count > trips_count
           OR carriers_count < 0 OR latest_price_date > as_of_date
           OR (trips_count = 1 AND cv_rate IS NOT NULL)
           OR reliability_level <> CASE
                WHEN trips_count >= 30 AND carriers_count >= 5
                 AND as_of_date - latest_price_date BETWEEN 0 AND 30 THEN 'HIGH'
                WHEN trips_count >= 10 THEN 'MEDIUM'
                ELSE 'LOW_DATA' END
    ) THEN
        RAISE EXCEPTION 'Invalid market corridor metric or freshness';
    END IF;

    -- This is a count of contributions, not a count of all unique source trips:
    -- a long-term or multi-group price legitimately belongs to multiple cells.
    SELECT max(as_of_date), coalesce(sum(trips_count), 0)
      INTO v_as_of, v_actual FROM mart.mart_market_corridor;
    IF v_as_of IS NOT NULL THEN
        SELECT count(*) INTO v_expected
        FROM (
            SELECT DISTINCT
                p.price_id, date_trunc('month', pd.price_date), bg.body_group_id
            FROM core.prices p
            JOIN core.price_dates pd USING (price_id)
            JOIN core.price_body_groups bg USING (price_id)
            WHERE p.is_fact AND p.price_type IN (1, 2, 3, 4, 5, 9)
              AND NOT p.complex AND NOT p.outlier_mark
              AND p.price > 0 AND p.price_rub > 0 AND p.distance > 0
              AND pd.price_date <= v_as_of
        ) AS eligible_contributions;
        IF v_actual <> v_expected THEN
            RAISE EXCEPTION 'Contribution count mismatch: mart %, core %', v_actual, v_expected;
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM mart.mart_market_corridor a
        LEFT JOIN mart.mart_market_corridor pm
          ON pm.month = (a.month - INTERVAL '1 month')::date
         AND pm.loading_city_gcid = a.loading_city_gcid
         AND pm.unloading_city_gcid = a.unloading_city_gcid
         AND pm.body_group_id = a.body_group_id
         AND pm.weight_category = a.weight_category
        LEFT JOIN mart.mart_market_corridor py
          ON py.month = (a.month - INTERVAL '1 year')::date
         AND py.loading_city_gcid = a.loading_city_gcid
         AND py.unloading_city_gcid = a.unloading_city_gcid
         AND py.body_group_id = a.body_group_id
         AND py.weight_category = a.weight_category
        WHERE a.mom_pct IS DISTINCT FROM
              100.0 * (a.median_trip / NULLIF(pm.median_trip, 0) - 1.0)
           OR a.yoy_pct IS DISTINCT FROM
              100.0 * (a.median_trip / NULLIF(py.median_trip, 0) - 1.0)
    ) THEN
        RAISE EXCEPTION 'MoM / YoY is not a calendar comparison';
    END IF;
END;
$checks$;

SELECT
    count(*) AS market_cells,
    min(month) AS first_month,
    max(month) AS last_month,
    sum(trips_count) AS cell_contributions,
    count(*) FILTER (WHERE reliability_level = 'HIGH') AS high_reliability_cells,
    count(*) FILTER (WHERE reliability_level = 'MEDIUM') AS medium_reliability_cells,
    count(*) FILTER (WHERE reliability_level = 'LOW_DATA') AS low_data_cells
FROM mart.mart_market_corridor;
