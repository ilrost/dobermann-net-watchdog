"""The mail queue: nothing lost while the provider cannot be reached, the order kept, old mail dropped."""
import os
import tempfile
import unittest
from unittest import mock

from dobermann import mailqueue, store


class Provider:
    """Stands in for notify.deliver: up or down, and what it took."""

    def __init__(self, up=True):
        self.up, self.taken = up, []

    def __call__(self, mail_cfg, secrets, subject, body):
        if not self.up:
            return False, "server non raggiungibile"
        self.taken.append(subject)
        return True, None


class TestMailQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(os.path.join(self.tmp.name, "d.db"))
        self.addCleanup(self.con.close)
        p = mock.patch("builtins.print")
        p.start()
        self.addCleanup(p.stop)

    def run_round(self, provider, now):
        return mailqueue.deliver_due(self.con, {}, {}, now=now, deliver=provider)

    def test_mails_wait_while_down_and_go_in_order_when_back(self):
        down = Provider(up=False)
        mailqueue.add(self.con, "giù", "b", now=1000)
        self.assertEqual(self.run_round(down, 1000), 1)
        mailqueue.add(self.con, "di nuovo su", "b", now=1010)
        self.assertFalse(mailqueue.due(self.con, 1020))      # both wait for the back-off, the order holds
        st = mailqueue.status(self.con)
        self.assertEqual((st["waiting"], st["last_ok"]), (2, None))
        self.assertIn("non raggiungibile", st["last_error"][1])
        mailqueue.retry_now(self.con, now=1030)               # the connection is back
        up = Provider()
        self.assertEqual(self.run_round(up, 1030), 0)
        self.assertEqual(up.taken, ["giù", "di nuovo su"])
        self.assertEqual(mailqueue.status(self.con)["last_ok"], 1030)

    def test_back_off_grows_and_stops_growing(self):
        down = Provider(up=False)
        mailqueue.add(self.con, "s", "b", now=0)
        now, waits = 0, []
        for _ in range(7):
            self.run_round(down, now)
            nxt = self.con.execute("SELECT next_try FROM outbox").fetchone()[0]
            waits.append(nxt - now)
            now = nxt
        self.assertEqual(waits, [60, 120, 300, 600, 1800, 1800, 1800])

    def test_a_week_old_mail_is_dropped_and_counted(self):
        mailqueue.add(self.con, "vecchia", "b", now=0)
        mailqueue.add(self.con, "nuova", "b", now=mailqueue.MAX_AGE_S)
        up = Provider()
        self.run_round(up, mailqueue.MAX_AGE_S + 10)
        self.assertEqual(up.taken, ["nuova"])
        self.assertEqual(mailqueue.status(self.con)["dropped"], 1)

    def test_an_exception_in_delivery_is_a_failure_not_a_crash(self):
        def boom(*a):
            raise RuntimeError("x")
        mailqueue.add(self.con, "s", "b", now=0)
        self.assertEqual(self.run_round(boom, 0), 1)
        self.assertIn("RuntimeError", mailqueue.status(self.con)["last_error"][1])

    def test_sender_runs_one_round_at_a_time_on_its_own_connection(self):
        path = os.path.join(self.tmp.name, "d.db")
        mailqueue.add(self.con, "s", "b")
        sender = mailqueue.Sender(path)
        up = Provider()
        with mock.patch.object(mailqueue.notify, "deliver", up):
            sender.kick({}, {}, wait=True)
        self.assertEqual(up.taken, ["s"])
        self.assertEqual(mailqueue.status(self.con)["waiting"], 0)


if __name__ == "__main__":
    unittest.main()
