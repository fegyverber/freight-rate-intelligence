-- Window is [start_date, end_date) by loading_date. Source is a complete snapshot.
-- Reconcile both the current source window and previously loaded IDs in that
-- window: source deletions, quarantine recovery and moved loading dates are safe.
CREATE OR REPLACE FUNCTION core.load_prices(p_start_date date, p_end_date date)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    v_read bigint;
    v_accepted bigint;
    v_rejected bigint;
    v_removed bigint;
    v_reasons jsonb;
BEGIN
    IF p_start_date IS NULL OR p_end_date IS NULL OR p_start_date >= p_end_date THEN
        RAISE EXCEPTION 'Expected non-empty [start_date, end_date) window';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('smart_logistics.pipeline'));
    DROP TABLE IF EXISTS pg_temp._load_ids, pg_temp._cities, pg_temp._fx, pg_temp._classified;

    CREATE TEMP TABLE _load_ids ON COMMIT DROP AS
      SELECT id AS price_id FROM raw.prices
        WHERE loading_date >= p_start_date AND loading_date < p_end_date
      UNION
      SELECT price_id FROM core.prices
        WHERE loading_date >= p_start_date AND loading_date < p_end_date
      UNION
      SELECT price_id FROM core.dq_quarantine_prices
        WHERE loading_date >= p_start_date AND loading_date < p_end_date;
    CREATE UNIQUE INDEX ON _load_ids(price_id);
    ANALYZE _load_ids;

    CREATE TEMP TABLE _cities ON COMMIT DROP AS
      SELECT DISTINCT ON (gc_id) gc_id, id AS source_city_id,
        btrim(split_part(full_name_local, ',', 1)) AS name,
        full_name_local,
        coalesce(nullif(btrim(substr(full_name_local, nullif(strpos(full_name_local, ','), 0) + 1)), ''),
                 'Не указан') AS region,
        latitude, longitude
      FROM raw.cities ORDER BY gc_id, id;
    CREATE UNIQUE INDEX ON _cities(gc_id);
    INSERT INTO core.dim_cities
      SELECT * FROM _cities
      ON CONFLICT (gc_id) DO UPDATE SET
        source_city_id = EXCLUDED.source_city_id, name = EXCLUDED.name,
        full_name_local = EXCLUDED.full_name_local, region = EXCLUDED.region,
        latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude;

    -- Identical duplicate rates collapse; contradictory quotes are quarantined.
    CREATE TEMP TABLE _fx ON COMMIT DROP AS
      SELECT num_code, setting_date, min(value) AS rate,
             count(DISTINCT value) AS distinct_rates
      FROM raw.exchange_rates GROUP BY num_code, setting_date;
    CREATE UNIQUE INDEX ON _fx(num_code, setting_date);
    ANALYZE _fx;

    CREATE TEMP TABLE _classified ON COMMIT DROP AS
    WITH prepared AS (
      SELECT p.*, to_jsonb(p) AS source_payload,
        lc.gc_id IS NOT NULL AND uc.gc_id IS NOT NULL AS cities_present,
        coalesce(r.distinct_rates, 0) AS rate_count,
        CASE WHEN p.currency_code = '643' THEN 1.0 ELSE r.rate END AS fx_rate
      FROM raw.prices p JOIN _load_ids i ON i.price_id = p.id
      LEFT JOIN _cities lc ON lc.gc_id = p.loading_city_gcid
      LEFT JOIN _cities uc ON uc.gc_id = p.unloading_city_gcid
      LEFT JOIN _fx r ON r.num_code = p.currency_code AND r.setting_date = p.nearest_rate_date
    ), normalized AS (
      SELECT *, CASE WHEN core.finite(price) AND core.finite(fx_rate)
                THEN round(price::numeric * fx_rate::numeric, 2) END AS normalized_price_rub
      FROM prepared
    )
    SELECT *, array_remove(ARRAY[
      CASE WHEN NOT core.finite(price) THEN 'NONFINITE_PRICE' END,
      CASE WHEN price <= 0 THEN 'NONPOSITIVE_PRICE' END,
      CASE WHEN currency_code = '643' AND price > 0 AND price <= 1000 THEN 'RUB_PRICE_LE_1000' END,
      CASE WHEN NOT core.finite(distance) THEN 'NONFINITE_DISTANCE' END,
      CASE WHEN distance <= 0 THEN 'NONPOSITIVE_DISTANCE' END,
      CASE WHEN NOT core.finite(total_weight) THEN 'NONFINITE_WEIGHT' END,
      CASE WHEN total_weight < 0 THEN 'NEGATIVE_WEIGHT' END,
      CASE WHEN NOT core.finite(total_volume) THEN 'NONFINITE_VOLUME' END,
      CASE WHEN total_volume < 0 THEN 'NEGATIVE_VOLUME' END,
      CASE WHEN NOT cities_present THEN 'UNKNOWN_CITY' END,
      CASE WHEN currency_code NOT IN ('643','840','978') THEN 'UNSUPPORTED_CURRENCY' END,
      CASE WHEN currency_code IN ('840','978') AND nearest_rate_date IS NULL THEN 'MISSING_RATE_DATE' END,
      CASE WHEN currency_code IN ('840','978') AND nearest_rate_date IS NOT NULL AND rate_count = 0
        THEN 'MISSING_EXACT_FX_RATE' END,
      CASE WHEN currency_code IN ('840','978') AND rate_count > 1 THEN 'CONFLICTING_FX_RATES' END,
      CASE WHEN currency_code IN ('840','978') AND rate_count > 0
        AND (NOT core.finite(fx_rate) OR fx_rate <= 0) THEN 'INVALID_FX_RATE' END,
      CASE WHEN price > 0 AND fx_rate > 0 AND normalized_price_rub <= 0
        THEN 'NONPOSITIVE_NORMALIZED_PRICE' END
    ], NULL)::text[] AS reason_codes
    FROM normalized;
    CREATE UNIQUE INDEX ON _classified(id);
    ANALYZE _classified;

    SELECT count(*), count(*) FILTER (WHERE cardinality(reason_codes) = 0),
                    count(*) FILTER (WHERE cardinality(reason_codes) > 0)
      INTO v_read, v_accepted, v_rejected FROM _classified;
    SELECT count(*) INTO v_removed FROM _load_ids i
      WHERE NOT EXISTS (SELECT 1 FROM _classified s WHERE s.id = i.price_id);

    -- Bridge rows cascade; the entire replacement is committed with the mart.
    DELETE FROM core.prices p USING _load_ids i WHERE p.price_id = i.price_id;
    DELETE FROM core.dq_quarantine_prices q USING _load_ids i WHERE q.price_id = i.price_id;
    INSERT INTO core.dq_quarantine_prices(price_id, loading_date, reason_codes, source_payload)
      SELECT id, loading_date, reason_codes, source_payload FROM _classified
      WHERE cardinality(reason_codes) > 0;
    INSERT INTO core.prices (
      price_id, source_created_at, source_updated_at, subscriber_id, organization_id,
      loading_date, end_loading_date, loading_city_gcid, unloading_city_gcid,
      distance, total_weight, total_volume, body_type, price, currency_code,
      nearest_rate_date, fx_rate, price_rub, price_type, is_fact, complex,
      outlier_mark, weight_category, volume_category, source)
    SELECT id, created_at, updated_at, subscriber_id, organization_id,
      loading_date, end_loading_date, loading_city_gcid, unloading_city_gcid,
      distance, total_weight, total_volume, body_type, price, currency_code,
      nearest_rate_date, fx_rate::numeric, normalized_price_rub, price_type, is_fact, complex,
      outlier_mark,
      coalesce(nullif(btrim(weight_category), ''), core.weight_category(total_weight)),
      coalesce(nullif(btrim(volume_category), ''), CASE WHEN total_volume > 0 AND total_volume <= 120 THEN 'FTL' ELSE 'OTH' END),
      source
    FROM _classified WHERE cardinality(reason_codes) = 0;

    INSERT INTO core.price_body_groups(price_id, body_group_id, body_group)
    WITH source_bodies AS (
      SELECT p.price_id, b.body_type_id FROM core.prices p
      JOIN _load_ids i USING(price_id)
      JOIN raw.price_body_types b ON b.price_id = p.price_id
      UNION ALL
      SELECT p.price_id, p.body_type FROM core.prices p JOIN _load_ids i USING(price_id)
      WHERE NOT EXISTS (SELECT 1 FROM raw.price_body_types b WHERE b.price_id = p.price_id)
    ), groups AS (
      SELECT DISTINCT b.price_id, core.body_group_id(b.body_type_id, t.std_id) AS body_group_id
      FROM source_bodies b LEFT JOIN raw.body_types t ON t.id = b.body_type_id
    )
    SELECT g.price_id, g.body_group_id, d.body_group
      FROM groups g JOIN core.dim_body_groups d USING(body_group_id);
    INSERT INTO core.price_dates(price_id, price_date)
      SELECT DISTINCT d.price_id, d.price_date FROM raw.price_dates d
      JOIN _load_ids i ON i.price_id = d.price_id
      JOIN core.prices p ON p.price_id = d.price_id;

    SELECT coalesce(jsonb_object_agg(reason, n), '{}'::jsonb) INTO v_reasons
    FROM (SELECT reason, count(*) AS n FROM _classified
          CROSS JOIN LATERAL unnest(reason_codes) AS reason GROUP BY reason) counts;
    RETURN jsonb_build_object('source_rows', v_read, 'accepted_rows', v_accepted,
      'quarantined_rows', v_rejected, 'source_deleted_rows', v_removed, 'dq_reasons', v_reasons);
END;
$$;
