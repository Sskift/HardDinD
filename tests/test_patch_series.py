import importlib.util
import io
import pathlib
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("series", pathlib.Path(__file__).resolve().parents[1] / "pipeline/patches.py")
series = importlib.util.module_from_spec(spec)
spec.loader.exec_module(series)


class PatchSeriesTests(unittest.TestCase):
    def test_fuse_target_is_allowed_but_other_subtrees_and_symlinks_are_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "linux-test"
            target = source / "fs/fuse/dev.c"
            target.parent.mkdir(parents=True)
            target.write_text("before\n")
            archive = root / "linux.tar"
            with tarfile.open(archive, "w") as tar:
                info = tarfile.TarInfo("linux-test/fs/fuse/dev.c")
                info.size = len(b"before\n")
                tar.addfile(info, io.BytesIO(b"before\n"))
            patch = root / "fuse.patch"
            text = "--- a/fs/fuse/dev.c\n+++ b/fs/fuse/dev.c\n@@ -1 +1 @@\n-before\n+after\n"
            patch.write_text(text)
            series.apply_series(source, archive, [patch])
            self.assertEqual(target.read_text(), "after\n")
            for name in ("fs/other/dev.c", "fs/fuse/../../../escape"):
                patch.write_text(text.replace("fs/fuse/dev.c", name))
                with self.assertRaisesRegex(ValueError, "unexpected patch path"):
                    series.apply_series(source, archive, [patch])
                self.assertEqual(target.read_text(), "after\n")
            outside = root / "outside.c"
            outside.write_text("keep\n")
            target.unlink()
            target.symlink_to(outside)
            patch.write_text(text)
            with self.assertRaisesRegex(ValueError, "patch path escapes source"):
                series.apply_series(source, archive, [patch])
            self.assertEqual(outside.read_text(), "keep\n")

    def test_overlapping_series_reapply_and_preserve_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "linux-test"
            target = source / "arch/um/test.c"
            target.parent.mkdir(parents=True)
            target.write_text("original\n")
            archive = root / "source.tar"
            with tarfile.open(archive, "w") as tar:
                info = tarfile.TarInfo("linux-test/arch/um/test.c")
                info.size = len(b"original\n")
                tar.addfile(info, io.BytesIO(b"original\n"))
            patches = []
            for i, (before, after) in enumerate([( "original", "first"), ("first", "second")]):
                p = root / f"{i}.patch"
                p.write_text(f"--- a/arch/um/test.c\n+++ b/arch/um/test.c\n@@ -1 +1 @@\n-{before}\n+{after}\n")
                patches.append(p)
            series.apply_series(source, archive, patches[:1])
            series.apply_series(source, archive, patches)
            series.apply_series(source, archive, patches)
            self.assertEqual(target.read_text(), "second\n")
            series.apply_series(source, archive, patches[:1])
            self.assertEqual(target.read_text(), "first\n")
            series.apply_series(source, archive, patches)
            target.write_text("developer edit\n")
            with self.assertRaisesRegex(ValueError, "local kernel edit"):
                series.apply_series(source, archive, patches)
            self.assertEqual(target.read_text(), "developer edit\n")
