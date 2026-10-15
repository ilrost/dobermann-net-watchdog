"""Changing dobermann's address: the commands for each platform."""
import ipaddress
import unittest

from dobermann.web import address


class TestAddress(unittest.TestCase):
    IF, GW = ipaddress.ip_interface("192.0.2.60/24"), ipaddress.ip_address("192.0.2.1")

    def test_new_address_needs_mask_and_a_gateway_on_its_network(self):
        self.assertEqual(address.parse_new("192.0.2.60/24", "192.0.2.1"), (self.IF, self.GW))
        for ip, gw in (("192.0.2.60", "192.0.2.1"), ("192.0.2.60/24", "10.0.0.1"), ("192.0.2.0/24", "192.0.2.1"),
                       ("x", "192.0.2.1"), ("192.0.2.60/24", "")):
            with self.assertRaises(ValueError):
                address.parse_new(ip, gw)

    def test_proxmox_keeps_the_mac_and_reboots(self):
        where, cmds = address.commands({"kind": "proxmox-lxc", "ctid": 210, "bridge": "vmbr1"}, self.IF, self.GW,
                                       mac="BC:24:11:00:00:01")
        self.assertEqual(cmds, ["pct set 210 -net0 name=eth0,bridge=vmbr1,firewall=1,gw=192.0.2.1,"
                                "hwaddr=BC:24:11:00:00:01,ip=192.0.2.60/24,type=veth", "pct reboot 210"])

    def test_networkmanager_and_the_others(self):
        _, cmds = address.commands({"kind": "networkmanager", "connection": "Wired 1"}, self.IF, self.GW,
                                   extra_restarts=["dobermann-mdns"])
        self.assertIn('nmcli connection modify "Wired 1" ipv4.method manual ipv4.addresses 192.0.2.60/24 '
                      'ipv4.gateway 192.0.2.1', cmds)
        self.assertEqual(cmds[-1], "systemctl restart dobermann-mdns")
        for kind in ("ifupdown", "networkd", "unknown"):
            _, cmds = address.commands({"kind": kind}, self.IF, self.GW)
            self.assertTrue(any("192.0.2.60/24" in c for c in cmds), kind)

    def test_platform_from_the_installer_record_first(self):
        self.assertEqual(address.detect_platform({"platform": {"kind": "proxmox-lxc", "ctid": 5}})["ctid"], 5)
        self.assertEqual(address.detect_platform({}, exists=lambda p: p == "/etc/network/interfaces",
                                                 nm=lambda: None)["kind"], "ifupdown")

    def test_certificate_covers_old_and_new(self):
        self.assertEqual(address.cert_names("192.0.2.50", self.IF.ip, "dobermann.local"),
                         ["192.0.2.60", "192.0.2.50", "dobermann.local"])


if __name__ == "__main__":
    unittest.main()
