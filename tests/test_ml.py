from datetime import date

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from ml.features import CATEGORICAL_FEATURES, build_features
from ml.pipeline import (
    _model_config, calculate_metrics, dataframe_hash, prediction_frame,
)


def test_metrics_units_and_values():
    actual = np.array([100.0, 200.0])
    predicted = np.array([110.0, 180.0])
    result = calculate_metrics(actual, predicted)
    assert result == pytest.approx({
        "r2": 0.9, "mape_pct": 10.0, "rmse_rub": np.sqrt(250), "mae_rub": 15.0,
    })


@pytest.mark.parametrize("actual,predicted", [
    ([0, 100], [1, 90]), ([1, np.nan], [1, 2]), ([1, 2], [1, np.inf]),
    ([1], [1]), ([1, 2], [1]),
])
def test_invalid_metrics_cannot_silently_look_successful(actual, predicted):
    with pytest.raises(ValueError):
        calculate_metrics(np.array(actual), np.array(predicted))


def test_future_targets_and_errors_are_null_even_if_price_is_present():
    frame = pd.DataFrame({
        "price_id": [1, 2], "loading_date": ["2026-10-26", "2026-10-27"],
        "loading_city_gcid": [1001, 1001], "unloading_city_gcid": [1003, 1003],
        "body_group": ["Тент", "Тент"], "actual_price_rub": [100.0, 1000000.0],
    })
    result = prediction_frame(frame, np.array([120.0, 150.0]),
                              date(2026, 10, 26), "model-test")
    assert result.split.tolist() == ["test", "future"]
    assert result.loc[0, "abs_error_rub"] == 20.0
    assert result.loc[0, "ape_pct"] == 20.0
    assert result.loc[1, ["actual_price_rub", "abs_error_rub", "ape_pct"]].isna().all()
    frame.loc[1, "actual_price_rub"] = 1.0
    assert_frame_equal(result, prediction_frame(frame, np.array([120.0, 150.0]),
                                                date(2026, 10, 26), "model-test"))


def test_content_hash_detects_changed_labels_and_is_index_independent():
    frame = pd.DataFrame({"price_id": [1, 2], "target": [100.0, 200.0]})
    original = dataframe_hash(frame)
    assert original == dataframe_hash(frame.set_axis([9, 10]))
    frame.loc[1, "target"] = 201.0
    assert original != dataframe_hash(frame)


def test_catboost_is_reproducible_and_reloadable_without_test_fitting(tmp_path):
    from catboost import CatBoostRegressor

    # A tiny, genuinely learnable sample tests the actual model integration,
    # numeric NaN handling, new categories and persisted model round-tripping.
    frame = pd.DataFrame({
        "loading_date": pd.date_range("2026-08-01", periods=60),
        "distance": np.linspace(100, 1500, 60),
        "total_weight": np.tile([5.0, 20.0], 30),
        "total_volume": 80.0, "origin_full_name": "Москва, г. Москва",
        "destination_full_name": "Казань, Республика Татарстан",
        "origin_latitude": np.nan, "origin_longitude": 37.6173,
        "destination_latitude": 55.7961, "destination_longitude": 49.1064,
        "body_type": np.tile([56, 4], 30), "body_group": np.tile(["Тент", "Рефрижератор"], 30),
        "weight_category": np.tile(["10T", "FTL"], 30), "volume_category": "FTL",
    })
    features = build_features(frame)
    target = frame.distance.to_numpy() * 70 + frame.total_weight.to_numpy() * 150
    params = _model_config(seed=42, iterations=15, thread_count=1)
    first = CatBoostRegressor(**params)
    second = CatBoostRegressor(**params)
    first.fit(features, target, cat_features=list(CATEGORICAL_FEATURES))
    second.fit(features, target, cat_features=list(CATEGORICAL_FEATURES))
    holdout = features.tail(4).copy()
    holdout["destination_region"] = "Ранее не встречавшийся регион"
    expected = first.predict(holdout)
    assert np.isfinite(expected).all()
    assert np.array_equal(expected, second.predict(holdout))
    path = tmp_path / "model.cbm"
    first.save_model(str(path))
    loaded = CatBoostRegressor()
    loaded.load_model(str(path))
    assert np.array_equal(expected, loaded.predict(holdout))


def test_pipeline_does_not_retrain_or_change_model_when_only_test_labels_change(
    monkeypatch, tmp_path
):
    import catboost
    import ml.pipeline as pipeline

    frame = pd.DataFrame({
        "price_id": range(1, 7),
        "loading_date": ["2026-07-26", "2026-08-25", "2026-10-04",
                         "2026-10-05", "2026-10-26", "2026-11-10"],
        "loading_city_gcid": 1001, "unloading_city_gcid": 1003,
        "distance": [100, 200, 300, 400, 500, 600],
        "total_weight": 20.0, "total_volume": 82.0,
        "origin_full_name": "Москва, г. Москва",
        "destination_full_name": "Казань, Республика Татарстан",
        "origin_latitude": 55.7558, "origin_longitude": 37.6173,
        "destination_latitude": 55.7961, "destination_longitude": 49.1064,
        "body_type": 56, "body_group": "Тент", "weight_category": "FTL",
        "volume_category": "FTL", "is_fact": True, "price_type": 3,
        "complex": False, "outlier_mark": False,
        "actual_price_rub": [10000.0, 20000.0, 30000.0, 40000.0, 50000.0, 999999.0],
    })
    fitted = []
    published = []

    class RecordingModel:
        def __init__(self, **params):
            assert "eval_set" not in params
            self.feature_importances_ = np.zeros(len(pipeline.FEATURE_COLUMNS))

        def fit(self, features, target, **kwargs):
            assert "eval_set" not in kwargs
            fitted.append((features.copy(), target.copy()))

        def predict(self, features):
            return features["distance"].to_numpy() * 90.0

        def save_model(self, path, **kwargs):
            from pathlib import Path
            Path(path).write_bytes(b"test model")

        def load_model(self, path):
            pass

    def record_publication(connection, predictions, manifest, report):
        published.append(predictions.copy())
        return {}

    monkeypatch.setattr(catboost, "CatBoostRegressor", RecordingModel)
    monkeypatch.setattr(pipeline, "_load_dataset", lambda connection, as_of: frame.copy())
    monkeypatch.setattr(pipeline, "_publish", record_publication)
    first = pipeline._run_ml(None, date(2026, 10, 26), tmp_path, iterations=10)
    frame.loc[frame.price_id.isin([4, 5]), "actual_price_rub"] *= 3
    frame.loc[frame.price_id.eq(6), "actual_price_rub"] = 1.0
    second = pipeline._run_ml(None, date(2026, 10, 26), tmp_path, iterations=10)
    assert first["model_id"] == second["model_id"]
    assert first["metrics"] != second["metrics"]
    assert len(fitted) == 1
    assert fitted[0][0].distance.tolist() == [100.0, 200.0, 300.0]
    assert fitted[0][1].tolist() == [10000.0, 20000.0, 30000.0]
    assert len(published[0]) == len(published[1]) == 3
    assert_frame_equal(
        published[0].query("split == 'future'").reset_index(drop=True),
        published[1].query("split == 'future'").reset_index(drop=True),
    )
