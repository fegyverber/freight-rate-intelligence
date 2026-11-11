from datetime import date

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from ml.features import (
    CATEGORICAL_FEATURES, FEATURE_COLUMNS, SplitBounds,
    build_features, primary_body_group, split_location, temporal_split,
)


def feature_row(**updates):
    row = dict(
        price_id=1, loading_date="2026-09-30", loading_city_gcid=1001,
        unloading_city_gcid=1003, distance=897.0, total_weight=20.0,
        total_volume=82.0, origin_full_name="Москва, г. Москва",
        destination_full_name="Казань, Республика Татарстан",
        origin_latitude=55.7558, origin_longitude=37.6173,
        destination_latitude=55.7961, destination_longitude=49.1064,
        body_type=56, body_group="Тент", weight_category="FTL",
        volume_category="FTL", actual_price_rub=84500.0,
        is_fact=True, price_type=3, complex=False, outlier_mark=False,
    )
    return {**row, **updates}


def test_features_never_depend_on_target_or_group_statistics():
    frame = pd.DataFrame([feature_row(), feature_row(price_id=2, body_type=46)])
    original = build_features(frame)
    for column in (
        "price", "price_rub", "actual_price_rub", "price_with_vat",
        "price_km", "carrier_cost", "customer_cost", "profit",
        "price_group_id", "avg_price", "min_km_price", "max_km_price",
        "organization_id", "outlier_mark", "is_fact", "price_type",
    ):
        frame[column] = [99999999, -100]
    assert_frame_equal(original, build_features(frame))
    assert tuple(original.columns) == FEATURE_COLUMNS


def test_region_extraction_is_separate_and_preserves_remainder():
    cities, regions = split_location(pd.Series([
        "Казань, Республика Татарстан", "Москва, г. Москва", "Город без региона",
        " Посёлок , Район, Регион ", None,
    ]))
    assert cities.tolist() == ["Казань", "Москва", "Город без региона", "Посёлок", "Не указан"]
    assert regions.tolist() == ["Республика Татарстан", "г. Москва", "Не указан",
                                "Район, Регион", "Не указан"]
    features = build_features(pd.DataFrame([feature_row()]))
    assert features.loc[0, "origin_region"] == "г. Москва"
    assert features.loc[0, "destination_region"] == "Республика Татарстан"


@pytest.mark.parametrize("code,group", [
    (None, "Не указан"), (1, "Борт"), (15, "Борт"), (2, "Изотерм"),
    (4, "Рефрижератор"), (46, "Рефрижератор"), (47, "Рефрижератор"),
    (48, "Рефрижератор"), (56, "Тент"), (57, "Тент"), (61, "Тент"),
    (68, "Прочие"), (999, "Прочие"),
])
def test_primary_body_group_handles_source_codes(code, group):
    assert primary_body_group(pd.Series([code])).iloc[0] == group


def test_multi_body_deal_stays_a_single_training_observation():
    frame = pd.DataFrame([feature_row(body_group="Борт | Тент")])
    features = build_features(frame)
    assert len(features) == 1
    assert features.loc[0, "body_group"] == "Борт | Тент"
    assert features.loc[0, "primary_body_group"] == "Тент"


def test_cyclic_components_are_on_unit_circle_and_wrap_calendar():
    frame = pd.DataFrame([
        feature_row(price_id=1, loading_date="2026-12-31"),
        feature_row(price_id=2, loading_date="2027-01-01"),
        feature_row(price_id=3, loading_date="2026-10-04"),
        feature_row(price_id=4, loading_date="2026-10-05"),
    ])
    features = build_features(frame)
    for prefix in ("month", "weekday", "dayofyear"):
        assert np.allclose(features[f"{prefix}_sin"] ** 2
                           + features[f"{prefix}_cos"] ** 2, 1.0)
    year_boundary = features.loc[0, ["dayofyear_sin", "dayofyear_cos"]].to_numpy(float) - \
        features.loc[1, ["dayofyear_sin", "dayofyear_cos"]].to_numpy(float)
    assert np.linalg.norm(year_boundary) < 0.03
    # Sunday and Monday are adjacent even though their integer codes are 6/0.
    weekday_boundary = features.loc[2, ["weekday_sin", "weekday_cos"]].to_numpy(float) - \
        features.loc[3, ["weekday_sin", "weekday_cos"]].to_numpy(float)
    assert np.linalg.norm(weekday_boundary) == pytest.approx(2 * np.sin(np.pi / 7))


def test_fixed_date_boundaries_and_no_overlap_or_future_labels():
    dates = ["2026-07-25", "2026-07-26", "2026-10-04", "2026-10-05",
             "2026-10-26", "2026-10-27", "2027-01-10"]
    frame = pd.DataFrame([feature_row(price_id=i, loading_date=d)
                          for i, d in enumerate(dates)])
    train, test, future = temporal_split(frame, date(2026, 10, 26))
    assert train.price_id.tolist() == [1, 2]
    assert test.price_id.tolist() == [3, 4]
    assert future.price_id.tolist() == [5, 6]
    assert future.actual_price_rub.isna().all()
    assert set(train.price_id).isdisjoint(test.price_id)


def test_temporal_filter_excludes_invalid_history_but_scores_planned_bids():
    frame = pd.DataFrame([
        feature_row(price_id=1, outlier_mark=True),
        feature_row(price_id=2, complex=True),
        feature_row(price_id=3, is_fact=False, price_type=6),
        feature_row(price_id=4, price_type=1),
        feature_row(price_id=5, price_type=2),
        feature_row(price_id=6, loading_date="2026-11-01", is_fact=False,
                    price_type=6, outlier_mark=True),
        feature_row(price_id=7, loading_date="2026-11-02", complex=True),
    ])
    train, test, future = temporal_split(frame, date(2026, 10, 26))
    assert train.price_id.tolist() == [4, 5]
    assert test.empty
    assert future.price_id.tolist() == [6]
    assert future.actual_price_rub.isna().all()


def test_duplicate_price_ids_are_rejected_before_training():
    with pytest.raises(ValueError, match="one row per price_id"):
        temporal_split(pd.DataFrame([feature_row(), feature_row()]), date(2026, 10, 26))


def test_calendar_months_are_used_for_train_start():
    bounds = SplitBounds.from_as_of(date(2026, 5, 31))
    assert bounds.train_start == date(2026, 2, 28)
    assert bounds.test_start == date(2026, 5, 10)


def test_missing_categories_and_numeric_nan_need_no_fitted_imputer():
    frame = pd.DataFrame([feature_row(body_type=None, body_group=None,
                                      origin_latitude=None, weight_category=None)])
    features = build_features(frame)
    assert pd.isna(features.loc[0, "origin_latitude"])
    assert not features[list(CATEGORICAL_FEATURES)].isna().any().any()
    assert features.loc[0, "body_group"] == "Не указан"


def test_missing_feature_input_fails_clearly():
    with pytest.raises(ValueError, match="Missing feature inputs"):
        build_features(pd.DataFrame([feature_row()]).drop(columns="distance"))
