from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ArcPyGdbIntegrationTests(unittest.TestCase):
    def test_writes_and_inspects_synthetic_gdb(self):
        adapter = importlib.import_module("arcpy_gdb")
        writer = getattr(adapter, "write_input_gdb", None)
        self.assertTrue(callable(writer), "write_input_gdb implementation is missing")

        try:
            import arcpy
        except ImportError:
            self.skipTest("ArcPy is unavailable in this Python environment")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "smoke.csv"
            long_note = "长" * 3000
            source.write_text(
                "trackid,longitude,latitude,datetime,Speed,Distance,kind,real_distance,stop_time,_source_row,notes\n"
                f"A,123.0,41.0,2024-01-01 10:00:00,99,999,ORIGINAL_KIND,SOURCE_DISTANCE,SOURCE_STOP,USER_ROW,{long_note}\n"
                "A,123.1,41.1,2024-01-01 10:01:00,99,999,K2,D2,S2,R2,n2\n"
                "A,123.1,41.1,2024-01-01 10:01:30,99,999,K3,D3,S3,R3,n3\n"
                "A,123.2,41.2,2024-01-01 10:02:00,99,999,K4,D4,S4,R4,n4\n"
                "B,123.3,41.3,2024-01-01 11:00:00,99,999,K5,D5,S5,R5,n5\n"
                "B,123.4,41.4,2024-01-01 11:01:00,99,999,K6,D6,S6,R6,n6\n",
                encoding="utf-8",
            )
            result = writer(source, root / "output", None, False)
            self.assertEqual(result.output_groups, 2)
            self.assertEqual(result.skipped_groups, 0)
            self.assertTrue(result.output_gdb.exists())

            previous = arcpy.env.workspace
            try:
                arcpy.env.workspace = str(result.output_gdb)
                feature_classes = sorted(arcpy.ListFeatureClasses())
                self.assertEqual(len(feature_classes), 2)
                for name in feature_classes:
                    feature_class = str(result.output_gdb / name)
                    self.assertEqual(arcpy.Describe(feature_class).spatialReference.factoryCode, 4326)
                    fields = {field.name: field.type for field in arcpy.ListFields(feature_class)}
                    self.assertEqual(fields["real_distance"], "Double")
                    self.assertEqual(fields["real_speed"], "Double")
                    self.assertEqual(fields["kind"], "String")
                    self.assertEqual(fields["stop_time"], "Double")
                    kinds = [row[0] for row in arcpy.da.SearchCursor(feature_class, ["kind"])]
                    self.assertEqual(kinds.count("start"), 1)
                    self.assertEqual(kinds.count("end"), 1)
                    source_kind = [
                        row[0] for row in arcpy.da.SearchCursor(feature_class, ["src_kind"])
                    ]
                    self.assertNotIn("start", source_kind)
                    notes_field = next(field for field in arcpy.ListFields(feature_class) if field.name == "notes")
                    self.assertGreaterEqual(notes_field.length, 3000)
                self.assertIn("processing_summary", arcpy.ListTables())
                self.assertIn("run_metadata", arcpy.ListTables())
            finally:
                arcpy.env.workspace = previous
                arcpy.management.ClearWorkspaceCache()

    def test_one_feature_write_failure_does_not_remove_other_groups(self):
        adapter = importlib.import_module("arcpy_gdb")
        try:
            import arcpy
        except ImportError:
            self.skipTest("ArcPy is unavailable in this Python environment")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "partial.csv"
            source.write_text(
                "trackid,longitude,latitude,datetime\n"
                "A,123.0,41.0,2024-01-01 10:00:00\n"
                "A,123.1,41.1,2024-01-01 10:01:00\n"
                "B,123.2,41.2,2024-01-01 11:00:00\n"
                "B,123.3,41.3,2024-01-01 11:01:00\n",
                encoding="utf-8",
            )
            original = adapter._write_feature_class

            def fail_a(arcpy_module, output_gdb, name, *args):
                if "track_A_" in name:
                    raise RuntimeError("synthetic group failure")
                return original(arcpy_module, output_gdb, name, *args)

            with mock.patch.object(adapter, "_write_feature_class", side_effect=fail_a):
                result = adapter.write_input_gdb(source, root / "output", None, False)
            self.assertEqual(result.output_groups, 1)
            self.assertEqual(len(result.failures), 1)
            previous = arcpy.env.workspace
            try:
                arcpy.env.workspace = str(result.output_gdb)
                self.assertEqual(len(arcpy.ListFeatureClasses()), 1)
            finally:
                arcpy.env.workspace = previous
                arcpy.management.ClearWorkspaceCache()


if __name__ == "__main__":
    unittest.main()
