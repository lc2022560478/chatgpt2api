from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.storage.json_storage import JSONStorageBackend


class JSONStorageTests(unittest.TestCase):
    def test_save_accounts_replaces_file_with_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "accounts.json"
            storage = JSONStorageBackend(path)
            storage.save_accounts([{"access_token": "old"}])
            replacement_inputs: list[Path] = []
            real_replace = os.replace

            def checking_replace(source: str | Path, destination: str | Path) -> None:
                replacement_inputs.append(Path(source))
                self.assertEqual(
                    json.loads(Path(source).read_text(encoding="utf-8")),
                    [{"access_token": "new"}],
                )
                real_replace(source, destination)

            with mock.patch("services.storage.json_storage.os.replace", side_effect=checking_replace):
                storage.save_accounts([{"access_token": "new"}])

            self.assertEqual(storage.load_accounts(), [{"access_token": "new"}])
            self.assertEqual(len(replacement_inputs), 1)
            self.assertEqual(list(path.parent.glob(".accounts.json.*.tmp")), [])

    def test_failed_replace_preserves_existing_accounts_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "accounts.json"
            storage = JSONStorageBackend(path)
            storage.save_accounts([{"access_token": "old"}])

            with mock.patch("services.storage.json_storage.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    storage.save_accounts([{"access_token": "new"}])

            self.assertEqual(storage.load_accounts(), [{"access_token": "old"}])
            self.assertEqual(list(path.parent.glob(".accounts.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
