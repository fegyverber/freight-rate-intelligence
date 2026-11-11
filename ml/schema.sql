CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS mart.ml_models (
    model_id text PRIMARY KEY,
    model_name text NOT NULL,
    as_of date NOT NULL,
    train_start date NOT NULL,
    train_end_exclusive date NOT NULL,
    train_rows integer NOT NULL CHECK (train_rows > 1),
    train_data_hash text NOT NULL,
    feature_code_hash text NOT NULL,
    feature_columns jsonb NOT NULL,
    hyperparameters jsonb NOT NULL,
    library_versions jsonb NOT NULL,
    artifact_relative_path text NOT NULL,
    artifact_sha256 text NOT NULL,
    trained_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mart.ml_evaluations (
    as_of date NOT NULL,
    model_id text NOT NULL REFERENCES mart.ml_models(model_id),
    test_rows integer NOT NULL CHECK (test_rows > 1),
    test_data_hash text NOT NULL,
    metrics jsonb NOT NULL,
    evaluated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (as_of, model_id)
);

-- A snapshot has one prediction per deal, even when the model is retrained.
-- Other as_of snapshots remain available as evaluation history.
CREATE TABLE IF NOT EXISTS mart.ml_price_predictions (
    as_of date NOT NULL,
    price_id bigint NOT NULL,
    loading_date date NOT NULL,
    loading_city_gcid integer NOT NULL,
    unloading_city_gcid integer NOT NULL,
    body_group text NOT NULL,
    split text NOT NULL CHECK (split IN ('test', 'future')),
    actual_price_rub double precision,
    predicted_price_rub double precision NOT NULL
        CHECK (predicted_price_rub > 0 AND predicted_price_rub < 'Infinity'::float8),
    abs_error_rub double precision,
    ape_pct double precision,
    model_id text NOT NULL REFERENCES mart.ml_models(model_id),
    model_name text NOT NULL,
    scored_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (as_of, price_id),
    CHECK (
        (split = 'future' AND loading_date > as_of
         AND actual_price_rub IS NULL AND abs_error_rub IS NULL AND ape_pct IS NULL)
        OR
        (split = 'test' AND loading_date <= as_of
         AND actual_price_rub > 0 AND abs_error_rub >= 0 AND ape_pct >= 0
         AND actual_price_rub IS NOT NULL AND abs_error_rub IS NOT NULL
         AND ape_pct IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ml_predictions_route_date_idx
    ON mart.ml_price_predictions
    (loading_city_gcid, unloading_city_gcid, loading_date);
CREATE INDEX IF NOT EXISTS ml_predictions_model_idx
    ON mart.ml_price_predictions (model_id, split);
