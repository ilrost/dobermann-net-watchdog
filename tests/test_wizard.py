"""The installation wizard, played by a scripted user: no root, no packages, nothing installed."""
import ipaddress
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wizard  # noqa: E402
from dobermann import i18n  # noqa: E402


class Script:
    """A user who types `answers` one by one, and a screen that keeps what was printed."""

    def __init__(self, answers, secrets=()):
        self.answers, self.secrets, self.screen = list(answers), list(secrets), []

    def console(self, assume_yes=False):
        return wizard.Console(inp=self._inp, secret=self._secret, out=self.screen.append, assume_yes=assume_yes)

    def _inp(self, prompt):
        self.screen.append(prompt)
        return self.answers.pop(0)

    def _secret(self, prompt):
        return self.secrets.pop(0)

    @property
    def text(self):
        return "\n".join(str(x) for x in self.screen)


class TestBanner(unittest.TestCase):
    def test_logo_then_name_and_version_then_a_line(self):
        lines = wizard.banner().splitlines()
        self.assertIn("__| |", lines[1])
        self.assertIn("net watchdog  v", lines[5])
        self.assertEqual(lines[-1], "=" * wizard.WIDTH)
        self.assertLessEqual(max(len(x) for x in lines), 80)


class TestAddresses(unittest.TestCase):
    def test_suggests_the_first_free_address_on_the_host_network(self):
        host = ipaddress.ip_interface("192.0.2.254/24")
        used = {"192.0.2.50", "192.0.2.51"}
        self.assertEqual(wizard.suggest_ct_address(host, in_use=lambda ip: ip in used), "192.0.2.52/24")

    def test_ct_address_needs_a_mask_and_must_be_free(self):
        free = lambda ip: False  # noqa: E731
        self.assertIsNone(wizard.check_ct_ip("dhcp", in_use=free))
        self.assertIsNone(wizard.check_ct_ip("192.0.2.50/24", in_use=free))
        self.assertIsNotNone(wizard.check_ct_ip("192.0.2.50", in_use=free))
        self.assertIsNotNone(wizard.check_ct_ip("nonsense", in_use=free))
        self.assertIsNotNone(wizard.check_ct_ip("192.0.2.50/24", in_use=lambda ip: True))


class TestConsole(unittest.TestCase):
    def test_enter_takes_the_proposed_answer_and_bad_numbers_are_asked_again(self):
        s = Script(["", "7", "2"])
        con = s.console()
        self.assertEqual(con.choose(">", [("a", "A"), ("b", "B")], 0), "a")
        self.assertEqual(con.choose(">", [("a", "A"), ("b", "B")], 0), "b")
        self.assertIn("1", s.text)

    def test_spanish_yes_and_an_unknown_answer_is_explained(self):
        s = Script(["boh", "sí"])
        self.assertTrue(s.console().yes("?"))
        self.assertTrue(any("[" not in str(x) and "/" in str(x) for x in s.screen))

    def test_password_can_be_skipped_and_must_match(self):
        self.assertIsNone(Script([], secrets=[""]).console().password())
        s = Script([], secrets=["short", "longenough", "different", "longenough", "longenough"])
        self.assertEqual(s.console().password(), "longenough")


class TestFlow(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def run_wizard(self, answers, secrets=(), argv=()):
        s = Script(answers, secrets)
        calls = {}

        def fake_install_run(args, out, password=None, summary=True):
            calls["args"], calls["password"] = args, password
            out('RESULT {"https": true, "fingerprint": "AA:BB", "setup_code": null, "name": "dobermann.local"}')
            return 0
        with mock.patch.object(wizard.os, "geteuid", return_value=0), \
                mock.patch.object(wizard.deps, "has_systemd", return_value=True), \
                mock.patch.object(wizard, "is_proxmox_host", return_value=False), \
                mock.patch.object(wizard, "local_addresses", return_value=[("eth0", "192.0.2.10/24")]), \
                mock.patch.object(wizard.deps, "missing", return_value=[]), \
                mock.patch.object(wizard.install, "run", side_effect=fake_install_run), \
                mock.patch("dobermann.__main__.primary_address", return_value="192.0.2.10"):
            code = wizard.main(list(argv), con=s.console())
        return code, s, calls

    def test_install_here_with_https_name_and_password(self):
        # language Italiano, name yes, HTTPS, password, confirm
        code, s, calls = self.run_wizard(["2", "", "", ""], secrets=["passwordlunga", "passwordlunga"])
        self.assertEqual(code, 0)
        a = calls["args"]
        self.assertEqual((a.language, a.https, a.name, a.address, a.apply), ("it", True, "dobermann.local", "192.0.2.10", True))
        self.assertEqual(calls["password"], "passwordlunga")
        self.assertIn("https://192.0.2.10/", s.text)
        self.assertIn("https://dobermann.local/", s.text)
        self.assertIn("AA:BB", s.text)
        self.assertNotIn("RESULT", s.text)
        self.assertNotIn("passwordlunga", s.text)

    def test_http_without_name_and_password_later(self):
        code, s, calls = self.run_wizard(["1", "n", "2", "y"], secrets=[""])
        self.assertEqual(code, 0)
        a = calls["args"]
        self.assertEqual((a.https, a.no_name, a.language), (False, True, "en"))
        self.assertIsNone(calls["password"])

    def test_saying_no_at_the_end_changes_nothing(self):
        code, s, calls = self.run_wizard(["1", "", "", "n"], secrets=[""])
        self.assertEqual(code, 0)
        self.assertNotIn("args", calls)

    def test_not_root(self):
        s = Script(["1"])
        with mock.patch.object(wizard.os, "geteuid", return_value=1000):
            self.assertEqual(wizard.main([], con=s.console()), 1)
        self.assertIn("sudo python3 wizard.py", s.text)


if __name__ == "__main__":
    unittest.main()
