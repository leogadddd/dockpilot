import tempfile
import unittest
from pathlib import Path

from dockpilot.cli import ValidationError, count_media, safe_source, read_yaml


class TestManifestSafety(unittest.TestCase):
    def test_count_media_excludes_symlink_and_nonmedia(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "photo.jpg").write_bytes(b"test")
            (root / "notes.txt").write_text("ignore")
            (root / "linked.jpg").symlink_to(root / "photo.jpg")
            self.assertEqual(count_media(root), (1, 4))

    def test_source_path_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            drive = Path(temporary)
            with self.assertRaises(ValidationError):
                safe_source(drive, "../other")
            with self.assertRaises(ValidationError):
                safe_source(drive, "/etc")

    def test_source_path_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            drive = Path(temporary)
            (drive / "photos").symlink_to("/etc")
            with self.assertRaises(ValidationError):
                safe_source(drive, "photos")

    def test_manifest_requires_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "homelab.yaml"
            path.write_text("name: test\n")
            with self.assertRaises(ValidationError):
                read_yaml(path)


if __name__ == "__main__":
    unittest.main()
