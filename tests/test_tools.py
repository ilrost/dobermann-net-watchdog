"""Installer and Proxmox CT script: the parts that can be checked without systemd or a Proxmox host."""
import argparse
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import install  # noqa: E402
import proxmox_ct  # noqa: E402


class TestInstall(unittest.TestCase):
    @staticmethod
    def args(tmp, *extra):
        return install.parser().parse_args(["--config-dir", os.path.join(tmp, "etc"), "--state-dir", os.path.join(tmp, "var"),
                                            "--systemd-dir", tmp, "--bin-path", os.path.join(tmp, "dobermann"),
                                            "--nullmailer-remotes", os.path.join(tmp, "remotes"), *extra])

    def test_an_update_keeps_the_choices_of_the_install(self):
        saved = {"https": False, "name": "dobermann.local", "bind": "192.0.2.5"}
        a = install.parser().parse_args([])
        self.assertEqual(install.resolve_choices(a, saved), (False, "dobermann.local", "192.0.2.5"))
        self.assertEqual(install.resolve_choices(a, {}), (True, None, None))
        a = install.parser().parse_args(["--https", "--no-name", "--bind", ""])
        self.assertEqual(install.resolve_choices(a, saved), (True, None, None))

    def test_web_env_keeps_other_lines(self):
        self.assertEqual(install.web_env_text("DOBERMANN_PORT=8080\nDOBERMANN_BIND=1.2.3.4\n", "192.0.2.5"),
                         "DOBERMANN_PORT=8080\nDOBERMANN_BIND=192.0.2.5\n")
        self.assertEqual(install.web_env_text("DOBERMANN_BIND=1.2.3.4\n", None), "")

    def test_avahi_in_a_container(self):
        self.assertIn("#rlimit-nproc=3", install.avahi_conf_for_container("[rlimits]\nrlimit-nproc=3\n"))
        self.assertIsNone(install.avahi_conf_for_container("[rlimits]\n#rlimit-nproc=3\n"))

    def test_config_merge_keeps_what_the_panel_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w") as f:
                json.dump({"mail": {"to": "a@example.org"}, "language": "it"}, f)
            self.assertTrue(install.update_config(tmp, language="de", url="https://192.0.2.5/watchdash"))
            self.assertFalse(install.update_config(tmp, language="de"))
            with open(os.path.join(tmp, "config.json")) as f:
                cfg = json.load(f)
            self.assertEqual(cfg, {"mail": {"to": "a@example.org"}, "language": "de",
                                   "dashboard_url": "https://192.0.2.5/watchdash"})

    def test_only_local_names(self):
        self.assertTrue(install.valid_name("dobermann.local"))
        self.assertFalse(install.valid_name("dobermann.lan"))
        self.assertFalse(install.valid_name("a b.local"))

    def test_units_have_no_placeholder_left_and_run_as_the_service_user(self):
        for svc in install.SERVICES:
            with open(os.path.join(install.UNIT_DIR, svc + ".service")) as f:
                text = install.render_unit(f.read(), "/opt/dobermann", "/etc/dobermann", "/var/lib/dobermann")
            self.assertNotIn("@", text, svc)
            self.assertIn("User=dobermann", text)
            self.assertIn("DOBERMANN_CONFIG_DIR=/etc/dobermann", text)
            self.assertIn("WorkingDirectory=/opt/dobermann", text)

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            calls = []
            self.assertEqual(install.run(args, out=lambda *_: None, runner=calls.append), 0)
            self.assertEqual(os.listdir(tmp), [])
            self.assertEqual(calls, [])

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root")
    def test_apply_writes_units_and_command_and_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp, "--apply")
            calls = []
            install.run(args, out=lambda *_: None, runner=calls.append)
            self.assertTrue(os.path.exists(os.path.join(tmp, "dobermann-web.service")))
            self.assertEqual(os.stat(args.config_dir).st_mode & 0o777, 0o750)
            with open(args.bin_path) as f:
                self.assertIn(f"DOBERMANN_STATE_DIR={args.state_dir}", f.read())
            self.assertIn(["systemctl", "restart", "dobermann.service"], calls)
            if install.shutil.which("openssl"):
                self.assertEqual(os.stat(os.path.join(args.config_dir, "tls.key")).st_mode & 0o777, 0o600)


class TestProxmoxCt(unittest.TestCase):
    AVAILABLE = """system          debian-12-standard_12.12-1_amd64.tar.zst                     amd64
system          debian-13-standard_13.6-1_amd64.tar.zst                      amd64
system          debian-13-standard_13.10-1_amd64.tar.zst                     amd64
system          debian-13-standard_13.6-1_arm64.tar.zst                      arm64
system          ubuntu-24.04-standard_24.04-2_amd64.tar.zst                  amd64"""

    def test_newest_debian_amd64_template(self):
        self.assertEqual(proxmox_ct.newest_template(self.AVAILABLE), "debian-13-standard_13.10-1_amd64.tar.zst")
        self.assertIsNone(proxmox_ct.newest_template("nothing here"))

    def test_network_option(self):
        self.assertEqual(proxmox_ct.net0("vmbr0", "dhcp", None), "name=eth0,bridge=vmbr0,ip=dhcp,firewall=1")
        self.assertIn("ip=192.168.1.50/24,gw=192.168.1.1", proxmox_ct.net0("vmbr0", "192.168.1.50/24", "192.168.1.1"))

    def test_create_is_unprivileged_and_starts_at_boot(self):
        args = argparse.Namespace(id=300, hostname="dobermann", cores=1, memory=256, storage="local-lvm", disk=2,
                                  bridge="vmbr0", ip="dhcp", gw=None)
        cmd = " ".join(proxmox_ct.create_plan(args, "local:vztmpl/x.tar.zst")[0])
        self.assertIn("--unprivileged 1", cmd)
        self.assertIn("--onboot 1", cmd)
        self.assertIn("--rootfs local-lvm:2", cmd)

    def test_tarball_has_the_code_but_no_git_or_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            for p in ("dobermann/__main__.py", "dobermann/__pycache__/x.pyc", ".git/HEAD", "tools/install.py"):
                os.makedirs(os.path.dirname(os.path.join(tmp, p)), exist_ok=True)
                open(os.path.join(tmp, p), "w").close()
            names = tarfile.open(fileobj=io.BytesIO(proxmox_ct.code_tarball(tmp))).getnames()
        self.assertIn("dobermann/dobermann/__main__.py", names)
        self.assertIn("dobermann/tools/install.py", names)
        self.assertFalse([n for n in names if re.search(r"\.git|__pycache__", n)], names)


if __name__ == "__main__":
    unittest.main()
