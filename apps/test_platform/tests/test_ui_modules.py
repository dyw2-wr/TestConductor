from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from apps.test_platform.ui_modules import UiModuleCatalog, UiModuleCatalogError

from .ui_asset_fixture import build_asset_database


class UiModuleTests(unittest.TestCase):
    def test_reads_site_scoped_asset_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "account.sqlite"
            expected = build_asset_database(path)

            catalog = UiModuleCatalog.from_asset_database(path)

            self.assertEqual(catalog.library_id, expected["library_id"])
            self.assertEqual(catalog.library_hash, expected["library_hash"])
            self.assertEqual(catalog.site, expected["site"])
            self.assertEqual(len(catalog.modules), 1)
            module = catalog.modules[0]
            self.assertEqual(module.ref, "account.login@v1")
            self.assertEqual(module.input_parameters[0]["name"], "account")
            self.assertNotIn("segments", module.__dict__)
            self.assertEqual(
                catalog.payload("account.login", 1)["segments"][0]["segment_id"],
                "login",
            )

    def test_rejects_tampered_procedure_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "account.sqlite"
            build_asset_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE procedures SET description='tampered'"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(UiModuleCatalogError, "描述"):
                UiModuleCatalog.from_asset_database(path)

    def test_rejects_library_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "account.sqlite"
            build_asset_database(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE asset_library SET library_hash=?",
                    ("0" * 64,),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(UiModuleCatalogError, "摘要"):
                UiModuleCatalog.from_asset_database(path)


if __name__ == "__main__":
    unittest.main()
