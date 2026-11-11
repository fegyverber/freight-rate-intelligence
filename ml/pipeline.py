"""Reproducible CatBoost training, honest holdout evaluation and batch scoring.

The caller supplies a psycopg 3 connection and owns its lifetime. Reading the
sample and publishing predictions use separate short transactions; training
does not keep a database transaction open.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from ml.features import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    FEATURE_VERSION,
    SplitBounds,
    build_features,
    temporal_split,
)

MODULE_DIR = Path(__file__).resolve().parent
MODEL_NAME = "CatBoostRegressor"
PREDICTION_COLUMNS = (
    "as_of", "price_id", "loading_date", "loading_city_gcid",
    "unloading_city_gcid", "body_group", "split", "actual_price_rub",
    "predicted_price_rub", "abs_error_rub", "ape_pct", "model_id", "model_name",
)


def _log(event: str, **fields: object) -> None:
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }, ensure_ascii=False, default=str), flush=True)


def _write_json(path: Path, value: dict) -> None:
    """Atomic local replacement: a failed write leaves the prior artifact intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n", encoding="utf-8",
    )
    temporary.replace(path)


def dataframe_hash(frame: pd.DataFrame) -> str:
    """Stable within the pinned pandas version; includes schema and row order."""
    digest = hashlib.sha256()
    schema = [(column, str(frame[column].dtype)) for column in frame.columns]
    digest.update(json.dumps(schema, ensure_ascii=False).encode("utf-8"))
    hashes = pd.util.hash_pandas_object(frame, index=False).to_numpy(dtype="<u8")
    digest.update(hashes.tobytes())
    return digest.hexdigest()


def calculate_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    actual = np.asarray(actual, dtype=np.float64)
    predicted = np.asarray(predicted, dtype=np.float64)
    if len(actual) < 2 or actual.shape != predicted.shape:
        raise ValueError("Evaluation needs at least two paired observations")
    if not np.isfinite(actual).all() or not np.isfinite(predicted).all():
        raise ValueError("Metrics require finite labels and predictions")
    if np.any(actual <= 0):
        raise ValueError("MAPE requires positive price_rub labels")
    return {
        "r2": float(r2_score(actual, predicted)),
        "mape_pct": float(np.mean(np.abs(predicted - actual) / actual) * 100.0),
        "rmse_rub": float(np.sqrt(mean_squared_error(actual, predicted))),
        "mae_rub": float(mean_absolute_error(actual, predicted)),
    }


def prediction_frame(
    frame: pd.DataFrame, predictions: np.ndarray, as_of: date, model_id: str
) -> pd.DataFrame:
    """Censor future targets again at the persistence boundary, defensively."""
    predictions = np.asarray(predictions, dtype=np.float64)
    if len(frame) != len(predictions) or not np.isfinite(predictions).all():
        raise ValueError("Prediction count or finite-value check failed")
    if np.any(predictions <= 0):
        raise ValueError("Predicted tariffs must be positive")
    output = frame.loc[:, [
        "price_id", "loading_date", "loading_city_gcid",
        "unloading_city_gcid", "body_group",
    ]].reset_index(drop=True).copy()
    output["loading_date"] = pd.to_datetime(output["loading_date"]).dt.date
    future = output["loading_date"].gt(as_of)
    output["as_of"] = as_of
    output["split"] = np.where(future, "future", "test")
    output["actual_price_rub"] = pd.to_numeric(
        frame["actual_price_rub"], errors="coerce"
    ).to_numpy(dtype=float)
    output.loc[future, "actual_price_rub"] = np.nan
    if output.loc[~future, "actual_price_rub"].isna().any():
        raise ValueError("Historical scored rows must have an observed target")
    output["predicted_price_rub"] = predictions
    output["abs_error_rub"] = (
        output["predicted_price_rub"] - output["actual_price_rub"]
    ).abs()
    output["ape_pct"] = output["abs_error_rub"] / output["actual_price_rub"] * 100.0
    output["model_id"] = model_id
    output["model_name"] = MODEL_NAME
    return output.loc[:, PREDICTION_COLUMNS]


def _load_dataset(connection, as_of: date) -> pd.DataFrame:
    bounds = SplitBounds.from_as_of(as_of)
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                (MODULE_DIR / "dataset.sql").read_text(encoding="utf-8"),
                {"train_start": bounds.train_start, "as_of": as_of},
            )
            # Only the three-month history and future pool are materialized.
            # No per-row DB queries, and no target aggregates are queried.
            columns = [column.name for column in cursor.description]
            return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)


def _model_config(seed: int, iterations: int, thread_count: int) -> dict:
    if iterations < 1 or thread_count < 1:
        raise ValueError("iterations and thread_count must be positive")
    return {
        "iterations": iterations,
        "depth": 6,
        "learning_rate": 0.06,
        "loss_function": "RMSE",
        "random_seed": seed,
        "thread_count": thread_count,
        "task_type": "CPU",
        "boosting_type": "Ordered",
        "has_time": True,
        "use_best_model": False,
        "allow_writing_files": False,
        "verbose": False,
    }


def _publish(connection, predictions: pd.DataFrame, manifest: dict, report: dict) -> dict:
    """Publish the complete snapshot atomically, keeping unchanged timestamps."""
    from psycopg.types.json import Jsonb

    columns = ", ".join(PREDICTION_COLUMNS)
    changed_columns = PREDICTION_COLUMNS[2:]
    update_assignments = ", ".join(
        f"{column} = EXCLUDED.{column}" for column in changed_columns
    )
    old_row = ", ".join(f"p.{column}" for column in changed_columns)
    new_row = ", ".join(f"EXCLUDED.{column}" for column in changed_columns)
    csv = io.StringIO()
    predictions.to_csv(csv, index=False, header=False, na_rep="\\N", float_format="%.17g")
    csv.seek(0)
    with connection.transaction():
        with connection.cursor() as cursor:
            # Use the same lock as ETL and market-mart refreshes.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext('smart_logistics.pipeline'))"
            )
            cursor.execute((MODULE_DIR / "schema.sql").read_text(encoding="utf-8"))
            cursor.execute("""
                INSERT INTO mart.ml_models (
                    model_id, model_name, as_of, train_start, train_end_exclusive,
                    train_rows, train_data_hash, feature_code_hash, feature_columns,
                    hyperparameters, library_versions, artifact_relative_path, artifact_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (model_id) DO UPDATE
                SET artifact_sha256 = EXCLUDED.artifact_sha256,
                    artifact_relative_path = EXCLUDED.artifact_relative_path
                WHERE (ml_models.artifact_sha256, ml_models.artifact_relative_path)
                    IS DISTINCT FROM
                      (EXCLUDED.artifact_sha256, EXCLUDED.artifact_relative_path)
            """, (
                manifest["model_id"], MODEL_NAME, report["as_of"],
                report["split"]["train_start_inclusive"],
                report["split"]["train_end_exclusive"], report["rows"]["train"],
                manifest["train_data_hash"], manifest["feature_code_hash"],
                Jsonb(list(FEATURE_COLUMNS)), Jsonb(manifest["hyperparameters"]),
                Jsonb(manifest["library_versions"]), manifest["artifact_relative_path"],
                manifest["artifact_sha256"],
            ))
            cursor.execute("""
                INSERT INTO mart.ml_evaluations
                    (as_of, model_id, test_rows, test_data_hash, metrics)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (as_of, model_id) DO UPDATE SET
                    test_rows = EXCLUDED.test_rows,
                    test_data_hash = EXCLUDED.test_data_hash,
                    metrics = EXCLUDED.metrics,
                    evaluated_at = CURRENT_TIMESTAMP
                WHERE (ml_evaluations.test_rows, ml_evaluations.test_data_hash,
                       ml_evaluations.metrics)
                    IS DISTINCT FROM
                      (EXCLUDED.test_rows, EXCLUDED.test_data_hash, EXCLUDED.metrics)
            """, (report["as_of"], manifest["model_id"], report["rows"]["test"],
                  report["test_data_hash"], Jsonb(report["metrics"])))
            cursor.execute("DROP TABLE IF EXISTS pg_temp.ml_prediction_stage")
            cursor.execute("""
                CREATE TEMP TABLE ml_prediction_stage
                (LIKE mart.ml_price_predictions INCLUDING DEFAULTS) ON COMMIT DROP
            """)
            with cursor.copy(
                f"COPY ml_prediction_stage ({columns}) FROM STDIN "
                r"WITH (FORMAT CSV, NULL '\N')"
            ) as copy:
                while block := csv.read(1024 * 1024):
                    copy.write(block)
            cursor.execute(f"""
                INSERT INTO mart.ml_price_predictions AS p ({columns})
                SELECT {columns} FROM ml_prediction_stage WHERE true
                ON CONFLICT (as_of, price_id) DO UPDATE
                SET {update_assignments}, scored_at = CURRENT_TIMESTAMP
                WHERE ({old_row}) IS DISTINCT FROM ({new_row})
            """)
            changed = cursor.rowcount
            # A corrected/deleted/reclassified deal must not leave a stale row.
            cursor.execute("""
                DELETE FROM mart.ml_price_predictions p
                WHERE p.as_of = %s AND NOT EXISTS (
                    SELECT 1 FROM ml_prediction_stage s
                    WHERE s.as_of = p.as_of AND s.price_id = p.price_id
                )
            """, (report["as_of"],))
            deleted = cursor.rowcount
    return {"prediction_rows_changed": changed, "stale_prediction_rows_deleted": deleted}


def _run_ml(
    connection,
    as_of: date,
    output_dir: Path,
    seed: int = 20261026,
    iterations: int = 500,
    thread_count: int = 2,
) -> dict:
    """Train on the fixed historical window and publish test + future scores."""
    from catboost import CatBoostRegressor

    as_of = date.fromisoformat(str(as_of))
    output_dir = Path(output_dir)
    bounds = SplitBounds.from_as_of(as_of)
    data = _load_dataset(connection, as_of)
    train, test, future = temporal_split(data, as_of)
    if len(train) < 2 or len(test) < 2:
        raise ValueError(
            f"Need at least 2 train and 2 test rows; got {len(train)} and {len(test)}. "
            "Load the complete requested date window before ML."
        )
    _log("ml_dataset_ready", as_of=as_of, extracted_rows=len(data),
         train_rows=len(train), test_rows=len(test), future_rows=len(future),
         split=bounds.as_dict())
    x_train = build_features(train)
    y_train = train["actual_price_rub"].to_numpy(dtype=np.float64)
    x_test = build_features(test)
    y_test = test["actual_price_rub"].to_numpy(dtype=np.float64)
    train_hash = dataframe_hash(x_train.assign(
        price_id=train["price_id"].to_numpy(), target=y_train,
    ))
    versions = {name: importlib.metadata.version(name)
                for name in ("catboost", "numpy", "pandas", "scikit-learn")}
    code_hash = hashlib.sha256(b"".join(
        (MODULE_DIR / filename).read_bytes()
        for filename in ("features.py", "dataset.sql", "pipeline.py")
    )).hexdigest()
    params = _model_config(seed, iterations, thread_count)
    identity = {
        "as_of": as_of.isoformat(), "train_data_hash": train_hash,
        "feature_version": FEATURE_VERSION, "feature_code_hash": code_hash,
        "hyperparameters": params, "library_versions": versions,
    }
    model_id = "catboost-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    model_dir = output_dir / "models" / model_id
    model_path = model_dir / "model.cbm"
    manifest_path = model_dir / "manifest.json"
    model = CatBoostRegressor(**params)
    cache_valid = False
    if model_path.exists() and manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        cache_valid = (
            previous.get("model_id") == model_id
            and previous.get("artifact_sha256") == hashlib.sha256(model_path.read_bytes()).hexdigest()
        )
    if cache_valid:
        model.load_model(str(model_path))
        _log("ml_model_reused", model_id=model_id)
    else:
        _log("ml_training_started", model_id=model_id, iterations=iterations)
        # No eval_set, early stopping, threshold selection or preprocessing fit
        # uses the test/future pools. Input is sorted by loading_date + price_id.
        model.fit(x_train, y_train, cat_features=list(CATEGORICAL_FEATURES))
        model_dir.mkdir(parents=True, exist_ok=True)
        temporary = model_path.with_suffix(f".{os.getpid()}.tmp")
        model.save_model(str(temporary), format="cbm")
        temporary.replace(model_path)
        _log("ml_training_completed", model_id=model_id)
    # Nonnegative domain constraint is fixed in advance and independent of test.
    test_prediction = np.maximum(model.predict(x_test), 0.01)
    metrics = calculate_metrics(y_test, test_prediction)
    all_scores = [prediction_frame(test, test_prediction, as_of, model_id)]
    if len(future):
        future_prediction = np.maximum(model.predict(build_features(future)), 0.01)
        all_scores.append(prediction_frame(future, future_prediction, as_of, model_id))
    predictions = pd.concat(all_scores, ignore_index=True)
    manifest = {
        **identity, "model_id": model_id, "model_name": MODEL_NAME,
        "feature_columns": list(FEATURE_COLUMNS),
        "categorical_features": list(CATEGORICAL_FEATURES),
        "artifact_relative_path": str(model_path.relative_to(output_dir)),
        "artifact_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "prediction_floor_rub": 0.01,
    }
    report = {
        "as_of": as_of.isoformat(), "model_id": model_id, "model_name": MODEL_NAME,
        "split": bounds.as_dict(),
        "rows": {"extracted": len(data), "train": len(train), "test": len(test),
                 "future": len(future), "predictions": len(predictions)},
        "metrics": metrics,
        "test_data_hash": dataframe_hash(x_test.assign(
            price_id=test["price_id"].to_numpy(), target=y_test,
        )),
        "prediction_data_hash": dataframe_hash(predictions),
        "target": "price_rub; all valid factual price_type values per assignment",
        "body_policy": "one row per deal; sorted distinct group set plus primary group",
        "limitations": [
            "2026-10-26 source outlier marks may not have been refreshed by the nightly job.",
            "Historical labels contain mixed customer, forwarder and carrier roles as required.",
            "Future scoring covers DQ-valid simple requests available in core; quarantined requests are excluded.",
            "A three-month training window cannot establish a complete annual seasonal pattern or long-term trend.",
            "This is a fixed-snapshot evaluation, not a reconstruction of historical source versions.",
        ],
    }
    _write_json(manifest_path, manifest)
    publication = _publish(connection, predictions, manifest, report)
    _write_json(output_dir / "ml_manifest.json", manifest)
    _write_json(output_dir / "ml_metrics.json", report)
    pd.DataFrame({
        "feature": FEATURE_COLUMNS,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False).to_csv(
        output_dir / "ml_feature_importance.csv", index=False,
    )
    predictions.groupby("split", sort=True).head(20).to_csv(
        output_dir / "ml_predictions_sample.csv", index=False,
    )
    _log("ml_scoring_completed", model_id=model_id, metrics=metrics,
         **report["rows"], **publication)
    return report


def run_ml(
    connection,
    as_of: date,
    output_dir: Path,
    seed: int = 20261026,
    iterations: int = 500,
    thread_count: int = 2,
) -> dict:
    """Serialize with ETL/mart refreshes without keeping a training transaction."""
    with connection.transaction():
        connection.execute(
            "SELECT pg_advisory_lock(hashtext('smart_logistics.pipeline'))"
        )
    try:
        return _run_ml(connection, as_of, output_dir, seed, iterations, thread_count)
    finally:
        with connection.transaction():
            connection.execute(
                "SELECT pg_advisory_unlock(hashtext('smart_logistics.pipeline'))"
            )


def main() -> None:
    from etl.db import connect

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat,
                        default=os.getenv("AS_OF", "2026-10-26"))
    parser.add_argument("--output-dir", type=Path,
                        default=Path(os.getenv("ARTIFACT_DIR", "artifacts")))
    parser.add_argument("--seed", type=int, default=int(os.getenv("RANDOM_SEED", "20261026")))
    parser.add_argument("--iterations", type=int, default=int(os.getenv("ML_ITERATIONS", "500")))
    parser.add_argument("--threads", type=int,
                        default=int(os.getenv("ML_THREAD_COUNT", "2")))
    args = parser.parse_args()
    with connect() as connection:
        run_ml(connection, args.as_of, args.output_dir, args.seed,
               args.iterations, args.threads)


if __name__ == "__main__":
    main()
