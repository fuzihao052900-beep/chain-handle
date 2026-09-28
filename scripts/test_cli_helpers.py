from __future__ import annotations

import importlib
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr
from pathlib import Path


def load_cli():
    try:
        return importlib.import_module("process_trip_chain")
    except ModuleNotFoundError as exc:
        if exc.name == "process_trip_chain":
            raise AssertionError("process_trip_chain implementation is missing") from exc
        raise


def load_gdb():
    try:
        return importlib.import_module("arcpy_gdb")
    except ModuleNotFoundError as exc:
        if exc.name == "arcpy_gdb":
            raise AssertionError("arcpy_gdb implementation is missing") from exc
        raise


class CliParserTests(unittest.TestCase):
    def test_parser_requires_input_and_output_and_does_not_overwrite_by_default(self):
        cli = load_cli()
        parser = cli.build_parser()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args([])
        options = parser.parse_args(["--input", "tracks.csv", "--output-dir", "results"])
        self.assertEqual(options.input, Path("tracks.csv"))
        self.assertEqual(options.output_dir, Path("results"))
        self.assertFalse(options.overwrite)

    def test_directory_enumeration_is_csv_only_and_deterministic(self):
        cli = load_cli()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.csv").write_text("x\n1\n", encoding="utf-8")
            (root / "A.CSV").write_text("x\n1\n", encoding="utf-8")
            (root / "ignored.txt").write_text("x", encoding="utf-8")
            paths = cli.enumerate_csvs(root)
        self.assertEqual([path.name for path in paths], ["A.CSV", "b.csv"])

    def test_output_name_is_sanitized_without_modifying_input(self):
        cli = load_cli()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a;b c.csv"
            source.write_text("x\n1\n", encoding="utf-8")
            output = cli.output_gdb_path(source, root / "results")
            self.assertEqual(output.name, "a_b_c_trip_chains.gdb")
            self.assertEqual(source.read_text(encoding="utf-8"), "x\n1\n")

    def test_help_does_not_import_arcpy(self):
        sys.modules.pop("arcpy", None)
        cli = load_cli()
        help_text = cli.build_parser().format_help()
        self.assertIn("--field-map", help_text)
        self.assertNotIn("arcpy", sys.modules)

    def test_batch_output_collisions_are_rejected_before_processing(self):
        cli = load_cli()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "a-b.csv", root / "a_b.csv"]
            for source in sources:
                source.write_text("x\n1\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                cli.preflight_batch_targets(sources, root / "out")
        self.assertIn("输出名冲突", str(ctx.exception))

    def test_supervisor_reports_native_worker_exit(self):
        cli = load_cli()
        completed = mock.Mock(returncode=-1073741819, stdout="", stderr="native crash")
        with mock.patch("subprocess.run", return_value=completed):
            report = cli.run_supervised(
                Path("sample.csv"), Path("out"), None, False
            )
        self.assertEqual(report["status"], "failed")
        self.assertIn("未提交有效回执", report["error"])

    def test_supervisor_preserves_success_report_when_arcpy_crashes_during_cleanup(self):
        cli = load_cli()
        payload = {
            "source_csv": "sample.csv",
            "output_gdb": "sample.gdb",
            "output_groups": 0,
            "status": "success",
            "failures": [],
        }
        def completed_with_receipt(command, **_kwargs):
            receipt = Path(command[command.index("--receipt-file") + 1])
            if any(str(part).endswith("verify_arcpy_output.py") for part in command):
                receipt.write_text(
                    json.dumps({"status": "verified", "feature_classes": 0}),
                    encoding="utf-8",
                )
                return mock.Mock(returncode=0, stdout="", stderr="")
            receipt.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return mock.Mock(returncode=3221227010, stdout="garbled", stderr="")

        with mock.patch("subprocess.run", side_effect=completed_with_receipt):
            report = cli.run_supervised(Path("sample.csv"), Path("out"), None, False)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["native_exit_code"], 3221227010)

    def test_supervisor_requires_independent_verification_receipt(self):
        cli = load_cli()

        def write_only(command, **_kwargs):
            receipt = Path(command[command.index("--receipt-file") + 1])
            if not any(str(part).endswith("verify_arcpy_output.py") for part in command):
                receipt.write_text(
                    json.dumps(
                        {
                            "status": "success",
                            "output_gdb": "sample.gdb",
                            "output_groups": 1,
                            "failures": [],
                        }
                    ),
                    encoding="utf-8",
                )
            return mock.Mock(returncode=0, stdout="", stderr="verification crash")

        with mock.patch("subprocess.run", side_effect=write_only):
            report = cli.run_supervised(Path("sample.csv"), Path("out"), None, False)
        self.assertEqual(report["status"], "failed")
        self.assertIn("独立验证", report["error"])

    def test_supervisor_does_not_trust_success_text_without_committed_receipt(self):
        cli = load_cli()
        payload = {"status": "success", "output_gdb": "sample.gdb"}
        completed = mock.Mock(returncode=3221227010, stdout=json.dumps(payload), stderr="")
        with mock.patch("subprocess.run", return_value=completed):
            report = cli.run_supervised(Path("sample.csv"), Path("out"), None, False)
        self.assertEqual(report["status"], "failed")
        self.assertIn("回执", report["error"])


class SafeOutputTests(unittest.TestCase):
    def test_long_text_ids_and_collisions_get_stable_names(self):
        gdb = load_gdb()
        used: set[str] = set()
        first = gdb.safe_feature_name("route/12345678901234567890", "2024-01-01", used)
        second = gdb.safe_feature_name("route:12345678901234567890", "2024-01-01", used)
        self.assertTrue(first.startswith("track_route_12345678901234567890_20240101"))
        self.assertNotEqual(first, second)
        used_again = {first}
        self.assertEqual(
            second,
            gdb.safe_feature_name("route:12345678901234567890", "2024-01-01", used_again),
        )

    def test_existing_target_requires_overwrite(self):
        gdb = load_gdb()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "tracks_trip_chains.gdb"
            target.mkdir()
            with self.assertRaises(gdb.OutputExistsError):
                gdb.validate_output_target(root, target, overwrite=False)
            self.assertEqual(gdb.validate_output_target(root, target, overwrite=True), target)

    def test_target_must_be_direct_child_of_output_directory(self):
        gdb = load_gdb()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            outside = root.parent / "outside.gdb"
            with self.assertRaises(gdb.OutputSafetyError):
                gdb.validate_output_target(root, outside, overwrite=True)

    def test_large_metadata_is_split_into_reversible_field_sized_chunks(self):
        gdb = load_gdb()
        payload = {"output_field_maps": {str(index): "字段" * 100 for index in range(100)}}
        chunks = gdb.metadata_chunks(payload, limit=4000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        self.assertEqual(json.loads("".join(chunks)), payload)

    def test_feature_names_are_case_insensitively_unique_after_validation(self):
        gdb = load_gdb()
        used: set[str] = set()
        validator = lambda value: value.lower()
        first = gdb.safe_feature_name("Route/A", "2024-01-01", used, validator)
        second = gdb.safe_feature_name("route:a", "2024-01-01", used, validator)
        self.assertNotEqual(first.casefold(), second.casefold())

    def test_source_values_come_from_immutable_input_row(self):
        gdb = load_gdb()
        import pandas as pd

        source = pd.DataFrame(
            {"kind": ["ORIGINAL"], "real_distance": ["SOURCE"], "_source_row": ["USER"]}
        )
        transformed = pd.Series(
            {"kind": "start", "real_distance": 42.0, "_source_row": 0}
        )
        self.assertEqual(
            gdb.original_values(source, transformed, list(source.columns)),
            ["ORIGINAL", "SOURCE", "USER"],
        )

    def test_source_text_fields_are_sized_from_actual_values(self):
        gdb = load_gdb()
        import pandas as pd

        value = "长" * 3000
        self.assertEqual(gdb._text_lengths(pd.DataFrame({"notes": [value]}), ["notes"]), {"notes": 3000})

    def test_mutation_boundary_never_deletes_without_overwrite(self):
        gdb = load_gdb()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "x.gdb"
            target.mkdir()
            arcpy = mock.Mock()
            with self.assertRaises(gdb.OutputExistsError):
                gdb.replace_target_at_mutation_boundary(arcpy, target, overwrite=False)
            arcpy.management.Delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
