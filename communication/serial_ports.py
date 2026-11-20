"""Fail-closed role-aware USB serial-port discovery.

Select by USB identity, not volatile /dev/ttyACM numbers. Never open
candidate ports or probe firmware (opening may reset an Arduino).
An explicit path always wins. Ambiguous matches require operator choice.
"""
from __future__ import annotations


class PortDiscoveryError(RuntimeError):
    pass


def _label(port):
    return (
        f"{getattr(port, 'device', '?')} "
        f"(description={getattr(port, 'description', None)!r}, "
        f"manufacturer={getattr(port, 'manufacturer', None)!r}, "
        f"product={getattr(port, 'product', None)!r}, "
        f"VID:PID={_usb_id(port)}, serial={getattr(port, 'serial_number', None)!r})"
    )


def _usb_id(port):
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    return (
        f"{vid:04x}:{pid:04x}" if isinstance(vid, int)
        and isinstance(pid, int) else "unknown"
    )


def _role(port):
    manufacturer = str(getattr(port, "manufacturer", "") or "").lower()
    product = str(getattr(port, "product", "") or "").lower()
    description = str(getattr(port, "description", "") or "").lower()
    text = " ".join((manufacturer, product, description))
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)

    # First honor explicit product identity. The Nano Every differs from
    # other Arduino devices; the Feather M4 differs from other Adafruit boards.
    if "nano every" in text:
        return "nano"
    if "feather" in text and ("m4" in text or "can" in text):
        return "feather"

    # Some USB descriptors omit product strings. Nano Every is known to
    # enumerate with Arduino's 2341:0058 or older 2A03:0058 identity.
    # Do not treat every Arduino or every Adafruit VID as the desired board.
    if vid in (0x2341, 0x2A03) and pid == 0x0058:
        return "nano"
    return None


def list_candidate_ports(ports=None):
    if ports is not None:
        return list(ports)
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise PortDiscoveryError("pyserial is required for USB port discovery") from exc
    return list(list_ports.comports())


def discover_port(role, *, ports=None, serial_number=None):
    """Find unique Nano Every or Feather M4. No arbitrary first-port fallback."""
    if role not in {"nano", "feather"}:
        raise ValueError(f"Unknown serial-device role: {role!r}")
    found = list_candidate_ports(ports)
    matches = [p for p in found if _role(p) == role]
    if serial_number is not None:
        matches = [p for p in matches
                   if str(getattr(p, "serial_number", None)) == str(serial_number)]
    if len(matches) == 1:
        return str(matches[0].device)
    detail = "\n  ".join(_label(p) for p in found) if found else "(no serial devices)"
    if not matches:
        raise PortDiscoveryError(
            f"No uniquely identifiable {role} USB serial device found. "
            f"Available ports:\n  {detail}\n"
            "Check USB descriptors with 'python -m serial.tools.list_ports -v' "
            "or pass an explicit port."
        )
    raise PortDiscoveryError(
        f"Ambiguous {role} devices ({len(matches)} matches). "
        f"Pass an explicit port or serial number. Available ports:\n  {detail}"
    )


def resolve_port(port, role, *, ports=None, serial_number=None):
    if port and str(port).lower() != "auto":
        return str(port)
    return discover_port(role, ports=ports, serial_number=serial_number)


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", choices=("nano", "feather", "both"), default="both")
    a = p.parse_args(argv)
    for role in (("nano", "feather") if a.role == "both" else (a.role,)):
        try:
            print(f"{role}: {discover_port(role)}")
        except PortDiscoveryError as exc:
            print(f"{role}: {exc}")
            if a.role != "both":
                raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
