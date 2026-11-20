"""Resolve a Linux USBTMC scope node without assuming /dev/usbtmcN is stable.

Resolution reads filesystem/sysfs metadata only; it does not contact the
instrument, start an acquisition, or stimulate. The caller must query *IDN?
and verify model identity before making any configuration changes.
"""

from pathlib import Path
import re


EXPECTED_VENDOR_ID = "0699"
EXPECTED_PRODUCT_ID = "0450"
EXPECTED_IDN_MANUFACTURER = "TEKTRONIX"
EXPECTED_IDN_MODEL = "MDO4104B-6"


def _usb_vendor_product(node_name, sysfs_root):
    """Read USB vendor/product from the interface's ancestors, if available."""
    interface = sysfs_root / node_name / "device"
    if not interface.exists():
        return None
    resolved = interface.resolve()
    for parent in (resolved, *resolved.parents):
        vendor_path = parent / "idVendor"
        product_path = parent / "idProduct"
        if vendor_path.is_file() and product_path.is_file():
            try:
                return (vendor_path.read_text().strip().lower(),
                        product_path.read_text().strip().lower())
            except OSError:
                return None
    return None


def resolve_scope_device(requested="auto", *, dev_root=Path("/dev"),
                         sysfs_root=Path("/sys/class/usbmisc")):
    """Find the MDO4104B-6's USBTMC node, or fail closed on ambiguity.

    With one node, only *IDN? verification by the caller can prove the model.
    With multiple nodes, sysfs USB IDs select the Tektronix 0699:0450.
    If selection is ambiguous, the operator must specify --device explicitly.
    """
    if requested != "auto":
        node = Path(requested)
        if not node.exists():
            raise FileNotFoundError(
                f"Specified USBTMC device {node} does not exist. "
                "Use --device auto or list /dev/usbtmc*."
            )
        return node

    nodes = sorted(node for node in dev_root.glob("usbtmc*")
                   if re.fullmatch(r"usbtmc[0-9]+", node.name))
    if not nodes:
        raise FileNotFoundError(
            "No /dev/usbtmcN node found. Check the rear USB-B device cable, "
            "scope power, lsusb -d 0699:0450, and the usbtmc kernel driver."
        )
    if len(nodes) == 1:
        return nodes[0]

    matches = [node for node in nodes
               if _usb_vendor_product(node.name, sysfs_root)
               == (EXPECTED_VENDOR_ID, EXPECTED_PRODUCT_ID)]
    if len(matches) == 1:
        return matches[0]
    raise RuntimeError(
        "Ambiguous USBTMC instruments: "
        + ", ".join(str(node) for node in nodes)
        + ". Select the intended scope using --device /dev/usbtmcN; "
        "automatic selection is deliberately disabled for multiple matches."
    )


def verify_scope_identity(identity):
    """Ensure we are talking to the expected scope before writing settings."""
    parts = [part.strip().upper() for part in identity.split(",")]
    if len(parts) < 3 or (parts[0], parts[1]) != (
            EXPECTED_IDN_MANUFACTURER, EXPECTED_IDN_MODEL):
        raise RuntimeError(
            f"Unexpected USBTMC instrument {identity!r}; expected "
            f"{EXPECTED_IDN_MANUFACTURER},{EXPECTED_IDN_MODEL}. "
            "Refusing to configure an unrecognized device."
        )
    return identity.strip()
