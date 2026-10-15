"""The web panel on a scratch port with temp config/state: login, pages, settings. No network."""
import http.cookiejar
import json
import os
import re
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from dobermann import config
from dobermann.diagnosis import Observation
from dobermann.engine import Engine
from dobermann.adapters.base import SampleResult
from dobermann.web import server


class WebCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {k: os.environ.get(k) for k in ("DOBERMANN_CONFIG_DIR", "DOBERMANN_STATE_DIR")}
        os.environ["DOBERMANN_CONFIG_DIR"] = os.path.join(self.tmp.name, "etc")
        os.environ["DOBERMANN_STATE_DIR"] = os.path.join(self.tmp.name, "var")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def get(self, path):
        try:
            with self.opener.open(self.base + path, timeout=10) as r:
                return r.status, r.geturl(), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.geturl(), e.read().decode()

    def post(self, path, **fields):
        data = urllib.parse.urlencode(fields).encode()
        try:
            with self.opener.open(self.base + path, data=data, timeout=30) as r:
                return r.status, r.geturl(), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.geturl(), e.read().decode()

    def login(self):
        self.post("/login", password="password1", password2="password1")


class TestAuth(WebCase):
    def test_everything_needs_a_session(self):
        for path in ("/watchdash", "/report", "/setup", "/api/samples?from=0&to=1", "/export"):
            status, url, _ = self.get(path)
            self.assertTrue(url.endswith("/login"), path)

    def test_with_a_setup_code_the_first_visit_must_give_it(self):
        from dobermann.web import auth
        code = auth.new_setup_code()
        _, _, body = self.get("/login")
        self.assertIn("setup_code", body)
        status, _, _ = self.post("/login", password="password1", password2="password1", setup_code="WRONG-CODE")
        self.assertEqual(status, 401)
        self.assertFalse(auth.is_configured())
        status, url, _ = self.post("/login", password="password1", password2="password1", setup_code=code.lower())
        self.assertTrue(url.endswith("/setup"))
        self.assertFalse(auth.setup_code_required())
        self.assertFalse(os.path.exists(os.path.join(config.config_dir(), "setup-code")))

    def test_first_visit_chooses_the_password_then_login_works(self):
        _, _, body = self.get("/login")
        self.assertIn("Conferma password", body)
        status, url, _ = self.post("/login", password="short", password2="short")
        self.assertEqual(status, 400)
        status, url, _ = self.post("/login", password="password1", password2="password1")
        self.assertTrue(url.endswith("/setup"))
        self.post("/logout")
        status, _, _ = self.post("/login", password="wrong-one")
        self.assertEqual(status, 401)
        status, url, _ = self.post("/login", password="password1")
        self.assertTrue(url.endswith("/watchdash"))

    def test_admin_file_is_private(self):
        self.login()
        self.assertEqual(os.stat(os.path.join(config.config_dir(), "admin.json")).st_mode & 0o777, 0o600)


class TestLoginThrottle(WebCase):
    def test_throttle_counts_before_checking_per_client_and_overall(self):
        now = [0.0]
        t = server.LoginThrottle(clock=lambda: now[0])
        self.assertEqual([t.attempt("10.0.0.1") for _ in range(6)], [True] * 5 + [False])
        self.assertTrue(t.attempt("10.0.0.2"))
        t.succeeded("10.0.0.2")
        now[0] = 61
        self.assertTrue(t.attempt("10.0.0.1"))
        for i in range(t.GLOBAL - 1):
            t.attempt(f"10.0.1.{i % 250}.{i}")
        self.assertFalse(t.attempt("10.0.0.3"))

    def test_parallel_attempts_cannot_exceed_the_limit(self):
        t = server.LoginThrottle()
        results = []
        threads = [threading.Thread(target=lambda: results.append(t.attempt("10.0.0.9"))) for _ in range(50)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(results.count(True), t.PER_CLIENT)

    def test_sixth_wrong_password_is_refused_without_checking(self):
        from unittest import mock
        self.login()
        self.post("/logout")
        throttle = server.LoginThrottle()
        with mock.patch.object(server, "_login_throttle", throttle), mock.patch.object(server.time, "sleep"):
            for _ in range(5):
                self.assertEqual(self.post("/login", password="wrong-one")[0], 401)
            status, _, body = self.post("/login", password="password1")
            self.assertEqual(status, 429)
            self.assertIn("Troppi tentativi", body)


class TestPages(WebCase):
    def setUp(self):
        super().setUp()
        config.save_chain({"version": 1, "nodes": [{"id": "router", "label": "Router <prova>", "ip": "127.0.0.1"}]})
        engine = Engine(async_mail=False)
        ts = __import__("time").time() - 600
        for i in range(30):
            down = 10 <= i < 14
            m = dict(gw_loss=0.0, gw_rtt=1.0, anchors={"a": {"ip": "192.0.2.1", "loss": 100.0 if down else 0.0, "rtt": None if down else 20.0}},
                     internet_loss=100.0 if down else 0.0, internet_rtt=None if down else 20.0,
                     dns_local_ok=not down, dns_local_ms=3.0, dns_ext_ok=not down, dns_ext_ms=8.0)
            engine.step(ts=ts + i * 5, measured=(m, {}))
        engine.con.close()
        self.login()

    def test_pages_render_with_data_and_no_leftover_placeholders(self):
        for path in ("/watchdash", "/report", "/report?period=day", "/report?period=year", "/setup"):
            status, _, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertNotRegex(body, r"__[A-Z_]+__", path)
            self.assertNotIn("FritzBox", body)
        _, _, body = self.get("/watchdash")
        data = json.loads(re.search(r"const DATA = (\{.*?\});\n", body, re.S).group(1))
        self.assertEqual([i["cause"] for i in data["incidents"]], ["UPSTREAM_OUTAGE"])
        self.assertIn("Router <prova> (127.0.0.1)", data["source"]["chain"])
        self.assertFalse(data["has_cellular"])

    def test_chain_label_is_escaped_in_settings(self):
        _, _, body = self.get("/setup")
        self.assertIn("Router &lt;prova&gt;", body)
        self.assertNotIn("Router <prova>", body)

    def test_samples_api_and_export(self):
        import time
        status, _, body = self.get(f"/api/samples?from={time.time() - 3600}&to={time.time()}")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)), 30)
        status, _, body = self.get("/export?table=samples")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith("ts,gw_loss"))


class TestSettings(WebCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_mail_password_goes_to_secrets_and_is_never_shown(self):
        status, _, body = self.post("/setup", action="save_mail", enabled="1", to="io@example.org", transport="smtp",
                                    smtp_host="smtp.example.org",
                                    smtp_port="587", smtp_security="starttls", smtp_username="io",
                                    smtp_password="segreto-123", dashboard_url="")
        self.assertIn("Notifiche salvate", body)
        self.assertNotIn("segreto-123", body)
        with open(config.config_file()) as f:
            self.assertNotIn("segreto-123", f.read())
        self.assertEqual(config.load_secrets()["mail"]["password"], "segreto-123")
        self.assertEqual(config.load()["mail"]["smtp"]["host"], "smtp.example.org")
        # saving again with an empty password keeps the stored one
        self.post("/setup", action="save_mail", enabled="1", to="io@example.org", transport="smtp",
                  smtp_host="smtp.example.org", smtp_port="587",
                  smtp_security="starttls", smtp_username="io", smtp_password="", dashboard_url="")
        self.assertEqual(config.load_secrets()["mail"]["password"], "segreto-123")

    def test_stored_password_never_goes_to_a_changed_server(self):
        from unittest import mock
        from dobermann import notify
        base = dict(action="save_mail", enabled="1", to="io@example.org", transport="smtp", smtp_host="smtp.example.org",
                    smtp_port="587", smtp_security="starttls", smtp_username="io", dashboard_url="")
        self.post("/setup", **dict(base, smtp_password="segreto-123"))
        with mock.patch.object(notify, "deliver", return_value=(True, None)) as deliver:
            status, _, body = self.post("/setup", **dict(base, action="test_mail", smtp_host="evil.example.net", smtp_password=""))
            self.assertEqual(status, 400)
            self.assertIn("riscrivi la password", body)
            deliver.assert_not_called()
            status, _, body = self.post("/setup", **dict(base, smtp_host="evil.example.net", smtp_password=""))
            self.assertEqual(status, 400)
            self.assertEqual(config.load()["mail"]["smtp"]["host"], "smtp.example.org")
            # same server and user: the stored password is used
            self.post("/setup", **dict(base, action="test_mail", smtp_password=""))
            self.assertEqual(deliver.call_args[0][1]["mail"]["password"], "segreto-123")
            # a new server with its own password is fine
            self.post("/setup", **dict(base, action="test_mail", smtp_host="smtp.other.org", smtp_password="altra"))
            self.assertEqual(deliver.call_args[0][1]["mail"]["password"], "altra")

    def test_test_mail_shows_why_it_failed(self):
        from unittest import mock
        from dobermann import notify
        with mock.patch.object(notify, "deliver", return_value=(False, "il server ha chiuso <la> connessione")), \
                mock.patch("builtins.print"):
            status, _, body = self.post("/setup", action="test_mail", enabled="1", to="io@example.org", transport="relay",
                                        smtp_host="smtp.example.org", smtp_port="587", smtp_security="starttls",
                                        smtp_username="io", smtp_password="", dashboard_url="")
        self.assertIn("Invio fallito: il server ha chiuso &lt;la&gt; connessione", body)

    def test_relay_save_writes_nullmailer_remotes(self):
        from unittest import mock
        from dobermann import relay
        remotes = os.path.join(self.tmp.name, "remotes")
        with mock.patch.object(relay, "detect", return_value="nullmailer"), \
                mock.patch.object(relay, "NULLMAILER_REMOTES", remotes):
            status, _, body = self.post("/setup", action="save_mail", enabled="1", to="io@example.org", transport="relay",
                                        smtp_host="smtp.example.org", smtp_port="465", smtp_security="ssl",
                                        smtp_username="io@example.org", smtp_password="pw-1", dashboard_url="")
            self.assertIn("nullmailer configurato", body)
            with open(remotes) as f:
                self.assertEqual(f.read(), "smtp.example.org smtp --port=465 --ssl --user=io@example.org --pass=pw-1\n")
            status, _, body = self.post("/setup", action="save_mail", enabled="1", to="io@example.org", transport="relay",
                                        smtp_host="smtp.example.org", smtp_port="465", smtp_security="ssl",
                                        smtp_username="io@example.org", smtp_password="con spazi", dashboard_url="")
            self.assertEqual(status, 400)
            self.assertIn("spazi", body)
        self.assertEqual(config.load_secrets()["mail"]["password"], "pw-1")   # the refused save changed nothing

    def test_alert_rules_save_and_validation(self):
        fields = dict(action="save_alerts", mode_outage="after", after_outage="3", mode_ip_renewal="never", after_ip_renewal="2",
                      mode_slow="summary", after_slow="5", mode_legacy_rat="summary", after_legacy_rat="5",
                      mode_dns="immediate", after_dns="5", summary_frequency="daily", summary_weekday="2", summary_hour="7")
        status, _, body = self.post("/setup", **fields)
        self.assertIn("Regole degli avvisi salvate", body)
        m = config.load()["mail"]
        self.assertEqual(m["rules"]["outage"], {"mode": "after", "after_min": 3})
        self.assertEqual(m["summary"], {"frequency": "daily", "weekday": 2, "hour": 7, "send_if_quiet": False})
        self.assertEqual(config.load()["mail"]["transport"], "relay")      # the rest of the mail settings untouched
        status, _, _ = self.post("/setup", **dict(fields, mode_dns="sempre"))
        self.assertEqual(status, 400)
        _, _, body = self.get("/setup")
        self.assertIn('<option value="never" selected>Mai</option>', body)

    def test_measure_validation_is_all_or_nothing(self):
        status, _, body = self.post("/setup", action="save_measure", interval_s="5", anchors="a,1.1.1.1\nb,nope",
                                    dns_local="", dns_external="1.1.1.1", high_latency_ms="150", packet_loss_pct="34",
                                    internet_down_pct="66", confirm_samples="2", retention_days="30")
        self.assertEqual(status, 400)
        self.assertIn("ancora non valida", body)
        self.assertFalse(os.path.exists(config.config_file()))
        status, _, body = self.post("/setup", action="save_measure", interval_s="10", anchors="a,1.1.1.1\nb,9.9.9.9",
                                    dns_local="", dns_external="1.1.1.1", high_latency_ms="200", packet_loss_pct="34",
                                    internet_down_pct="66", confirm_samples="2", retention_days="30")
        self.assertEqual(status, 200)
        cfg = config.load()
        self.assertEqual((cfg["interval_s"], cfg["thresholds"]["high_latency_ms"]), (10, 200))
        self.assertIsNone(cfg["dns"]["local"])

    def test_chain_edit_and_device_password(self):
        config.save_chain({"version": 1, "nodes": [
            {"id": "router", "label": "R", "ip": "127.0.0.1"},
            {"id": "modem", "label": "M", "ip": "127.0.0.2", "adapter": "zte_goform", "options": {}}]})
        _, _, body = self.get("/setup")
        self.assertIn('name="pw_modem"', body)          # only the device that takes a password gets the field
        self.assertNotIn('name="pw_router"', body)
        self.post("/setup", action="save_chain", label_router="Il mio router", label_modem="M", pw_modem="zte-pw")
        self.assertEqual(config.load_chain()["nodes"][0]["label"], "Il mio router")
        self.assertEqual(config.load_secrets()["devices"]["modem"]["password"], "zte-pw")
        self.post("/setup", action="save_chain", label_router="Il mio router", remove_modem="1")
        self.assertEqual([n["id"] for n in config.load_chain()["nodes"]], ["router"])
        self.assertNotIn("modem", config.load_secrets()["devices"])

    def test_detect_drops_a_password_whose_device_moved(self):
        from unittest import mock
        from dobermann import discover
        config.save_chain({"version": 1, "nodes": [{"id": "modem", "label": "M", "ip": "192.168.2.1"}]})
        config.save_secrets({"devices": {"modem": {"password": "old"}}})
        new = {"version": 1, "nodes": [{"id": "router", "label": "R", "ip": "192.168.1.1"},
                                       {"id": "modem", "label": "Altro", "ip": "192.168.3.1"}]}
        with mock.patch.object(discover, "propose_chain", return_value=(new, ["nota"])):
            _, _, body = self.post("/setup", action="detect", extra="192.168.3.1")
        self.assertIn("Catena rilevata", body)
        self.assertEqual(config.load_chain(), new)
        self.assertNotIn("modem", config.load_secrets().get("devices", {}))

    def test_interval_api(self):
        req = urllib.request.Request(self.base + "/api/interval", data=json.dumps({"interval": 12}).encode(),
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req) as r:
            self.assertEqual(json.load(r)["interval"], 12)
        self.assertEqual(config.load()["interval_s"], 12)

    def test_password_change_keeps_this_browser_logged_in(self):
        self.post("/setup", action="password", new_password="nuova-pass", new_password2="nuova-pass")
        status, url, _ = self.get("/setup")
        self.assertTrue(url.endswith("/setup"))


if __name__ == "__main__":
    unittest.main()


class TestToolbarAndAddress(WebCase):
    def test_every_page_has_the_four_icons(self):
        self.login()
        for path in ("/watchdash", "/report", "/setup"):
            _, _, body = self.get(path)
            for target in ('action="/language"', 'href="/setup"', 'action="/logout-all"', 'action="/logout"'):
                self.assertIn(target, body, (path, target))

    def test_language_menu_changes_the_language_and_returns_to_the_page(self):
        self.login()
        status, url, body = self.post("/language", lang="it", next="/report")
        self.assertTrue(url.endswith("/report"))
        self.assertIn('<html lang="it">', body)
        self.assertEqual(config.load()["language"], "it")
        _, url, _ = self.post("/language", lang="xx", next="//evil.example")
        self.assertTrue(url.endswith("/watchdash"))
        self.assertEqual(config.load()["language"], "it")

    def test_log_out_everywhere(self):
        self.login()
        self.post("/logout-all")
        _, url, _ = self.get("/setup")
        self.assertTrue(url.endswith("/login"))

    def test_address_change_on_proxmox_prepares_the_host_command(self):
        self.login()
        raw = json.load(open(config.config_file())) if os.path.exists(config.config_file()) else {}
        raw["platform"] = {"kind": "proxmox-lxc", "ctid": 210, "bridge": "vmbr0"}
        config.save(raw)
        _, _, body = self.post("/setup", action="change_ip", new_ip="192.0.2.60/24", new_gw="192.0.2.1")
        self.assertIn("pct set 210 -net0 name=eth0,bridge=vmbr0,firewall=1,gw=192.0.2.1", body)
        self.assertIn("ip=192.0.2.60/24,type=veth", body)
        self.assertEqual(config.load()["dashboard_url"], "http://192.0.2.60/watchdash")
        status, _, body = self.post("/setup", action="change_ip", new_ip="192.0.2.60/24", new_gw="198.51.100.1")
        self.assertEqual(status, 400)

    def test_panel_link_is_saved_from_the_address_section(self):
        self.login()
        self.post("/setup", action="save_link", dashboard_url="https://192.0.2.9/watchdash")
        self.assertEqual(config.load()["dashboard_url"], "https://192.0.2.9/watchdash")
        status, _, _ = self.post("/setup", action="save_link", dashboard_url="javascript:alert(1)")
        self.assertEqual(status, 400)


class TestUpdatesSection(WebCase):
    def test_check_now_shows_a_failure_as_a_failure(self):
        from dobermann import updates
        self.login()
        with mock.patch.object(updates, "check", side_effect=updates.UpdateError("GitHub giù")):
            status, _, body = self.post("/setup", action="check_updates")
        self.assertEqual(status, 200)
        self.assertIn("banner error", body)
        self.assertIn("Ultimo controllo non riuscito: GitHub giù", body)

    def test_a_newer_version_shows_in_the_toolbar_and_the_daily_check_can_be_turned_off(self):
        from dobermann import updates
        self.login()
        updates.save_status({"latest": "v99.0.0", "newer": True, "compare_url": "https://example.org/c", "ts": 1})
        _, _, body = self.get("/watchdash")
        self.assertIn('class="upd"', body)
        self.post("/setup", action="save_updates")
        self.assertFalse(config.load()["updates"]["check"])


class TestAuthConcurrency(WebCase):
    def test_parallel_logins_keep_every_session_and_the_password(self):
        from dobermann.web import auth
        auth.set_password("password1")
        tokens = []
        threads = [threading.Thread(target=lambda: tokens.append(auth.create_session())) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(auth.check_session(t) for t in tokens))
        self.assertTrue(auth.verify_password("password1"))


class TestRequestHardening(WebCase):
    def raw(self, request):
        import socket
        host, port = self.base.split("//")[1].split(":")
        with socket.create_connection((host, int(port)), timeout=10) as s:
            s.sendall(request)
            return s.recv(4096).decode(errors="replace")

    def test_a_negative_content_length_is_not_read_until_the_client_hangs_up(self):
        reply = self.raw(b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\npassword=aaaaaaaa")
        self.assertTrue(reply.startswith("HTTP/1."), reply[:80])

    def test_logout_is_a_post_and_a_link_cannot_log_out(self):
        self.login()
        _, url, _ = self.get("/logout")
        self.assertTrue(url.endswith("/watchdash"))
        _, url, _ = self.post("/logout")
        self.assertTrue(url.endswith("/login"))

    def test_interval_api_refuses_a_body_that_is_not_an_object(self):
        self.login()
        req = urllib.request.Request(self.base + "/api/interval", data=b"[1]", method="POST")
        try:
            self.opener.open(req, timeout=10)
            status = 200
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 400)
