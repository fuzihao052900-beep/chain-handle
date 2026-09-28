from __future__ import annotations

import hashlib
import json
import math
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pandas as pd

from chain_core import (
    FieldMapping,
    GroupSummary,
    InputValidationError,
    group_trajectories,
    prepare_input,
    read_csv_robust,
    rejected_group_summaries,
    resolve_fields,
    transform_group,
)


class ArcPyEnvironmentError(RuntimeError):
    pass


class GdbWriteError(RuntimeError):
    pass


class OutputExistsError(GdbWriteError):
    pass


class OutputSafetyError(GdbWriteError):
    pass


@dataclass(frozen=True)
class RunResult:
    source_csv: Path
    output_gdb: Path
    source_rows: int
    valid_rows: int
    rejected_rows: Mapping[str, int]
    candidate_groups: int
    output_groups: int
    skipped_groups: int
    time_precision: str
    failures: tuple[str, ...] = ()


def _safe_token(value: str) -> str:
    token = re.sub(r"[^\w]+", "_", str(value), flags=re.UNICODE).strip("_")
    return token or "unknown"


def safe_feature_name(
    track_id: str,
    date_text: str,
    used: set[str],
    validator=lambda value: value,
) -> str:
    date_token = re.sub(r"\D+", "", str(date_text)) or "undated"
    base = f"track_{_safe_token(track_id)}_{date_token}"[:120].rstrip("_")
    candidate = validator(base)
    used_folded = {item.casefold() for item in used}
    if candidate.casefold() in used_folded:
        digest = hashlib.sha1(f"{track_id}|{date_text}".encode("utf-8")).hexdigest()[:8]
        candidate = validator(f"{base[:111].rstrip('_')}_{digest}")
        counter = 2
        while candidate.casefold() in used_folded:
            suffix = f"_{digest}_{counter}"
            candidate = validator(f"{base[: 120 - len(suffix)].rstrip('_')}{suffix}")
            counter += 1
    used.add(candidate)
    return candidate


def validate_output_target(
    output_dir: Path, target: Path, overwrite: bool
) -> Path:
    root = output_dir.resolve()
    resolved = target.resolve()
    if resolved.parent != root or resolved.suffix.casefold() != ".gdb":
        raise OutputSafetyError(
            f"拒绝不安全的输出目标；GDB 必须直接位于输出目录下: {resolved}"
        )
    if resolved.exists() and not overwrite:
        raise OutputExistsError(f"输出已存在；如需替换请显式使用 --overwrite: {resolved}")
    return resolved


@contextmanager
def output_lock(target: Path):
    lock_path = target.with_name(f"{target.name}.chain_handle.lock")
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise OutputExistsError(f"输出目标正在由另一个进程处理: {target}") from exc
    try:
        os.write(descriptor, f"pid={os.getpid()}".encode("ascii"))
        os.close(descriptor)
        yield lock_path
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def replace_target_at_mutation_boundary(arcpy, target: Path, overwrite: bool) -> None:
    if not target.exists():
        return
    if not overwrite:
        raise OutputExistsError(f"输出已存在；未授权删除: {target}")
    arcpy.management.Delete(str(target))


def _load_arcpy():
    try:
        import arcpy
    except ImportError as exc:
        raise ArcPyEnvironmentError(
            "当前 Python 环境无法导入 ArcPy；请使用 ArcGIS Pro Python 运行此命令。"
        ) from exc
    return arcpy


def _distance_function(arcpy):
    spatial_reference = arcpy.SpatialReference(4326)

    def distance(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
        vertices = arcpy.Array([arcpy.Point(lon1, lat1), arcpy.Point(lon2, lat2)])
        geometry = arcpy.Polyline(vertices, spatial_reference)
        return float(geometry.getLength("GEODESIC", "METERS"))

    return distance


def geodesic_distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    return _distance_function(_load_arcpy())(lon1, lat1, lon2, lat2)


def _target_for_csv(input_csv: Path, output_dir: Path) -> Path:
    stem = _safe_token(input_csv.stem)
    return output_dir.resolve() / f"{stem}_trip_chains.gdb"


def _python_datetime(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime()


def _python_number(value):
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


_AUDIT_FIELDS = (
    "source_row",
    "norm_time",
    "stop_start",
    "stop_end",
    "merged_count",
    "move_seconds",
    "real_distance",
    "real_speed",
    "kind",
    "stop_time",
)


def _original_field_map(arcpy, feature_class: str, columns: list[str]) -> dict[str, str]:
    used = {field.name.casefold() for field in arcpy.ListFields(feature_class)}
    used.update(field.casefold() for field in _AUDIT_FIELDS)
    mapping: dict[str, str] = {}
    for index, original in enumerate(columns, start=1):
        base = arcpy.ValidateFieldName(str(original), str(Path(feature_class).parent))
        if not base:
            base = f"source_{index}"
        if base.casefold() in used:
            base = arcpy.ValidateFieldName(f"src_{base}", str(Path(feature_class).parent))
        if base.casefold() in used:
            digest = hashlib.sha1(str(original).encode("utf-8")).hexdigest()[:6]
            base = f"src_{index}_{digest}"
        counter = 2
        candidate = base
        while candidate.casefold() in used:
            suffix = f"_{counter}"
            candidate = f"{base[: 60 - len(suffix)]}{suffix}"
            counter += 1
        mapping[str(original)] = candidate
        used.add(candidate.casefold())
    return mapping


def _text_lengths(frame: pd.DataFrame, columns: list[str], limit: int = 1_000_000) -> dict[str, int]:
    lengths: dict[str, int] = {}
    for column in columns:
        maximum = max((len(str(value)) for value in frame[column] if not pd.isna(value)), default=1)
        if maximum > limit:
            raise GdbWriteError(
                f"源字段 {column!r} 的文本长度 {maximum} 超过文件地理数据库写入上限 {limit}"
            )
        lengths[column] = max(1, maximum)
    return lengths


def _add_point_fields(
    arcpy,
    feature_class: str,
    source_fields: Mapping[str, str],
    lengths: Mapping[str, int],
) -> None:
    for original, field_name in source_fields.items():
        arcpy.management.AddField(
            feature_class, field_name, "TEXT", field_length=int(lengths[original])
        )
    definitions = (
        ("source_row", "LONG", None),
        ("norm_time", "DATE", None),
        ("stop_start", "DATE", None),
        ("stop_end", "DATE", None),
        ("merged_count", "LONG", None),
        ("move_seconds", "DOUBLE", None),
        ("real_distance", "DOUBLE", None),
        ("real_speed", "DOUBLE", None),
        ("kind", "TEXT", 10),
        ("stop_time", "DOUBLE", None),
    )
    for name, field_type, length in definitions:
        options = {"field_length": length} if length else {}
        arcpy.management.AddField(feature_class, name, field_type, **options)


def _write_feature_class(
    arcpy,
    output_gdb: Path,
    name: str,
    frame: pd.DataFrame,
    source_frame: pd.DataFrame,
    original_columns: list[str],
) -> tuple[str, dict[str, str]]:
    feature_class = str(output_gdb / name)
    arcpy.management.CreateFeatureclass(
        str(output_gdb), name, "POINT", spatial_reference=arcpy.SpatialReference(4326)
    )
    source_fields = _original_field_map(arcpy, feature_class, original_columns)
    _add_point_fields(
        arcpy,
        feature_class,
        source_fields,
        _text_lengths(source_frame, original_columns),
    )
    cursor_fields = ["SHAPE@XY", *source_fields.values(), *_AUDIT_FIELDS]
    with arcpy.da.InsertCursor(feature_class, cursor_fields) as cursor:
        for _, row in frame.iterrows():
            source_values = original_values(source_frame, row, original_columns)
            cursor.insertRow(
                [
                    (float(row["_longitude"]), float(row["_latitude"])),
                    *source_values,
                    int(row["_source_row"]),
                    _python_datetime(row["_timestamp"]),
                    _python_datetime(row["_stop_start"]),
                    _python_datetime(row["_stop_end"]),
                    int(row["_merged_count"]),
                    _python_number(row["move_seconds"]),
                    _python_number(row["real_distance"]),
                    _python_number(row["real_speed"]),
                    str(row["kind"]),
                    float(row["stop_time"]),
                ]
            )
    arcpy.management.AddSpatialIndex(feature_class)
    return feature_class, source_fields


def original_values(
    source_frame: pd.DataFrame, transformed_row: pd.Series, columns: list[str]
) -> list[str | None]:
    source_row = source_frame.iloc[int(transformed_row["_source_row"])]
    return [
        None if source_row[column] is None or pd.isna(source_row[column]) else str(source_row[column])
        for column in columns
    ]


def _create_summary_table(arcpy, output_gdb: Path, rows: list[tuple[GroupSummary, str | None]]) -> str:
    table = str(output_gdb / "processing_summary")
    arcpy.management.CreateTable(str(output_gdb), "processing_summary")
    definitions = (
        (
            "track_id",
            "TEXT",
            min(1_000_000, max((len(item.track_id) for item, _ in rows), default=1)),
        ),
        ("chain_date", "TEXT", 10),
        ("input_rows", "LONG", None),
        ("valid_rows", "LONG", None),
        ("reject_rows", "LONG", None),
        ("repr_points", "LONG", None),
        ("output_pts", "LONG", None),
        ("merged_rows", "LONG", None),
        ("stop_points", "LONG", None),
        ("start_time", "DATE", None),
        ("end_time", "DATE", None),
        ("time_anom", "LONG", None),
        ("status", "TEXT", 32),
        ("reason", "TEXT", 512),
        ("feature_cls", "TEXT", 160),
    )
    for name, field_type, length in definitions:
        options = {"field_length": length} if length else {}
        arcpy.management.AddField(table, name, field_type, **options)
    fields = [definition[0] for definition in definitions]
    with arcpy.da.InsertCursor(table, fields) as cursor:
        for summary, feature_name in rows:
            cursor.insertRow(
                [
                    summary.track_id,
                    summary.date,
                    summary.input_rows,
                    summary.valid_rows,
                    summary.rejected_rows,
                    summary.representative_points,
                    summary.output_points,
                    summary.merged_rows,
                    summary.stop_points,
                    _python_datetime(summary.start_time),
                    _python_datetime(summary.end_time),
                    summary.time_anomalies,
                    summary.status,
                    summary.reason,
                    feature_name,
                ]
            )
    return table


def _create_metadata_table(arcpy, output_gdb: Path, metadata: Mapping[str, object]) -> str:
    table = str(output_gdb / "run_metadata")
    arcpy.management.CreateTable(str(output_gdb), "run_metadata")
    arcpy.management.AddField(table, "meta_key", "TEXT", field_length=64)
    arcpy.management.AddField(table, "meta_part", "LONG")
    arcpy.management.AddField(table, "meta_value", "TEXT", field_length=4000)
    with arcpy.da.InsertCursor(table, ["meta_key", "meta_part", "meta_value"]) as cursor:
        for key in sorted(metadata):
            value = metadata[key]
            for part, text in enumerate(metadata_chunks(value), start=1):
                cursor.insertRow([str(key), part, text])
    return table


def metadata_chunks(value: object, limit: int = 4000) -> list[str]:
    if limit <= 0:
        raise ValueError("metadata chunk limit must be positive")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return [text[index : index + limit] for index in range(0, len(text), limit)] or [""]


def verify_gdb_path(output_gdb: Path, expected_groups: int) -> dict[str, object]:
    arcpy = _load_arcpy()
    output = str(output_gdb.resolve())
    if not arcpy.Exists(output):
        raise GdbWriteError(f"写入后验证失败，GDB 不存在: {output}")
    previous = arcpy.env.workspace
    try:
        arcpy.env.workspace = output
        feature_classes = list(arcpy.ListFeatureClasses() or [])
        tables = set(arcpy.ListTables() or [])
        if len(feature_classes) != expected_groups:
            raise GdbWriteError(
                f"写入后验证失败，轨迹要素类数量 {len(feature_classes)} != {expected_groups}"
            )
        required_tables = {"processing_summary", "run_metadata"}
        missing = sorted(required_tables - tables)
        if missing:
            raise GdbWriteError(f"写入后验证失败，缺少审计表: {missing}")
        return {
            "gdb_exists": True,
            "feature_classes": len(feature_classes),
            "required_tables": sorted(required_tables),
        }
    finally:
        arcpy.env.workspace = previous
        arcpy.management.ClearWorkspaceCache()


def verify_gdb_contract(result: RunResult) -> dict[str, object]:
    return verify_gdb_path(result.output_gdb, result.output_groups)


def write_input_gdb(
    input_csv: Path,
    output_dir: Path,
    field_overrides: Mapping[str, str] | None,
    overwrite: bool,
) -> RunResult:
    source = input_csv.resolve()
    destination_dir = output_dir.resolve()
    target = validate_output_target(
        destination_dir, _target_for_csv(source, destination_dir), overwrite
    )

    frame, encoding = read_csv_robust(source)
    mapping: FieldMapping = resolve_fields(frame.columns, field_overrides)
    prepared = prepare_input(frame, mapping, source.name)
    arcpy = _load_arcpy()
    distance = _distance_function(arcpy)
    transformed = [
        transform_group(key, rows, distance, prepared.group_counts.get(key))
        for key, rows in group_trajectories(prepared)
    ]
    rejected_summaries = rejected_group_summaries(prepared)

    destination_dir.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output_lock(target):
            replace_target_at_mutation_boundary(arcpy, target, overwrite)
            arcpy.management.CreateFileGDB(str(destination_dir), target.name)
            created = True
            used_names: set[str] = set()
            summary_rows: list[tuple[GroupSummary, str | None]] = [
                (summary, None) for summary in rejected_summaries
            ]
            field_maps: dict[str, dict[str, str]] = {}
            failures: list[str] = []
            original_columns = [str(column) for column in frame.columns]
            validator = lambda value: arcpy.ValidateTableName(value, str(target))
            for result in transformed:
                if result.summary.status != "success":
                    summary_rows.append((result.summary, None))
                    continue
                feature_name = safe_feature_name(
                    result.key.track_id, result.key.date, used_names, validator
                )
                feature_path = str(target / feature_name)
                try:
                    _, source_fields = _write_feature_class(
                        arcpy,
                        target,
                        feature_name,
                        result.frame,
                        frame,
                        original_columns,
                    )
                except Exception as exc:
                    try:
                        if arcpy.Exists(feature_path):
                            arcpy.management.Delete(feature_path)
                    except Exception:
                        pass
                    message = f"{result.key.track_id}/{result.key.date}: {exc}"
                    failures.append(message)
                    summary_rows.append(
                        (
                            replace(
                                result.summary,
                                output_points=0,
                                status="write_failed",
                                reason=str(exc)[:512],
                            ),
                            None,
                        )
                    )
                    continue
                field_maps[feature_name] = source_fields
                summary_rows.append((result.summary, feature_name))

            _create_summary_table(arcpy, target, summary_rows)
            output_count = sum(summary.status == "success" for summary, _ in summary_rows)
            skipped_count = len(summary_rows) - output_count
            metadata = {
                "script_version": "1.1.0",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "source_csv": str(source),
                "encoding": encoding,
                "time_precision": prepared.time_precision,
                "spatial_reference": "WGS 84 (EPSG:4326)",
                "source_rows": prepared.source_rows,
                "valid_rows": prepared.valid_rows,
                "rejected_rows": dict(prepared.rejected_rows),
                "field_mapping": asdict(mapping),
                "output_field_maps": field_maps,
                "candidate_groups": len(summary_rows),
                "output_groups": output_count,
                "skipped_groups": skipped_count,
                "trajectory_failures": failures,
                "stop_rule": "exact_consecutive_coordinate_equality",
                "speed_formula": "real_distance / (next_stop_start - current_stop_end)",
            }
            _create_metadata_table(arcpy, target, metadata)
            return RunResult(
                source_csv=source,
                output_gdb=target,
                source_rows=prepared.source_rows,
                valid_rows=prepared.valid_rows,
                rejected_rows=dict(prepared.rejected_rows),
                candidate_groups=len(summary_rows),
                output_groups=output_count,
                skipped_groups=skipped_count,
                time_precision=prepared.time_precision,
                failures=tuple(failures),
            )
    except (InputValidationError, OutputExistsError, OutputSafetyError):
        raise
    except Exception as exc:
        if created:
            try:
                arcpy.management.ClearWorkspaceCache()
                arcpy.management.Delete(str(target))
            except Exception:
                pass
        raise GdbWriteError(f"文件地理数据库写入失败: {target}: {exc}") from exc
