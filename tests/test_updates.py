"""New versions: tags, the daily check, the archive checks and the update with its way back."""
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from dobermann import __version__, config, updates


def tags_fetch(*names):
    return lambda url, timeout, **kw: json.dumps([{"name": n} for n in names]).encode()


def archive(files, top="dobermann-net-watchdog-0.9.0", extra=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(f"{top}/{name}")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        for ti in extra or ():
            tar.addfile(ti)
    return buf.getvalue()


GOOD = {"dobermann/__init__.py": '__version__ = "0.9.0"\n', "tools/install.py": "", "wizard.py": ""}


class TestCheck(unittest.TestCase):
    def test_newest_version_tag(self):
        self.assertEqual(updates.newest(["v0.1.0", "v0.10.0", "v0.9.3", "latest", "v1.0"]), "v0.10.0")
        self.assertIsNone(updates.newest(["nightly"]))

    def test_newer_or_not(self):
        st = updates.check(fetch=tags_fetch("v0.0.1", "v99.0.0"))
        self.assertEqual((st["latest"], st["newer"]), ("v99.0.0", True))
        self.assertIn(f"compare/v{__version__}...v99.0.0", st["compare_url"])
        self.assertFalse(updates.check(fetch=tags_fetch(f"v{__version__}"))["newer"])

    def test_a_private_repository_is_said_plainly(self):
        def fetch(url, timeout, **kw):
            raise updates.UpdateError("nessuna versione raggiungibile")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DOBERMANN_STATE_DIR": tmp}):
            st = updates.check_and_save(now=100, fetch=fetch)
            self.assertIn("nessuna versione", st["error"])
            self.assertEqual(updates.load_status()["ts"], 100)

    def test_once_a_day_and_only_if_wanted(self):
        self.assertTrue(updates.due({}, 100_000, 0))
        self.assertFalse(updates.due({}, 100_000, 100_000 - 3600))
        self.assertFalse(updates.due({"updates": {"check": False}}, 100_000, 0))

    def test_an_installed_update_is_no_longer_news(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DOBERMANN_STATE_DIR": tmp}):
            updates.save_status({"latest": f"v{__version__}", "newer": True, "ts": 1})
            self.assertFalse(updates.load_status()["newer"])


class TestArchive(unittest.TestCase):
    def test_a_good_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = updates.unpack(archive(GOOD), tmp)
            self.assertTrue(os.path.isfile(os.path.join(root, "wizard.py")))

    def test_dangerous_or_incomplete_archives_are_refused(self):
        link = tarfile.TarInfo("dobermann-net-watchdog-0.9.0/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        bad = [archive(dict(GOOD, **{"../../x": ""})), archive(GOOD, extra=[link]),
               archive({"tools/install.py": "", "wizard.py": ""}), b"not a tar"]
        for data in bad:
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(updates.UpdateError):
                updates.unpack(data, tmp)


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.code = os.path.join(self.tmp.name, "dobermann")
        os.makedirs(os.path.join(self.code, "tools"))
        with open(os.path.join(self.code, "OLD"), "w") as f:
            f.write("old")
        self.fetch = lambda url, timeout, **kw: archive(GOOD)

    def tearDown(self):
        self.tmp.cleanup()

    def run_apply(self, services_ok):
        def runner(cmd):
            ok = services_ok if cmd[0] == "systemctl" else True
            return subprocess.CompletedProcess(cmd, 0 if ok else 3, "", "")
        with mock.patch.object(updates.os, "geteuid", return_value=0):
            return updates.apply("v0.9.0", code_dir=self.code, fetch=self.fetch, runner=runner, out=lambda *a: None,
                                 wait_s=0)

    def test_new_code_in_place_old_kept(self):
        self.assertTrue(self.run_apply(True))
        self.assertTrue(os.path.isfile(os.path.join(self.code, "wizard.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.code + ".old", "OLD")))

    def test_services_not_back_means_the_old_code_returns(self):
        self.assertFalse(self.run_apply(False))
        self.assertTrue(os.path.isfile(os.path.join(self.code, "OLD")))
        self.assertFalse(os.path.exists(os.path.join(self.code, "wizard.py")))

    def test_a_git_checkout_is_left_to_git(self):
        os.makedirs(os.path.join(self.code, ".git"))
        with self.assertRaises(updates.UpdateError):
            self.run_apply(True)


class TestDefaults(unittest.TestCase):
    def test_the_daily_check_is_on_for_a_real_install(self):
        # the tests turn it off (tests/__init__.py), so the shipped default is read from the source
        with open(config.__file__) as f:
            self.assertIn('"updates": {"check": True}', f.read())


if __name__ == "__main__":
    unittest.main()
