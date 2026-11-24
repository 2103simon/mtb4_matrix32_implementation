"""Draw provisional scope coverage on the full MCU trace and plot overlap only.

Offline-only; uses saved PC arrival timestamps and existing per-record
scope-to-host estimates. Never treats an unqualified clock map as calibrated
physical latency. No onset approval required; raw files stay untouched.

Usage:
    python -m oscilloscope.plot_mcu_scope_coverage --run-dir data/recordings/<run>
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from analyze_mcu_recording import load_recordings, plot_traces
from oscilloscope.analyze_integrated_matrix32 import tentative_host_map


def render(root: Path, *, taxel: int | None = None) -> list[Path]:
    root = Path(root)
    files = sorted((root / "mcu_matrix32").glob("*_tactile_rates.csv"))
    if len(files) != 1:
        raise ValueError(f"Expected one MCU rates CSV; found {len(files)}")
    recording_id = files[0].name.removesuffix("_tactile_rates.csv")
    mcu = load_recordings(root / "mcu_matrix32", recording_id)
    host_ns = mcu["taxel_pc_ns"]
    if len(host_ns) == 0:
        raise ValueError("Empty MCU arrival series")
    clock_path = root / "epoch_clock_analysis.json"
    if not clock_path.exists():
        print("[MCU PLOT] No clock mapping: scope-window plots skipped")
        return []
    import json
    clock = json.loads(clock_path.read_text(encoding="utf-8"))
    records = clock.get("records", [])
    windows = []
    for rec in records:
        idx = int(rec["record_index"])
        mapping = tentative_host_map(root, record_index=idx)
        if mapping is None:
            print(f"[MCU PLOT] Record {idx}: no valid host map; omitted")
            continue
        begin = mapping["first_sample_pc_monotonic_ns_est"]
        end = mapping["last_sample_pc_monotonic_ns_est"]
        if end <= begin:
            raise ValueError(f"Record {idx}: nonpositive estimated scope window")
        windows.append((idx, begin, end))
    if not windows:
        print("[MCU PLOT] No usable estimated scope windows; plots skipped")
        return []
    out = root / "analysis"
    out.mkdir(exist_ok=True)
    origin = int(host_ns[0])
    ms_windows = [((begin - origin) / 1e6, (end - origin) / 1e6)
                  for _, begin, end in windows]
    full = out / "mcu_full_stream_scope_windows.png"
    plot_traces(mcu, full, selected_taxel=taxel, x_axis="pc",
                scope_windows=ms_windows)
    output = [full]
    for idx, begin, end in windows:
        # This is a host-arrival overlap, not scope-domain electrical coverage.
        mask = (host_ns >= begin) & (host_ns <= end)
        if not np.any(mask):
            print(f"[MCU PLOT] Record {idx}: no MCU samples within estimated window")
            continue
        cropped = out / f"mcu_scope_overlap_record_{idx:05d}.png"
        interval = ((begin - origin) / 1e6, (end - origin) / 1e6)
        plot_traces(mcu, cropped, selected_taxel=taxel, x_axis="pc",
                    interval=interval, scope_windows=[interval])
        output.append(cropped)
    for path in output:
        print(f"[MCU PLOT] Saved {path} (scope boundaries provisional)")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--taxel", type=int, choices=range(12), default=None)
    args = parser.parse_args(argv)
    return render(args.run_dir, taxel=args.taxel)


if __name__ == "__main__":
    main()
