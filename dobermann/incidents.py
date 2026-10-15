"""Turning a stream of per-sample states into incidents.

The state machine is a class that takes no network and no database so it can be tested sample by sample:
  * a non-OK streak is one incident; when it passes through several causes they are its phases;
  * a noisy cause (see diagnosis.NOISY_CAUSES) opens an incident only after `confirm` samples in a
    row, so a single borderline sample is recorded but not reported;
  * an incident is closed only after a grace period of OK samples, and resumes if the problem comes
    back within it, so a cascading reconnect is one event and not three;
  * the alert goes out at most once per incident; whether and when is the user's rule for the kind of
    event (alerts.py), asked through on_open on every sample of the incident until it says yes.
"""
from dobermann import diagnosis


class Tracker:
    def __init__(self, confirm_samples=2, on_open=None, on_close=None):
        self.confirm = confirm_samples
        self.on_open = on_open or (lambda inc, obs, cause: None)      # True once the alert went out
        self.on_close = on_close or (lambda inc, ok_since: None)      # incident finished
        self.open = None
        self.pending = None            # {"incident", "ok_since"} during the grace period
        self.noisy_streak = 0
        self.prev_noisy = None
        self.ip = None                 # last known public/WAN IP

    @staticmethod
    def merge_grace(interval):
        # one missed or late sample must not split a reconnect, minutes apart must not merge
        return max(2 * interval, 10)

    def _new(self, ts):
        return {"start_ts": ts, "end_ts": ts, "phases": [], "samples_count": 0,
                "max_gw_loss": 0, "max_internet_loss": 0, "max_rtt": 0,
                "ip_before": self.ip, "ip_after": self.ip,
                "max_rx_bps": 0, "max_tx_bps": 0, "alerted": False}

    @staticmethod
    def _add_phase(inc, cause, ts):
        phases = inc["phases"]
        if phases and phases[-1]["cause"] == cause:
            phases[-1]["end_ts"] = ts
            phases[-1]["samples_count"] += 1
        else:
            phases.append({"cause": cause, "start_ts": ts, "end_ts": ts, "samples_count": 1})

    def feed(self, obs, state, interval, wan_ip=None, rx_bps=None, tx_bps=None):
        ts = obs.ts
        grace = self.merge_grace(interval)
        if state != "OK":
            if state in diagnosis.NOISY_CAUSES:
                self.noisy_streak = self.noisy_streak + 1 if state == self.prev_noisy else 1
                self.prev_noisy = state
            else:
                self.noisy_streak, self.prev_noisy = 0, None
            resumable = self.open is None and self.pending is not None and ts - self.pending["ok_since"] <= grace
            fresh = self.open is None and not resumable
            if fresh and state in diagnosis.NOISY_CAUSES and self.noisy_streak < self.confirm:
                pass   # unconfirmed blip from a clean baseline: in the samples, not an incident
            else:
                if self.open is None:
                    if resumable:
                        self.open = self.pending["incident"]
                    else:
                        if self.pending is not None:
                            self.on_close(self.pending["incident"], self.pending["ok_since"])
                        self.open = self._new(ts)
                    self.pending = None
                inc = self.open
                self._add_phase(inc, state, ts)
                if not inc["alerted"] and self.on_open(inc, obs, state):
                    inc["alerted"] = True
                inc["end_ts"] = ts
                inc["samples_count"] += 1
                inc["max_gw_loss"] = max(inc["max_gw_loss"], obs.gw_loss)
                inc["max_internet_loss"] = max(inc["max_internet_loss"], obs.internet_loss)
                if obs.internet_rtt:
                    inc["max_rtt"] = max(inc["max_rtt"], obs.internet_rtt)
                inc["ip_after"] = wan_ip or inc["ip_after"]
                inc["max_rx_bps"] = max(inc["max_rx_bps"], rx_bps or 0)
                inc["max_tx_bps"] = max(inc["max_tx_bps"], tx_bps or 0)
        else:
            self.noisy_streak, self.prev_noisy = 0, None
            if self.open is not None:
                # the new IP usually shows up only on the first OK sample
                self.open["ip_after"] = wan_ip or self.open["ip_after"]
                self.pending = {"incident": self.open, "ok_since": ts}
                self.open = None
            elif self.pending is not None and ts - self.pending["ok_since"] > grace:
                self.on_close(self.pending["incident"], self.pending["ok_since"])
                self.pending = None
        # after the sample: an incident opened now keeps the IP from before it as ip_before
        if wan_ip:
            self.ip = wan_ip
