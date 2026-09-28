from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path

import pandas as pd


def load_core():
    try:
        return importlib.import_module("chain_core")
    except ModuleNotFoundError as exc:
        if exc.name == "chain_core":
            raise AssertionError("chain_core implementation is missing") from exc
        raise


class FieldResolutionTests(unittest.TestCase):
    def test_resolves_english_aliases(self):
        core = load_core()
        mapping = core.resolve_fields(
            ["Track_ID", "Longitude", "Latitude", "Date_Time", "Speed", "Distance"]
        )
        self.assertEqual(mapping.track_id, "Track_ID")
        self.assertEqual(mapping.longitude, "Longitude")
        self.assertEqual(mapping.latitude, "Latitude")
        self.assertEqual(mapping.datetime, "Date_Time")
        self.assertEqual(mapping.speed, "Speed")
        self.assertEqual(mapping.distance, "Distance")

    def test_resolves_chinese_aliases(self):
        core = load_core()
        mapping = core.resolve_fields(["轨迹编号", "经度", "纬度", "日期时间", "速度", "距离"])
        self.assertEqual(mapping.track_id, "轨迹编号")
        self.assertEqual(mapping.longitude, "经度")
        self.assertEqual(mapping.latitude, "纬度")
        self.assertEqual(mapping.datetime, "日期时间")

    def test_resolves_separate_date_and_time(self):
        core = load_core()
        mapping = core.resolve_fields(["lon", "lat", "date", "time"])
        self.assertIsNone(mapping.datetime)
        self.assertEqual(mapping.date, "date")
        self.assertEqual(mapping.time, "time")

    def test_missing_required_fields_lists_discovered_and_missing(self):
        core = load_core()
        with self.assertRaises(core.InputValidationError) as ctx:
            core.resolve_fields(["latitude", "name"])
        message = str(ctx.exception)
        self.assertIn("longitude", message)
        self.assertIn("datetime", message)
        self.assertIn("latitude", message)
        self.assertIn("name", message)

    def test_normalizes_bom_spaces_case_and_punctuation(self):
        core = load_core()
        mapping = core.resolve_fields(["\ufeff Long-itude ", " LAT_ITUDE ", "DATE_TIME"])
        self.assertEqual(mapping.longitude, "\ufeff Long-itude ")
        self.assertEqual(mapping.latitude, " LAT_ITUDE ")
        self.assertEqual(mapping.datetime, "DATE_TIME")

    def test_ambiguous_aliases_require_override(self):
        core = load_core()
        columns = ["longitude", "lon", "latitude", "datetime"]
        with self.assertRaises(core.InputValidationError) as ctx:
            core.resolve_fields(columns)
        self.assertIn("ambiguous", str(ctx.exception).lower())
        mapping = core.resolve_fields(columns, {"longitude": "lon"})
        self.assertEqual(mapping.longitude, "lon")


class CsvLoadingTests(unittest.TestCase):
    def test_reads_utf8_bom_and_gb18030(self):
        core = load_core()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            utf8 = root / "utf8.csv"
            gb = root / "gb.csv"
            text = "经度,纬度,日期时间\n123.1,41.1,2024-01-01 10:00:00\n"
            utf8.write_text(text, encoding="utf-8-sig")
            gb.write_text(text, encoding="gb18030")
            utf_frame, utf_encoding = core.read_csv_robust(utf8)
            gb_frame, gb_encoding = core.read_csv_robust(gb)
        self.assertEqual(list(utf_frame.columns), ["经度", "纬度", "日期时间"])
        self.assertEqual(list(gb_frame.columns), ["经度", "纬度", "日期时间"])
        self.assertEqual(utf_encoding, "utf-8-sig")
        self.assertEqual(gb_encoding, "gb18030")

    def test_preserves_long_and_text_track_ids_as_strings(self):
        core = load_core()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ids.csv"
            path.write_text(
                "trackid,longitude,latitude,datetime\n"
                "12345678901234567890,123.1,41.1,2024-01-01 10:00:00\n"
                "route-A,123.2,41.2,2024-01-01 10:01:00\n",
                encoding="utf-8",
            )
            frame, _ = core.read_csv_robust(path)
        self.assertEqual(frame["trackid"].tolist(), ["12345678901234567890", "route-A"])

    def test_rejects_exact_duplicate_csv_headers_before_pandas_mangles_them(self):
        core = load_core()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.csv"
            path.write_text(
                "longitude,longitude,latitude,datetime\n"
                "123.1,9.9,41.1,2024-01-01 10:00:00\n",
                encoding="utf-8",
            )
            with self.assertRaises(core.InputValidationError) as ctx:
                core.read_csv_robust(path)
        self.assertIn("重复表头", str(ctx.exception))


class InputPreparationTests(unittest.TestCase):
    def test_rejects_date_only_precision(self):
        core = load_core()
        frame = pd.DataFrame(
            {"longitude": [123.1], "latitude": [41.1], "datetime": ["2024-01-01"]}
        )
        mapping = core.resolve_fields(frame.columns)
        with self.assertRaises(core.InputValidationError) as ctx:
            core.prepare_input(frame, mapping, "sample.csv")
        self.assertIn("date", str(ctx.exception).lower())

    def test_reports_minute_and_second_precision(self):
        core = load_core()
        minute_frame = pd.DataFrame(
            {"longitude": [123.1], "latitude": [41.1], "datetime": ["2024-01-01 10:30"]}
        )
        second_frame = pd.DataFrame(
            {"longitude": [123.1], "latitude": [41.1], "datetime": ["2024-01-01 10:30:00"]}
        )
        mapping = core.resolve_fields(minute_frame.columns)
        self.assertEqual(core.prepare_input(minute_frame, mapping, "a.csv").time_precision, "minute")
        self.assertEqual(core.prepare_input(second_frame, mapping, "a.csv").time_precision, "second")

    def test_combines_separate_date_and_time(self):
        core = load_core()
        frame = pd.DataFrame(
            {"lon": [123.1], "lat": [41.1], "date": ["2024-01-01"], "time": ["10:30:15"]}
        )
        prepared = core.prepare_input(frame, core.resolve_fields(frame.columns), "sample.csv")
        self.assertEqual(prepared.time_precision, "second")
        self.assertEqual(str(prepared.frame.loc[0, "_timestamp"]), "2024-01-01 10:30:15")

    def test_counts_and_removes_invalid_rows(self):
        core = load_core()
        frame = pd.DataFrame(
            {
                "trackid": ["A", "A", "A", "A", "A"],
                "longitude": [123.1, 181, 123.1, 123.1, None],
                "latitude": [41.1, 41.1, 91, 41.1, 41.1],
                "datetime": [
                    "2024-01-01 10:00:00",
                    "2024-01-01 10:01:00",
                    "2024-01-01 10:02:00",
                    "bad-time",
                    "2024-01-01 10:04:00",
                ],
            }
        )
        prepared = core.prepare_input(frame, core.resolve_fields(frame.columns), "sample.csv")
        self.assertEqual(prepared.source_rows, 5)
        self.assertEqual(prepared.valid_rows, 1)
        self.assertEqual(prepared.rejected_rows["invalid_coordinate"], 3)
        self.assertEqual(prepared.rejected_rows["invalid_time"], 1)

    def test_preserves_track_id_as_text_in_prepared_rows(self):
        core = load_core()
        frame = pd.DataFrame(
            {
                "trackid": [12345678901234567890, "route-A"],
                "longitude": [123.1, 123.2],
                "latitude": [41.1, 41.2],
                "datetime": ["2024-01-01 10:00:00", "2024-01-01 10:01:00"],
            },
            dtype=object,
        )
        prepared = core.prepare_input(frame, core.resolve_fields(frame.columns), "sample.csv")
        self.assertEqual(prepared.frame["_track_key"].tolist(), ["12345678901234567890", "route-A"])

    def test_date_only_rows_are_rejected_even_when_other_rows_have_seconds(self):
        core = load_core()
        frame = pd.DataFrame(
            {
                "longitude": [123.1, 123.2],
                "latitude": [41.1, 41.2],
                "datetime": ["2024-01-01", "2024-01-01 10:00:00"],
            }
        )
        prepared = core.prepare_input(frame, core.resolve_fields(frame.columns), "sample.csv")
        self.assertEqual(prepared.valid_rows, 1)
        self.assertEqual(prepared.rejected_rows["invalid_time"], 1)
        self.assertEqual(prepared.frame["_timestamp"].dt.hour.tolist(), [10])

    def test_iso_z_and_changing_offsets_sort_by_instant_but_keep_local_dates(self):
        core = load_core()
        frame = pd.DataFrame(
            {
                "trackid": ["A", "A", "A"],
                "longitude": [1, 2, 3],
                "latitude": [1, 2, 3],
                "datetime": [
                    "2024-01-02T00:10:00+08:00",
                    "2024-01-01T16:05:00Z",
                    "2024-01-01T10:00:00.500-05:00",
                ],
            }
        )
        prepared = core.prepare_input(frame, core.resolve_fields(frame.columns), "sample.csv")
        self.assertEqual(prepared.valid_rows, 3)
        self.assertEqual(prepared.frame["_date"].tolist(), ["2024-01-02", "2024-01-01", "2024-01-01"])
        groups = list(core.group_trajectories(prepared))
        jan1 = next(rows for key, rows in groups if key.date == "2024-01-01")
        self.assertEqual(jan1["_longitude"].tolist(), [3.0, 2.0])

    def test_candidate_group_counts_include_rejected_and_fully_rejected_groups(self):
        core = load_core()
        frame = pd.DataFrame(
            {
                "trackid": ["A", "A", "B"],
                "longitude": [123.1, 999, 999],
                "latitude": [41.1, 41.1, 41.1],
                "datetime": [
                    "2024-01-01 10:00:00",
                    "2024-01-01 10:01:00",
                    "2024-01-01 11:00:00",
                ],
            }
        )
        prepared = core.prepare_input(frame, core.resolve_fields(frame.columns), "sample.csv")
        self.assertEqual(prepared.group_counts[core.GroupKey("A", "2024-01-01")], (2, 1, 1))
        rejected = core.rejected_group_summaries(prepared)
        self.assertEqual([(item.track_id, item.input_rows, item.valid_rows) for item in rejected], [("B", 1, 0)])


class TrajectoryTransformationTests(unittest.TestCase):
    def prepare(self, rows, include_track=True):
        core = load_core()
        columns = ["longitude", "latitude", "datetime"]
        if include_track:
            columns.insert(0, "trackid")
        frame = pd.DataFrame(rows, columns=columns)
        return core, core.prepare_input(frame, core.resolve_fields(frame.columns), "sample.csv")

    def test_groups_by_track_and_date(self):
        core, prepared = self.prepare(
            [
                ["A", 1, 1, "2024-01-01 10:00:00"],
                ["A", 2, 2, "2024-01-02 10:00:00"],
                ["B", 3, 3, "2024-01-01 10:00:00"],
            ]
        )
        keys = [key for key, _ in core.group_trajectories(prepared)]
        self.assertEqual(
            [(key.track_id, key.date) for key in keys],
            [("A", "2024-01-01"), ("A", "2024-01-02"), ("B", "2024-01-01")],
        )

    def test_missing_track_id_groups_by_source_and_date(self):
        core, prepared = self.prepare(
            [[1, 1, "2024-01-01 10:00:00"], [2, 2, "2024-01-01 10:01:00"]],
            include_track=False,
        )
        groups = list(core.group_trajectories(prepared))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0].track_id, "sample.csv")

    def test_time_ties_keep_source_order(self):
        core, prepared = self.prepare(
            [
                ["A", 3, 3, "2024-01-01 10:00:00"],
                ["A", 1, 1, "2024-01-01 10:00:00"],
                ["A", 2, 2, "2024-01-01 10:00:00"],
            ]
        )
        _, rows = next(iter(core.group_trajectories(prepared)))
        self.assertEqual(rows["_longitude"].tolist(), [3.0, 1.0, 2.0])

    def test_only_consecutive_exact_coordinates_collapse(self):
        core, prepared = self.prepare(
            [
                ["A", 1, 1, "2024-01-01 10:00:00"],
                ["A", 2, 2, "2024-01-01 10:01:00"],
                ["A", 1, 1, "2024-01-01 10:02:00"],
            ]
        )
        key, rows = next(iter(core.group_trajectories(prepared)))
        result = core.transform_group(key, rows, lambda *_: 1.0)
        self.assertEqual(result.frame["_longitude"].tolist(), [1.0, 2.0, 1.0])
        self.assertEqual(result.frame["_merged_count"].tolist(), [1, 1, 1])

    def test_collapsed_run_accumulates_stop_time(self):
        core, prepared = self.prepare(
            [
                ["A", 1, 1, "2024-01-01 10:00:00"],
                ["A", 1, 1, "2024-01-01 10:00:10"],
                ["A", 1, 1, "2024-01-01 10:00:25"],
                ["A", 2, 2, "2024-01-01 10:01:00"],
            ]
        )
        key, rows = next(iter(core.group_trajectories(prepared)))
        result = core.transform_group(key, rows, lambda *_: 5.0)
        self.assertEqual(result.frame.loc[0, "_merged_count"], 3)
        self.assertEqual(result.frame.loc[0, "stop_time"], 25.0)

    def test_speed_uses_current_stop_end_to_next_start(self):
        core, prepared = self.prepare(
            [
                ["A", 1, 1, "2024-01-01 10:00:00"],
                ["A", 1, 1, "2024-01-01 10:00:10"],
                ["A", 2, 2, "2024-01-01 10:00:20"],
            ]
        )
        key, rows = next(iter(core.group_trajectories(prepared)))
        result = core.transform_group(key, rows, lambda *_: 100.0)
        self.assertEqual(result.frame.loc[0, "real_distance"], 100.0)
        self.assertEqual(result.frame.loc[0, "move_seconds"], 10.0)
        self.assertEqual(result.frame.loc[0, "real_speed"], 10.0)

    def test_nonpositive_time_keeps_distance_and_nulls_speed(self):
        core, prepared = self.prepare(
            [
                ["A", 1, 1, "2024-01-01 10:00:00"],
                ["A", 2, 2, "2024-01-01 10:00:00"],
            ]
        )
        key, rows = next(iter(core.group_trajectories(prepared)))
        result = core.transform_group(key, rows, lambda *_: 50.0)
        self.assertEqual(result.frame.loc[0, "real_distance"], 50.0)
        self.assertTrue(pd.isna(result.frame.loc[0, "real_speed"]))
        self.assertEqual(result.summary.time_anomalies, 1)

    def test_last_point_has_null_distance_and_speed(self):
        core, prepared = self.prepare(
            [
                ["A", 1, 1, "2024-01-01 10:00:00"],
                ["A", 2, 2, "2024-01-01 10:01:00"],
            ]
        )
        key, rows = next(iter(core.group_trajectories(prepared)))
        result = core.transform_group(key, rows, lambda *_: 10.0)
        self.assertTrue(pd.isna(result.frame.iloc[-1]["real_distance"]))
        self.assertTrue(pd.isna(result.frame.iloc[-1]["real_speed"]))

    def test_kind_precedence_marks_start_way_stop_end(self):
        core, prepared = self.prepare(
            [
                ["A", 0, 0, "2024-01-01 10:00:00"],
                ["A", 0, 0, "2024-01-01 10:00:05"],
                ["A", 1, 1, "2024-01-01 10:00:10"],
                ["A", 2, 2, "2024-01-01 10:00:20"],
                ["A", 2, 2, "2024-01-01 10:00:25"],
                ["A", 3, 3, "2024-01-01 10:00:30"],
                ["A", 3, 3, "2024-01-01 10:00:35"],
            ]
        )
        key, rows = next(iter(core.group_trajectories(prepared)))
        result = core.transform_group(key, rows, lambda *_: 1.0)
        self.assertEqual(result.frame["kind"].tolist(), ["start", "way", "stop", "end"])
        self.assertEqual(result.frame["stop_time"].tolist(), [5.0, 0.0, 5.0, 5.0])

    def test_single_representative_point_is_skipped(self):
        core, prepared = self.prepare(
            [
                ["A", 1, 1, "2024-01-01 10:00:00"],
                ["A", 1, 1, "2024-01-01 10:01:00"],
            ]
        )
        key, rows = next(iter(core.group_trajectories(prepared)))
        result = core.transform_group(key, rows, lambda *_: 0.0)
        self.assertEqual(result.summary.status, "insufficient_points")
        self.assertTrue(result.frame.empty)


if __name__ == "__main__":
    unittest.main()
