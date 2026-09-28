from __future__ import annotations

import importlib
import unittest


class ArcPyDiagnosticTests(unittest.TestCase):
    def test_classifies_committed_receipt_despite_cleanup_crash(self):
        diagnostics = importlib.import_module("diagnose_arcpy")
        self.assertEqual(
            diagnostics.classify_probe(3221227010, {"import_ok": True}),
            "healthy_with_cleanup_crash",
        )

    def test_does_not_call_missing_receipt_healthy(self):
        diagnostics = importlib.import_module("diagnose_arcpy")
        self.assertEqual(diagnostics.classify_probe(0, None), "unavailable")
