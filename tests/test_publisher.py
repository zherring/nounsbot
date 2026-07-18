import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import db, publisher


class ConstitutionVersionTests(unittest.TestCase):
    def test_version_parsed_from_constitution_heading(self):
        version = publisher.constitution_version()
        self.assertIsNotNone(version)
        self.assertRegex(version, r"^v\d+\.\d+$")

    def test_register_maps_current_rev_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            amendments = Path(tmp) / "amendments.json"
            amendments.write_text(json.dumps({"revs": {"0000000": "v0.1"}, "amendments": []}))
            with (
                patch.object(publisher, "AMENDMENTS_PATH", amendments),
                patch.object(publisher, "constitution_version", return_value="v9.9"),
                patch.object(db, "constitution_rev", return_value="abc1234"),
            ):
                self.assertTrue(publisher.register_constitution_rev())
                data = json.loads(amendments.read_text())
                self.assertEqual(data["revs"]["abc1234"], "v9.9")
                self.assertEqual(data["revs"]["0000000"], "v0.1")
                self.assertFalse(publisher.register_constitution_rev())

    def test_register_never_writes_without_version_or_rev(self):
        with tempfile.TemporaryDirectory() as tmp:
            amendments = Path(tmp) / "amendments.json"
            amendments.write_text(json.dumps({"revs": {}}))
            with (
                patch.object(publisher, "AMENDMENTS_PATH", amendments),
                patch.object(publisher, "constitution_version", return_value=None),
            ):
                self.assertFalse(publisher.register_constitution_rev())
            with (
                patch.object(publisher, "AMENDMENTS_PATH", amendments),
                patch.object(publisher, "constitution_version", return_value="v1.0"),
                patch.object(db, "constitution_rev", return_value="unknown"),
            ):
                self.assertFalse(publisher.register_constitution_rev())
            self.assertEqual(json.loads(amendments.read_text()), {"revs": {}})


if __name__ == "__main__":
    unittest.main()
