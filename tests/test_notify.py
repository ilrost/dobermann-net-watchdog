"""Sending: who the mail comes from, and a failure explained in words the user can act on."""
import smtplib
import socket
import unittest
from unittest import mock

from dobermann import notify

GMAIL = {"to": "me@example.org",
         "smtp": {"host": "smtp.gmail.com", "port": 587, "security": "starttls", "username": "sender@example.com"}}


class FakeSmtp:
    """Stands in for smtplib.SMTP; fails at the step named in `fail_at`."""
    fail_at, error, sent = None, None, []

    def __init__(self, host, port, timeout=None, **kw):
        if self.fail_at == "connect":
            raise self.error

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        if self.fail_at == "starttls":
            raise self.error

    def login(self, user, password):
        if self.fail_at == "login":
            raise self.error

    def sendmail(self, from_addr, to_addrs, text):
        FakeSmtp.sent.append((from_addr, to_addrs))


class TestNotify(unittest.TestCase):
    def setUp(self):
        FakeSmtp.fail_at, FakeSmtp.error, FakeSmtp.sent = None, None, []
        p = mock.patch.object(notify.smtplib, "SMTP", FakeSmtp)
        p.start()
        self.addCleanup(p.stop)

    def test_sender_defaults_to_the_smtp_account(self):
        self.assertEqual(notify.sender(GMAIL), "sender@example.com")
        self.assertEqual(notify.sender(dict(GMAIL, **{"from": "x@example.org"})), "x@example.org")
        self.assertEqual(notify.sender(dict(GMAIL, smtp={"username": "plainuser"})), "me@example.org")
        self.assertTrue(notify.deliver(GMAIL, {"mail": {"password": "p"}}, "s", "b")[0])
        self.assertEqual(FakeSmtp.sent, [("sender@example.com", ["me@example.org"])])

    def test_disconnect_at_login_points_to_the_app_password_and_never_shows_the_password(self):
        FakeSmtp.fail_at, FakeSmtp.error = "login", smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        ok, why = notify.deliver(GMAIL, {"mail": {"password": "segreto-xyz"}}, "s", "b")
        self.assertFalse(ok)
        self.assertIn("login", why)
        self.assertIn("password per app", why)
        self.assertNotIn("segreto-xyz", why)

    def test_other_failures(self):
        cases = [("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"), "535"),
                 ("connect", socket.gaierror(-2, "Name or service not known"), "non trovato"),
                 ("connect", ConnectionRefusedError(), "porta"),
                 ("starttls", smtplib.SMTPNotSupportedError(), "465")]
        for stage, err, expect in cases:
            FakeSmtp.fail_at, FakeSmtp.error = stage, err
            ok, why = notify.deliver(GMAIL, {"mail": {"password": "p"}}, "s", "b")
            self.assertFalse(ok)
            self.assertIn(expect, why, (stage, err))
        self.assertNotIn("password per app", notify.explain("login", smtplib.SMTPServerDisconnected(), "smtp.example.org"))

    def test_deliver_never_raises(self):
        FakeSmtp.fail_at, FakeSmtp.error = "connect", OSError("boom")
        self.assertEqual(notify.deliver(GMAIL, {}, "s", "b"), (False, "OSError: boom"))


if __name__ == "__main__":
    unittest.main()


class TestHeaderSafety(unittest.TestCase):
    def test_a_line_break_in_a_header_is_refused_before_anything_is_sent(self):
        ok, why = notify.deliver({"to": "a@example.org\nBcc: x@example.org"}, {}, "s", "b")
        self.assertFalse(ok)
        self.assertIn("a capo", why)
