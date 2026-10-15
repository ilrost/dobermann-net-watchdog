"""The local relay: the nullmailer line, handing a message over, the trigger."""
import os
import stat
import tempfile
import unittest

from dobermann import relay


class TestRelay(unittest.TestCase):
    def test_remotes_line(self):
        cfg = {"host": "smtp.example.org", "port": 587, "security": "starttls", "username": "me"}
        self.assertEqual(relay.remotes_line(cfg, "pw"), "smtp.example.org smtp --port=587 --starttls --user=me --pass=pw")
        self.assertEqual(relay.remotes_line(dict(cfg, security="none", username=""), None),
                         "smtp.example.org smtp --port=587")
        for bad in ({"host": ""}, dict(cfg, username="a b")):
            with self.assertRaises(ValueError):
                relay.remotes_line(bad, "pw")
        with self.assertRaises(ValueError):
            relay.remotes_line(cfg, "abcd efgh")

    def test_submit_passes_sender_and_recipient_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, fake = os.path.join(tmp, "out"), os.path.join(tmp, "sendmail")
            with open(fake, "w") as f:
                f.write(f'#!/bin/sh\necho "$@" > {out}\ncat >> {out}\n[ "$5" != "fail@example.org" ]\n')
            os.chmod(fake, 0o755)
            self.assertTrue(relay.submit(b"Subject: x\n\nbody\n", "a@example.org", "b@example.org", sendmail=fake))
            with open(out) as f:
                self.assertEqual(f.read(), "-i -f a@example.org -- b@example.org\nSubject: x\n\nbody\n")
            self.assertFalse(relay.submit(b"x", "a@example.org", "fail@example.org", sendmail=fake))

    def test_flush_without_a_reader_does_not_block_or_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = os.path.join(tmp, "trigger")
            os.mkfifo(fifo)
            if not relay.shutil.which("postqueue"):
                relay.flush(trigger=fifo)                                  # ENXIO, swallowed
            relay.flush(trigger=os.path.join(tmp, "missing"))
            self.assertTrue(stat.S_ISFIFO(os.stat(fifo).st_mode))


if __name__ == "__main__":
    unittest.main()
