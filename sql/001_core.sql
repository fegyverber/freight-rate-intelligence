CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS core.dim_cities (
    gc_id integer PRIMARY KEY,
    source_city_id bigint NOT NULL,
    name text NOT NULL,
    full_name_local text NOT NULL,
    region text NOT NULL,
    latitude double precision,
    longitude double precision
);

CREATE TABLE IF NOT EXISTS core.dim_body_groups (
    body_group_id integer PRIMARY KEY,
    body_group text NOT NULL UNIQUE
);
INSERT INTO core.dim_body_groups VALUES
    (56, 'Тент'), (4, 'Рефрижератор'), (2, 'Изотерм'),
    (15, 'Борт'), (0, 'Прочие'), (-1, 'Не указан')
ON CONFLICT (body_group_id) DO UPDATE SET body_group = EXCLUDED.body_group;

CREATE TABLE IF NOT EXISTS core.prices (
    price_id bigint PRIMARY KEY,
    source_created_at timestamp,
    source_updated_at timestamp,
    subscriber_id integer,
    organization_id integer,
    loading_date date NOT NULL,
    end_loading_date date,
    loading_city_gcid integer NOT NULL REFERENCES core.dim_cities(gc_id),
    unloading_city_gcid integer NOT NULL REFERENCES core.dim_cities(gc_id),
    distance double precision NOT NULL CHECK (distance > 0 AND distance < 'Infinity'::float8),
    total_weight double precision NOT NULL CHECK (total_weight >= 0 AND total_weight < 'Infinity'::float8),
    total_volume double precision NOT NULL CHECK (total_volume >= 0 AND total_volume < 'Infinity'::float8),
    body_type integer,
    price double precision NOT NULL CHECK (price > 0 AND price < 'Infinity'::float8),
    currency_code text NOT NULL,
    nearest_rate_date date,
    fx_rate numeric NOT NULL CHECK (fx_rate > 0 AND fx_rate < 'Infinity'::numeric),
    price_rub numeric NOT NULL CHECK (price_rub > 0 AND price_rub < 'Infinity'::numeric),
    price_type smallint NOT NULL,
    is_fact boolean NOT NULL,
    complex boolean NOT NULL,
    outlier_mark boolean NOT NULL,
    weight_category text NOT NULL,
    volume_category text NOT NULL,
    source text
);
CREATE INDEX IF NOT EXISTS core_prices_loading_date_idx ON core.prices(loading_date);
CREATE INDEX IF NOT EXISTS core_prices_route_idx
    ON core.prices(loading_city_gcid, unloading_city_gcid, weight_category);

CREATE TABLE IF NOT EXISTS core.price_body_groups (
    price_id bigint NOT NULL REFERENCES core.prices(price_id) ON DELETE CASCADE,
    body_group_id integer NOT NULL REFERENCES core.dim_body_groups(body_group_id),
    body_group text NOT NULL,
    PRIMARY KEY(price_id, body_group_id)
);
CREATE TABLE IF NOT EXISTS core.price_dates (
    price_id bigint NOT NULL REFERENCES core.prices(price_id) ON DELETE CASCADE,
    price_date date NOT NULL,
    PRIMARY KEY(price_id, price_date)
);
CREATE INDEX IF NOT EXISTS core_price_dates_date_idx ON core.price_dates(price_date, price_id);

CREATE TABLE IF NOT EXISTS core.dq_quarantine_prices (
    price_id bigint PRIMARY KEY,
    loading_date date NOT NULL,
    reason_codes text[] NOT NULL CHECK (cardinality(reason_codes) > 0),
    source_payload jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS dq_quarantine_loading_date_idx ON core.dq_quarantine_prices(loading_date);

CREATE TABLE IF NOT EXISTS ops.pipeline_runs (
    run_id uuid PRIMARY KEY,
    command text NOT NULL,
    start_date date,
    end_date date,
    as_of date NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
    result jsonb,
    error text
);

CREATE OR REPLACE FUNCTION core.finite(value double precision)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT value IS NOT NULL AND value > '-Infinity'::float8 AND value < 'Infinity'::float8
$$;

CREATE OR REPLACE FUNCTION core.weight_category(weight double precision)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
      WHEN round(weight::numeric, 1) >= 30 THEN 'G20T'
      WHEN round(weight::numeric, 1) >= 10 THEN 'FTL'
      WHEN round(weight::numeric, 1) >= 5 THEN '10T'
      WHEN round(weight::numeric, 1) >= 2.5 THEN '5T'
      WHEN round(weight::numeric, 1) >= 0.5 THEN 'L5T'
      ELSE 'OTH' END
$$;

CREATE OR REPLACE FUNCTION core.body_group_id(body_type integer, source_std_id integer DEFAULT NULL)
RETURNS integer LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
      WHEN body_type IS NULL THEN -1
      WHEN body_type IN (56,57,58,59,60,61) THEN 56
      WHEN body_type IN (4,46,47,48) THEN 4
      WHEN body_type = 2 THEN 2
      WHEN body_type IN (1,15) THEN 15
      WHEN source_std_id IN (56,4,2,15) THEN source_std_id
      ELSE 0 END
$$;
