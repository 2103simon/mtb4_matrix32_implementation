"""Read-only finite recorded MCU snapshot replay for isolated bench A/B tests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
from pathlib import Path
import time

from experiment_analysis.paths import raw_run_dir, sha256


@dataclass
class ReplayPlan:
    provenance: dict
    source_config: dict
    batches: list


def load_replay(source_run, *, duration_s=20.5, start_s=None, stale_s=.25):
    """Select a fixed host-arrival window; preserve equal-timestamp batches.

    Default start uses the prior record's estimated scope start, only for
    selecting input, not for claiming precise waveform alignment.
    """
    if not math.isfinite(duration_s) or not 0 < duration_s <= 24:
        raise ValueError("Replay must be finite, at most 24 seconds")
    if not math.isfinite(stale_s) or stale_s <= 0:
        raise ValueError("Invalid stale threshold")
    # Keep numerical/plotting imports off the ordinary live-controller startup path.
    from analyze_mcu_recording import choose_recording, load_recordings

    root = raw_run_dir(source_run)
    folder = root/"mcu_matrix32"
    if len(list(folder.glob("*_tactile_rates.csv"))) != 1:
        raise ValueError("Expected one unambiguous source rate recording")
    rid = choose_recording(folder)
    data = load_recordings(folder, rid)
    if data.get("mode") != "rates":
        raise ValueError("Replay requires recorded atomic rate snapshots")
    settings = data["experiment_settings"]
    if settings.get("mcu_configuration_confirmed", {}).get("acknowledged") is not True:
        raise ValueError("Source must be an original ACK-configured MCU recording")
    cfg = settings["configuration"]
    times = [int(t) for t in data["taxel_pc_ns"]]
    if not times or any(b < a for a, b in zip(times, times[1:])):
        raise ValueError("Source arrival times must be nondecreasing")
    if start_s is None:
        epoch = json.loads((root/"epoch_clock_analysis.json").read_text())
        first = epoch["records"][0]["first_sample_host_monotonic_ns_est"]
        start_ns = int(first)
        selection = "original_record_1_estimated_scope_start"
    else:
        if not math.isfinite(start_s) or start_s < 0:
            raise ValueError("Invalid replay offset")
        start_ns = times[0] + round(start_s*1e9)
        selection = "explicit_seconds_after_first_mcu_arrival"
    end_ns = start_ns + round(duration_s*1e9)
    if start_ns < times[0] or end_ns > times[-1]:
        raise ValueError("Replay window not covered by source telemetry")
    indices = [i for i,t in enumerate(times) if start_ns <= t < end_ns]
    if not indices:
        raise ValueError("Empty replay window")
    batches = []
    for i in indices:
        offset = times[i]-start_ns
        snap = {
            "raw_timestamp_ms": int(data["taxel_raw_timestamps"][i]),
            "corrected_timestamp_ms": float(data["taxel_corrected_timestamps"][i]),
            "taxels": data["taxel_data"][i].tolist(),
            "ra_rates_hz": data["ra_rate_hz"][i].tolist(),
            "sa_rates_hz": data["sa_rate_hz"][i].tolist(),
        }
        for key in ("taxels", "ra_rates_hz", "sa_rates_hz"):
            if len(snap[key]) != 12 or any(not math.isfinite(v) or v < 0 for v in snap[key]):
                raise ValueError("Invalid source snapshot")
        if not batches or batches[-1][0] != offset:
            batches.append((offset, []))
        batches[-1][1].append(snap)
    gaps = [batches[0][0], *(b[0]-a[0] for a,b in zip(batches,batches[1:])),
            end_ns-start_ns-batches[-1][0]]
    if max(gaps) >= round(stale_s*1e9):
        raise ValueError("Replay window contains a gap at or above stale limit")
    provenance = {
        "source_run": str(root), "source_recording_id": rid,
        "source_rates_sha256": sha256(folder/f"{rid}_tactile_rates.csv"),
        "source_settings_sha256": sha256(folder/f"{rid}_settings.json"),
        "selection": selection, "source_start_ns": start_ns,
        "source_end_ns_exclusive": end_ns, "start_s": (start_ns-times[0])/1e9,
        "duration_s": duration_s, "sample_count": len(indices),
        "batch_count": len(batches), "max_source_gap_ms": max(gaps)/1e6,
        "source_waveforms_read": False,
    }
    return ReplayPlan(provenance, cfg, batches)


class ReplaySnapshots:
    """Latest-slot interface consumed unchanged by the live frequency worker."""
    def __init__(self):
        self.latest = None
        self.running = True

    def is_running(self):
        return self.running

    def get_latest_rates_snapshot(self):
        return self.latest

    def publish(self, batch, arrival_ns, recorder=None):
        for source in batch:
            snapshot = {**source, "pc_arrival_monotonic_ns": arrival_ns}
            self.latest = snapshot
            if recorder is not None:
                recorder.record_tactile_rates(snapshot)


async def wait_until_ns(deadline_ns):
    while (remaining := deadline_ns-time.monotonic_ns()) > 0:
        await asyncio.sleep(remaining/1e9)


async def replay_batches(plan, snapshots, capture, *, start_ns, max_lateness_s=.05):
    """Reject excessive delivery lateness rather than silently replaying stale data.

    Actual PC arrival timestamps, not planned times, drive the watchdog and
    are saved in the new session. Equal source timestamps share one delivery.
    Short scheduling delays can compress adjacent deliveries; log every delay
    so A/B timing equivalence can be checked rather than assumed.
    """
    limit_ns = round(max_lateness_s*1e9)
    if limit_ns <= 0:
        raise ValueError("Invalid replay lateness limit")
    lateness = []
    for offset, batch in plan.batches:
        deadline = start_ns+offset
        await wait_until_ns(deadline)
        actual = time.monotonic_ns()
        delay = actual-deadline
        if not 0 <= delay <= limit_ns:
            raise TimeoutError("Replay delivery exceeded lateness limit")
        snapshots.publish(batch, actual, capture.recorder)
        lateness.append(delay/1e6)
        capture.emit("replay_batch", planned_ns=deadline, arrival_ns=actual,
                     source_offset_ns=offset, sample_count=len(batch),
                     lateness_ms=delay/1e6)
    await wait_until_ns(start_ns+round(plan.provenance["duration_s"]*1e9))
    capture.emit("replay_completed", batches=len(lateness),
                 max_lateness_ms=max(lateness), source_samples=plan.provenance["sample_count"])
