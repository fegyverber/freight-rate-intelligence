"""Pure feature transformations: no fitting and no access to target statistics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

FACT_PRICE_TYPES = (1, 2, 3, 4, 5, 9)
UNKNOWN_CATEGORY = "Не указан"
FEATURE_VERSION = "1"

CATEGORICAL_FEATURES = (
    "origin_city",
    "destination_city",
    "origin_region",
    "destination_region",
    "body_group",
    "primary_body_group",
    "weight_category",
    "volume_category",
)
NUMERIC_FEATURES = (
    "distance",
    "total_weight",
    "total_volume",
    "origin_latitude",
    "origin_longitude",
    "destination_latitude",
    "destination_longitude",
    "year",
    "month_sin",
    "month_cos",
    "weekday_sin",
    "weekday_cos",
    "dayofyear_sin",
    "dayofyear_cos",
)
# This allowlist is the only frame passed to the model. Extra DB columns can
# never silently turn into features, including an accidentally added price.
FEATURE_COLUMNS = NUMERIC_FEATURES + CATEGORICAL_FEATURES

SOURCE_FEATURE_COLUMNS = (
    "loading_date",
    "distance",
    "total_weight",
    "total_volume",
    "origin_full_name",
    "destination_full_name",
    "origin_latitude",
    "origin_longitude",
    "destination_latitude",
    "destination_longitude",
    "body_type",
    "body_group",
    "weight_category",
    "volume_category",
)


@dataclass(frozen=True)
class SplitBounds:
    train_start: date
    test_start: date
    as_of: date

    @classmethod
    def from_as_of(cls, as_of: date) -> "SplitBounds":
        # Calendar months, not an approximate 90-day interval.
        start = (pd.Timestamp(as_of) - pd.DateOffset(months=3)).date()
        return cls(start, as_of - timedelta(weeks=3), as_of)

    def as_dict(self) -> dict[str, str]:
        return {
            "train_start_inclusive": self.train_start.isoformat(),
            "train_end_exclusive": self.test_start.isoformat(),
            "test_start_inclusive": self.test_start.isoformat(),
            "test_end_inclusive": self.as_of.isoformat(),
        }


def _category(values: pd.Series) -> pd.Series:
    return values.astype("string").str.strip().replace("", pd.NA).fillna(
        UNKNOWN_CATEGORY
    ).astype(str)


def split_location(full_name: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Split once: a region suffix containing more commas is preserved."""
    parts = full_name.astype("string").str.partition(",", expand=True)
    return _category(parts[0]), _category(parts[2])


def primary_body_group(values: pd.Series) -> pd.Series:
    codes = pd.to_numeric(values, errors="coerce")
    conditions = [
        codes.isna(),
        codes.isin((56, 57, 58, 59, 60, 61)),
        codes.isin((4, 46, 47, 48)),
        codes.eq(2),
        codes.isin((1, 15)),
    ]
    return pd.Series(
        np.select(
            conditions,
            [UNKNOWN_CATEGORY, "Тент", "Рефрижератор", "Изотерм", "Борт"],
            default="Прочие",
        ),
        index=values.index,
    )


def build_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a deterministic feature frame; labels and IDs are never read."""
    missing = set(SOURCE_FEATURE_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing feature inputs: {sorted(missing)}")
    features = pd.DataFrame(index=frame.index)
    for column in NUMERIC_FEATURES[:7]:
        # CatBoost handles numeric NaN itself; no all-data imputation occurs.
        features[column] = pd.to_numeric(frame[column], errors="raise").astype(
            "float64"
        ).replace([np.inf, -np.inf], np.nan)
    dates = pd.to_datetime(frame["loading_date"], errors="raise")
    if dates.isna().any():
        raise ValueError("loading_date cannot be missing")
    features["year"] = dates.dt.year.astype("float64")
    cyclic = {
        "month": (dates.dt.month - 1, 12.0),
        "weekday": (dates.dt.dayofweek, 7.0),
        "dayofyear": (dates.dt.dayofyear - 1, 365.25),
    }
    for name, (position, period) in cyclic.items():
        angle = 2.0 * np.pi * position / period
        features[f"{name}_sin"] = np.sin(angle)
        features[f"{name}_cos"] = np.cos(angle)
    features["origin_city"], features["origin_region"] = split_location(
        frame["origin_full_name"]
    )
    features["destination_city"], features["destination_region"] = split_location(
        frame["destination_full_name"]
    )
    for column in ("body_group", "weight_category", "volume_category"):
        features[column] = _category(frame[column])
    features["primary_body_group"] = primary_body_group(frame["body_type"])
    return features.loc[:, FEATURE_COLUMNS]


def temporal_split(
    frame: pd.DataFrame, as_of: date
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split at deal grain and censor targets of future rows immediately."""
    if frame["price_id"].duplicated().any():
        raise ValueError("ML input must contain exactly one row per price_id")
    bounds = SplitBounds.from_as_of(as_of)
    data = frame.copy()
    data["loading_date"] = pd.to_datetime(data["loading_date"], errors="raise")
    target = pd.to_numeric(data["actual_price_rub"], errors="coerce")
    factual = (
        data["is_fact"].eq(True)
        & data["price_type"].isin(FACT_PRICE_TYPES)
        & data["complex"].eq(False)
        & data["outlier_mark"].eq(False)
        & np.isfinite(target)
        & target.gt(0)
    )
    loading = data["loading_date"]
    train_mask = factual & loading.ge(pd.Timestamp(bounds.train_start)) & loading.lt(
        pd.Timestamp(bounds.test_start)
    )
    test_mask = factual & loading.ge(pd.Timestamp(bounds.test_start)) & loading.le(
        pd.Timestamp(bounds.as_of)
    )
    # A future request has no observed target. Its current quoted price and
    # target-derived outlier flag must not decide whether it can be scored.
    future_mask = data["complex"].eq(False) & loading.gt(pd.Timestamp(bounds.as_of))
    train, test, future = (
        data.loc[mask].sort_values(["loading_date", "price_id"]).reset_index(drop=True)
        for mask in (train_mask, test_mask, future_mask)
    )
    future["actual_price_rub"] = np.nan
    return train, test, future
