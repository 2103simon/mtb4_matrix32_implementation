"""Offline board-role USB discovery tests; no devices are opened."""
import unittest
from types import SimpleNamespace
from communication.serial_ports import (
    PortDiscoveryError, discover_port, resolve_port,
)


def port(device, *, description="", manufacturer="", product="", vid=None,
         pid=None, serial_number=None):
    return SimpleNamespace(
        device=device, description=description, manufacturer=manufacturer,
        product=product, vid=vid, pid=pid, serial_number=serial_number,
    )


class PortDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.nano = port("/dev/ttyACM1", description="Arduino Nano Every",
                         manufacturer="Arduino LLC", vid=0x2341, pid=0x0058,
                         serial_number="NANO123")
        self.feather = port("/dev/ttyACM0", description="Feather M4 CAN",
                            manufacturer="Adafruit", vid=0x239A, pid=0x80CA,
                            serial_number="FEATHER123")

    def test_resolves_distinct_devices_and_swapped_paths(self):
        for ports in ([self.feather, self.nano],
                      [self.nano, self.feather]):
            self.assertEqual(discover_port("nano", ports=ports), "/dev/ttyACM1")
            self.assertEqual(discover_port("feather", ports=ports), "/dev/ttyACM0")

    def test_nano_vid_pid_without_product(self):
        nano = port("/dev/ttyACM2", vid=0x2A03, pid=0x0058)
        self.assertEqual(discover_port("nano", ports=[nano]), "/dev/ttyACM2")

    def test_does_not_misidentify_unrelated_arduino(self):
        other = port("/dev/ttyACM2", description="Arduino Uno", vid=0x2341, pid=0x0043)
        with self.assertRaises(PortDiscoveryError):
            discover_port("nano", ports=[other])

    def test_does_not_treat_any_adafruit_as_feather_m4(self):
        other = port("/dev/ttyACM2", manufacturer="Adafruit", product="Circuit Playground")
        with self.assertRaises(PortDiscoveryError):
            discover_port("feather", ports=[other])

    def test_ambiguity_fail_closed_and_serial_number_disambiguation(self):
        another = port("/dev/ttyACM3", product="Nano Every", serial_number="NANO456")
        with self.assertRaisesRegex(PortDiscoveryError, "Ambiguous"):
            discover_port("nano", ports=[self.nano, another])
        self.assertEqual(discover_port("nano", ports=[self.nano, another],
                                      serial_number="NANO456"), "/dev/ttyACM3")

    def test_explicit_override(self):
        self.assertEqual(resolve_port("/dev/ttyACM9", "nano", ports=[]), "/dev/ttyACM9")

    def test_missing_device_has_diagnostics(self):
        with self.assertRaisesRegex(PortDiscoveryError, "Available ports"):
            resolve_port("auto", "feather", ports=[self.nano])


if __name__ == "__main__":
    unittest.main()
