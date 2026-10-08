"""Package imports must not launch production acceptance jobs."""

import importlib
import unittest
from unittest.mock import patch


class PackageTests(unittest.TestCase):
    def test_audit_imports_do_not_open_databases_or_run_audits(self):
        with patch("sqlite3.connect", side_effect=AssertionError("audit started during import")):
            for name in ("code", "metadata", "projection", "children", "restore"):
                with self.subTest(audit=name):
                    module = importlib.import_module("github_archive.audits." + name)
                    self.assertTrue(callable(module.main))
