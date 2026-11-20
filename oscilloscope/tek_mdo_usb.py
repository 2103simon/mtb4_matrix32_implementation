"""Tektronix MDO4104B-6 USBTMC probe and single-waveform capture.

Uses the Linux kernel usbtmc character device; no PyVISA needed.
Run as root for the initial test if /dev/usbtmc* is root-only.
"""
import argparse
import csv
import math
import os
import struct
import tempfile
from pathlib import Path

def read_line(f):
    # USBTMC reads may return a partial text response; accumulate through newline.
    chunks = []
    while True:
        chunk = f.read(1)
        if not chunk:
            raise EOFError("No complete SCPI text response")
        chunks.append(chunk)
        if chunk == b"\n":
            return b"".join(chunks).decode("ascii", errors="replace").strip()

def query(f, cmd):
    f.write((cmd + "\n").encode("ascii"))
    return read_line(f)

def write(f, cmd):
    f.write((cmd + "\n").encode("ascii"))

def read_exact(f, size):
    """Read an exact byte count even when USBTMC returns short chunks."""
    chunks = []
    remaining = size
    while remaining:
        chunk = f.read(remaining)
        if not chunk:
            raise EOFError(f"Incomplete USBTMC response ({remaining} bytes missing)")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def binary_block(f, *, expected_bytes=None):
    """Read one IEEE 488.2 definite-length CURVE? block.

    Verify the declared payload length *before* reading a potentially
    inconsistent transfer. This cannot detect equal-length corrupt content.
    """
    write(f, "CURVE?")
    marker = read_exact(f, 1)
    if marker != b"#":
        raise RuntimeError(f"Expected binary block (#), got {marker!r}")
    nd_token = read_exact(f, 1)
    if nd_token not in tuple(bytes([i]) for i in range(ord("1"), ord("9") + 1)):
        raise RuntimeError(f"Invalid/indefinite binary block length digit: {nd_token!r}")
    nd = int(nd_token)
    length_bytes = read_exact(f, nd)
    if not length_bytes.isdigit():
        raise RuntimeError(f"Invalid binary block length: {length_bytes!r}")
    n = int(length_bytes)
    if n > 100_000_000:
        raise RuntimeError(f"Unreasonable waveform payload size: {n}")
    if expected_bytes is not None and n != expected_bytes:
        raise RuntimeError(
            f"Binary block declares {n} bytes; expected {expected_bytes} "
            "from requested points and waveform byte width"
        )
    return read_exact(f, n)


def capture(f, channel, points, output):
    """Download, validate and atomically publish one scope waveform CSV.

    Returns the preamble and decoded transfer metadata for the S00 run log.
    """
    if points < 1:
        raise ValueError("points must be positive")
    write(f, "HEADER OFF")
    write(f, f"DATA:SOURCE CH{channel}")
    write(f, "DATA:ENCdg RIBinary")
    write(f, "DATA:WIDth 2")
    write(f, "DATA:START 1")
    write(f, f"DATA:STOP {points}")

    def number(cmd):
        return float(query(f, cmd))

    xincr = number("WFMPRE:XINcr?")
    xzero = number("WFMPRE:XZEro?")
    ptoff = number("WFMPRE:PT_Off?")
    ymult = number("WFMPRE:YMUlt?")
    yoff = number("WFMPRE:YOFf?")
    yzero = number("WFMPRE:YZEro?")
    width_number = number("WFMPRE:BYT_Nr?")
    order = query(f, "WFMPRE:BYT_Or?").strip().upper()
    fmt = query(f, "WFMPRE:BN_Fmt?").strip().upper()

    if not all(math.isfinite(v) for v in
               (xincr, xzero, ptoff, ymult, yoff, yzero, width_number)):
        raise RuntimeError("Non-finite WFMPRE scaling or byte width")
    if xincr <= 0 or ymult == 0:
        raise RuntimeError("Invalid WFMPRE time or voltage increment")
    if width_number not in (1, 2):
        raise RuntimeError(f"Unsupported byte width: {width_number}")
    width = int(width_number)
    if not fmt.startswith("RI"):
        raise RuntimeError(f"Expected signed integer samples, got {fmt!r}")
    if order.startswith("MSB"):
        endian = ">"
    elif order.startswith("LSB"):
        endian = "<"
    else:
        raise RuntimeError(f"Unrecognized waveform byte order: {order!r}")

    expected_bytes = points * width
    payload = binary_block(f, expected_bytes=expected_bytes)
    raw = struct.unpack(f"{endian}{points}{'h' if width == 2 else 'b'}", payload)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # No partial or incorrectly decoded CSV can masquerade as a valid capture.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False
        ) as fp:
            temp_path = Path(fp.name)
            writer = csv.writer(fp)
            writer.writerow(["time_s", "voltage_v", "raw"])
            for i, value in enumerate(raw):
                writer.writerow([xzero + (i - ptoff) * xincr,
                                 (value - yoff) * ymult + yzero, value])
        os.replace(temp_path, output)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    rail = 32767 if width == 2 else 127
    lower_rail = -rail - 1
    report = {
        "waveform_preamble": {
            "xincr_s": xincr, "xzero_s": xzero, "ptoff": ptoff,
            "ymult_v_per_code": ymult, "yoff_codes": yoff,
            "yzero_v": yzero, "bytes_per_sample": width,
            "byte_order": order, "binary_format": fmt,
        },
        "binary_transfer": {
            "requested_samples": points, "decoded_samples": len(raw),
            "declared_payload_bytes": len(payload),
            "expected_payload_bytes": expected_bytes,
            "raw_min": min(raw), "raw_max": max(raw),
            "storage_rail_code_count": sum(
                code in (lower_rail, rail) for code in raw
            ),
        },
    }
    print(f"Received {len(raw)} points; saved {output}")
    print(f"Raw sample range: {min(raw)} .. {max(raw)}")
    return report

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default="/dev/usbtmc3", help="Linux USBTMC device path")
    p.add_argument("--capture", type=int, choices=range(1, 5), metavar="CH")
    p.add_argument("--points", type=int, default=1000)
    p.add_argument("--output", default="data/tek_usb_ch1.csv")
    a = p.parse_args()
    if a.points < 1:
        p.error("--points must be positive")
    print(f"Opening {a.device}")
    with open(a.device, "r+b", buffering=0) as f:
        identity = query(f, "*IDN?")
        print("Identity:", identity)
        if "TEKTRONIX" not in identity.upper():
            raise RuntimeError("Unexpected instrument identity; refusing capture")
        if a.capture:
            print("Capturing current displayed/acquired waveform (not starting stimulation)")
            capture(f, a.capture, a.points, a.output)

if __name__ == "__main__":
    main()
