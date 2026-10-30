"""The speed test: what it measures, one at a time, and that the engine does not log its own slowness."""
import os
import tempfile
import unittest
import urllib.error

from dobermann import speedtest, store


class Body:
    def __init__(self, n, clock=None, per_chunk=0.0):
        self.left, self.clock, self.per_chunk = n, clock, per_chunk

    def read(self, k=-1):
        k = self.left if k < 0 else min(k, self.left)
        self.left -= k
        if self.clock:
            self.clock.t += self.per_chunk
        return b"x" * k

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class TestSpeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = store.connect(os.path.join(self.tmp.name, "d.db"))
        self.addCleanup(self.con.close)

    def test_speed_is_what_moved_over_the_time_it_took(self):
        clock = Clock()

        def opener(url, data=None, timeout=10):
            if data is not None:
                clock.t += 2.0                     # 2 MB up in 2 s
                return Body(0)
            return Body(int(url.split("=")[1]), clock, per_chunk=0.01)
        res = speedtest.run(self.con, {}, 10, opener=opener, clock=clock, now=lambda: 1000.0)
        self.assertIsNone(res["error"])
        # 10 MB in 153 chunks of 64 KiB, 0.01 s each
        self.assertAlmostEqual(res["down_bps"] / 1e6, 10e6 * 8 / 1.53 / 1e6, places=0)
        self.assertEqual(res["up_bps"], 8_000_000)
        self.assertEqual(res["bytes"], 12_000_000)
        self.assertFalse(speedtest.running(self.con, 1000.0))
        self.assertEqual(len(speedtest.history(self.con)), 1)

    def test_the_download_stops_at_the_time_limit(self):
        clock = Clock()
        got, secs = speedtest.download("u?bytes={bytes}", 100_000_000,
                                       opener=lambda url, timeout=10: Body(100_000_000, clock, per_chunk=1.0),
                                       clock=clock)
        self.assertEqual(secs, speedtest.TIME_LIMIT_S)
        self.assertLess(got, 100_000_000)

    def test_a_failure_is_recorded_not_raised(self):
        def opener(url, data=None, timeout=10):
            raise urllib.error.URLError("no route")
        res = speedtest.run(self.con, {}, 25, opener=opener, now=lambda: 5.0)
        self.assertIn("no route", res["error"])
        self.assertEqual(speedtest.history(self.con)[0]["error"], res["error"])

    def test_only_offered_sizes_and_one_at_a_time(self):
        with self.assertRaises(ValueError):
            speedtest.run(self.con, {}, 7)
        speedtest._lock.acquire()
        try:
            with self.assertRaises(speedtest.Busy):
                speedtest.run(self.con, {}, 10)
        finally:
            speedtest._lock.release()

    def test_other_server_from_the_configuration(self):
        cfg = {"speedtest": {"download_url": "https://example.net/d?b={bytes}", "upload_url": "https://example.net/u"}}
        self.assertEqual(speedtest.urls(cfg), ("https://example.net/d?b={bytes}", "https://example.net/u"))


if __name__ == "__main__":
    unittest.main()
