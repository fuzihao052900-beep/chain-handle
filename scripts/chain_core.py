from __future__ import annotations

import csv
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import pandas as pd


class InputValidationError(ValueError):
    """The source table cannot be interpreted as a trip-chain table."""


@dataclass(frozen=True)
class FieldMapping:
    longitude: str
    latitude: str
    datetime: str | None = None
    date: str | None = None
    time: str | None = None
    track_id: str | None = None
    speed: str | None = None
    distance: str | None = None


@dataclass(frozen=True)
class InspectionResult:
    discovered_fields: tuple[str, ...]
    mapping: FieldMapping
    time_precision: str


@dataclass(frozen=True)
class PreparedInput:
    frame: pd.DataFrame
    mapping: FieldMapping
    source_name: str
    time_precision: str
    source_rows: int
    valid_rows: int
    rejected_rows: Mapping[str, int]
    group_counts: Mapping[GroupKey, tuple[int, int, int]]


@dataclass(frozen=True, order=True)
class GroupKey:
    track_id: str
    date: str


@dataclass(frozen=True)
class GroupSummary:
    track_id: str
    date: str
    input_rows: int
    representative_points: int
    output_points: int
    merged_rows: int
    stop_points: int
    start_time: pd.Timestamp | None
    end_time: pd.Timestamp | None
    time_anomalies: int
    status: str
    valid_rows: int = 0
    rejected_rows: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class TrajectoryResult:
    key: GroupKey
    frame: pd.DataFrame
    summary: GroupSummary


DistanceFunction = Callable[[float, float, float, float], float]


def normalize_header(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).lstrip("\ufeff").strip().casefold()
    return "".join(character for character in text if character.isalnum())


_ALIASES = {
    "longitude": {"longitude", "lon", "lng", "x", "经度"},
    "latitude": {"latitude", "lat", "y", "纬度"},
    "datetime": {"datetime", "timestamp", "date_time", "日期时间"},
    "date": {"date", "day", "日期"},
    "time": {"time", "时刻", "时间"},
    "track_id": {"trackid", "track_id", "trajectory_id", "轨迹编号", "轨迹id"},
    "speed": {"speed", "速度"},
    "distance": {"distance", "距离"},
}
_NORMALIZED_ALIASES = {
    semantic: {normalize_header(alias) for alias in aliases}
    for semantic, aliases in _ALIASES.items()
}


def resolve_fields(
    columns: Sequence[object], overrides: Mapping[str, str] | None = None
) -> FieldMapping:
    originals = [str(column) for column in columns]
    override_map = dict(overrides or {})
    unknown = sorted(set(override_map) - set(_ALIASES))
    if unknown:
        raise InputValidationError(f"field-map 包含未知语义字段: {unknown}")

    selected: dict[str, str | None] = {}
    for semantic, aliases in _NORMALIZED_ALIASES.items():
        if semantic in override_map:
            requested = override_map[semantic]
            if requested not in originals:
                raise InputValidationError(
                    f"field-map 指定的字段不存在: {semantic}={requested}; 已发现字段: {originals}"
                )
            selected[semantic] = requested
            continue
        matches = [column for column in originals if normalize_header(column) in aliases]
        if len(matches) > 1:
            raise InputValidationError(
                f"Ambiguous field mapping for {semantic}: {matches}; 请使用 --field-map 明确指定。"
            )
        selected[semantic] = matches[0] if matches else None

    missing: list[str] = []
    if selected["longitude"] is None:
        missing.append("longitude")
    if selected["latitude"] is None:
        missing.append("latitude")
    has_datetime = selected["datetime"] is not None
    has_date_time = selected["date"] is not None and selected["time"] is not None
    if not (has_datetime or has_date_time):
        missing.append("datetime (or date+time)")
    if missing:
        raise InputValidationError(
            f"出行链数据缺少必要参数: {missing}; 已发现字段: {originals}; "
            f"支持别名: {_ALIASES}"
        )

    return FieldMapping(
        longitude=str(selected["longitude"]),
        latitude=str(selected["latitude"]),
        datetime=selected["datetime"],
        date=selected["date"],
        time=selected["time"],
        track_id=selected["track_id"],
        speed=selected["speed"],
        distance=selected["distance"],
    )


def read_csv_robust(path: Path) -> tuple[pd.DataFrame, str]:
    errors: list[str] = []
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as stream:
                header = next(csv.reader(stream), [])
            duplicates = sorted(name for name, count in Counter(header).items() if count > 1)
            if duplicates:
                raise InputValidationError(
                    f"CSV 存在重复表头，无法无歧义映射字段: {duplicates}"
                )
            frame = pd.read_csv(path, encoding=encoding, dtype=str, keep_default_na=False)
            return frame, encoding
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise InputValidationError(f"CSV 编码无法识别（尝试 utf-8-sig、gb18030）: {errors}")


_SECOND_RE = re.compile(r"(?:[T\s]|^)\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s|$|Z|[+-])", re.I)
_MINUTE_RE = re.compile(r"(?:[T\s]|^)\d{1,2}:\d{2}(?:\s|$|Z|[+-])", re.I)


def _time_precision(values: pd.Series) -> str:
    texts = values.astype(str).str.strip()
    texts = texts[texts.ne("")]
    if texts.str.contains(_SECOND_RE, regex=True).any():
        return "second"
    if texts.str.contains(_MINUTE_RE, regex=True).any():
        return "minute"
    return "date"


def _parse_timestamp(value: object) -> tuple[pd.Timestamp, str | None]:
    try:
        timestamp = pd.Timestamp(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return pd.NaT, None
    if pd.isna(timestamp):
        return pd.NaT, None
    local_date = timestamp.strftime("%Y-%m-%d")
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp, local_date


def prepare_input(
    frame: pd.DataFrame, mapping: FieldMapping, source_name: str
) -> PreparedInput:
    work = frame.copy()
    work["_source_row"] = range(len(work))
    work["_longitude"] = pd.to_numeric(work[mapping.longitude], errors="coerce")
    work["_latitude"] = pd.to_numeric(work[mapping.latitude], errors="coerce")

    if mapping.datetime is not None:
        raw_timestamp = work[mapping.datetime].astype(str).str.strip()
        precision_source = raw_timestamp
    else:
        raw_date = work[str(mapping.date)].astype(str).str.strip()
        raw_time = work[str(mapping.time)].astype(str).str.strip()
        raw_timestamp = raw_date + " " + raw_time
        precision_source = raw_time

    precision_rows = precision_source.astype(str).str.strip().map(_time_precision_value)
    precision = "second" if precision_rows.eq("second").any() else (
        "minute" if precision_rows.eq("minute").any() else "date"
    )
    if precision == "date":
        raise InputValidationError(
            "时间精度为 [date]，只有日期或缺少具体时刻；出行链处理要求时间至少精确到分钟。"
        )

    parsed = [
        _parse_timestamp(value) if row_precision != "date" else (pd.NaT, None)
        for value, row_precision in zip(raw_timestamp, precision_rows)
    ]
    work["_timestamp"] = pd.to_datetime([value[0] for value in parsed], errors="coerce")
    work["_date"] = [value[1] if value[1] is not None else "<invalid-time>" for value in parsed]
    coordinate_invalid = (
        work["_longitude"].isna()
        | work["_latitude"].isna()
        | ~work["_longitude"].between(-180, 180)
        | ~work["_latitude"].between(-90, 90)
    )
    time_invalid = work["_timestamp"].isna() | precision_rows.eq("date")
    valid = ~(coordinate_invalid | time_invalid)

    if mapping.track_id is None:
        work["_track_key"] = source_name
    else:
        identifiers = work[mapping.track_id].astype(str).str.strip()
        work["_track_key"] = identifiers.where(identifiers.ne(""), source_name)
    cleaned = work.loc[valid].copy().reset_index(drop=True)
    rejected = {
        "invalid_coordinate": int(coordinate_invalid.sum()),
        "invalid_time": int(time_invalid.sum()),
    }
    if cleaned.empty:
        raise InputValidationError(
            f"剔除无效坐标或时间后没有可处理记录；拒绝统计: {rejected}"
        )

    group_counts: dict[GroupKey, tuple[int, int, int]] = {}
    for (track_id, date_text), rows in work.groupby(["_track_key", "_date"], sort=True, dropna=False):
        valid_count = int(valid.loc[rows.index].sum())
        total = int(len(rows))
        group_counts[GroupKey(str(track_id), str(date_text))] = (
            total,
            valid_count,
            total - valid_count,
        )

    return PreparedInput(
        frame=cleaned,
        mapping=mapping,
        source_name=source_name,
        time_precision=precision,
        source_rows=int(len(work)),
        valid_rows=int(len(cleaned)),
        rejected_rows=rejected,
        group_counts=group_counts,
    )


def _time_precision_value(value: object) -> str:
    text = str(value).strip()
    if _SECOND_RE.search(text):
        return "second"
    if _MINUTE_RE.search(text):
        return "minute"
    return "date"


def rejected_group_summaries(prepared: PreparedInput) -> list[GroupSummary]:
    summaries: list[GroupSummary] = []
    for key, (input_rows, valid_rows, rejected_rows) in sorted(prepared.group_counts.items()):
        if valid_rows:
            continue
        summaries.append(
            GroupSummary(
                track_id=key.track_id,
                date=key.date,
                input_rows=input_rows,
                representative_points=0,
                output_points=0,
                merged_rows=0,
                stop_points=0,
                start_time=None,
                end_time=None,
                time_anomalies=0,
                status="no_valid_rows",
                valid_rows=0,
                rejected_rows=rejected_rows,
                reason="该轨迹分组的坐标或时间记录全部无效",
            )
        )
    return summaries


def group_trajectories(
    prepared: PreparedInput,
) -> Iterable[tuple[GroupKey, pd.DataFrame]]:
    ordered = prepared.frame.sort_values(
        ["_track_key", "_date", "_timestamp", "_source_row"],
        kind="mergesort",
    )
    for (track_id, date_text), rows in ordered.groupby(
        ["_track_key", "_date"], sort=True, dropna=False
    ):
        yield GroupKey(str(track_id), str(date_text)), rows.reset_index(drop=True)


def _collapse_exact_runs(rows: pd.DataFrame) -> pd.DataFrame:
    ordered = rows.sort_values(["_timestamp", "_source_row"], kind="mergesort").reset_index(
        drop=True
    )
    new_run = (
        ordered["_longitude"].ne(ordered["_longitude"].shift())
        | ordered["_latitude"].ne(ordered["_latitude"].shift())
    )
    ordered["_run_id"] = new_run.cumsum()
    representatives: list[pd.Series] = []
    for _, run in ordered.groupby("_run_id", sort=False):
        representative = run.iloc[0].copy()
        start = pd.Timestamp(run.iloc[0]["_timestamp"])
        end = pd.Timestamp(run.iloc[-1]["_timestamp"])
        representative["_stop_start"] = start
        representative["_stop_end"] = end
        representative["_merged_count"] = int(len(run))
        representative["stop_time"] = float((end - start).total_seconds())
        representatives.append(representative)
    return pd.DataFrame(representatives).drop(columns=["_run_id"], errors="ignore").reset_index(
        drop=True
    )


def transform_group(
    key: GroupKey,
    rows: pd.DataFrame,
    distance_m: DistanceFunction,
    counts: tuple[int, int, int] | None = None,
) -> TrajectoryResult:
    collapsed = _collapse_exact_runs(rows)
    representative_count = int(len(collapsed))
    common = {
        "track_id": key.track_id,
        "date": key.date,
        "input_rows": counts[0] if counts else int(len(rows)),
        "representative_points": representative_count,
        "merged_rows": int(len(rows) - representative_count),
        "stop_points": int((collapsed["_merged_count"] > 1).sum()),
        "start_time": (
            pd.Timestamp(collapsed.iloc[0]["_stop_start"]) if representative_count else None
        ),
        "end_time": (
            pd.Timestamp(collapsed.iloc[-1]["_stop_end"]) if representative_count else None
        ),
        "valid_rows": counts[1] if counts else int(len(rows)),
        "rejected_rows": counts[2] if counts else 0,
    }
    if representative_count < 2:
        summary = GroupSummary(
            **common,
            output_points=0,
            time_anomalies=0,
            status="insufficient_points",
            reason="合并连续相同坐标后不足两个有效点",
        )
        return TrajectoryResult(key, collapsed.iloc[0:0].copy(), summary)

    distances: list[float | None] = []
    speeds: list[float | None] = []
    movement_seconds: list[float | None] = []
    time_anomalies = 0
    for index in range(representative_count - 1):
        current = collapsed.iloc[index]
        following = collapsed.iloc[index + 1]
        distance = float(
            distance_m(
                float(current["_longitude"]),
                float(current["_latitude"]),
                float(following["_longitude"]),
                float(following["_latitude"]),
            )
        )
        seconds = float(
            (
                pd.Timestamp(following["_stop_start"])
                - pd.Timestamp(current["_stop_end"])
            ).total_seconds()
        )
        distances.append(distance)
        movement_seconds.append(seconds)
        if seconds > 0:
            speeds.append(distance / seconds)
        else:
            speeds.append(None)
            time_anomalies += 1

    distances.append(None)
    speeds.append(None)
    movement_seconds.append(None)
    collapsed["real_distance"] = distances
    collapsed["move_seconds"] = movement_seconds
    collapsed["real_speed"] = speeds

    kinds = ["way"] * representative_count
    kinds[0] = "start"
    kinds[-1] = "end"
    for index in range(1, representative_count - 1):
        if int(collapsed.iloc[index]["_merged_count"]) > 1:
            kinds[index] = "stop"
    collapsed["kind"] = kinds

    summary = GroupSummary(
        **common,
        output_points=representative_count,
        time_anomalies=time_anomalies,
        status="success",
    )
    return TrajectoryResult(key, collapsed, summary)
