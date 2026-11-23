"""Full S01/S02-style event inventory and provisional MCU/ACK annotations.

This offline report never modifies the source-locked review decisions.
"""
from __future__ import annotations
import bisect
import csv
import json
from pathlib import Path
from oscilloscope.analyze_integrated_matrix32 import tentative_host_map

FIELDS = ("id", "kind", "time_s", "sample_index", "voltage_v",
          "detector_status", "flag", "decision", "note",
          "preceding_period_ms", "provisional_inverse_period_hz",
          "taxel_value", "mcu_requested_hz", "latest_sf_ack_hz",
          "latest_sc_ack", "clock_context_status", "raw_figure")


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def fmt(value, digits=2):
    try:
        return f"{float(value):.{digits}f}" if value is not None else "-"
    except (ValueError, TypeError):
        return "-"


def md(value):
    return str(value if value not in (None, "") else "-").replace("|", "/").replace("\n", " ")


def context(root, record):
    root = Path(root)
    mapping = tentative_host_map(root, int(record["index"]))
    directory = root / "mcu_matrix32"
    paths = list(directory.glob("*_tactile_rates.csv"))
    configs = list(directory.glob("*_settings.json"))
    source = {}
    if len(configs) == 1:
        settings = json.loads(configs[0].read_text(encoding="utf-8"))
        source = (settings.get("configuration") or {}).get("stimulation_source") or {}
    taxel = source.get("taxel", 11)
    encoding = source.get("encoding", "sa_ii")
    info = {"mapping": mapping, "taxel": taxel, "encoding": encoding,
            "times": [], "rows": [], "events": [], "event_times": []}
    if (mapping is None or len(paths) != 1
            or type(taxel) is not int or taxel not in range(12)
            or encoding not in ("ra_i", "sa_ii")):
        return info
    rate_key = ("sa_rate_hz_" if encoding == "sa_ii" else "ra_rate_hz_") + str(taxel)
    pressure_key = "taxel_" + str(taxel)
    h0 = mapping["first_sample_pc_monotonic_ns_est"]
    h1 = mapping["last_sample_pc_monotonic_ns_est"]
    with paths[0].open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            ns = int(row["pc_arrival_monotonic_ns"])
            if h0 - 250000000 <= ns <= h1:
                info["rows"].append((ns, float(row[pressure_key]), float(row[rate_key])))
    info["rows"].sort()
    info["times"] = [row[0] for row in info["rows"]]
    trace = list(directory.glob("*_matrix32_commands.jsonl"))
    if len(trace) == 1:
        with trace[0].open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                ns = event.get("pc_monotonic_ns")
                if (ns is not None and int(ns) <= h1
                        and event.get("event") in ("sf_ack", "sc_ack")):
                    info["events"].append(event)
        info["events"].sort(key=lambda e: int(e["pc_monotonic_ns"]))
        info["event_times"] = [int(e["pc_monotonic_ns"]) for e in info["events"]]
    return info


def associate(info, time_s, start, finish):
    mapping = info["mapping"]
    if mapping is None or finish <= start:
        return (None, None, None, None, "NO_CLOCK_MAP")
    h0 = mapping["first_sample_pc_monotonic_ns_est"]
    h1 = mapping["last_sample_pc_monotonic_ns_est"]
    ns = int(h0 + (time_s-start)/(finish-start)*(h1-h0))
    idx = bisect.bisect_right(info["times"], ns)-1
    sample = info["rows"][idx] if idx >= 0 else None
    if sample is not None and not 0 <= ns-sample[0] <= 250000000:
        sample = None
    idx = bisect.bisect_right(info["event_times"], ns)-1
    # Precomputed latest-known ACK state avoids rescanning long BLE logs
    # once per physical onset (which would be quadratic for 5M-point runs).
    sf, sc = None, None
    if idx >= 0:
        if "sf_at_event" not in info:
            last_sf, last_sc = None, None
            info["sf_at_event"], info["sc_at_event"] = [], []
            for event in info["events"]:
                if event.get("status") == "OK":
                    if event.get("event") == "sf_ack":
                        last_sf = event.get("frequency_hz")
                    elif event.get("event") == "sc_ack":
                        last_sc = event.get("current_command")
                info["sf_at_event"].append(last_sf)
                info["sc_at_event"].append(last_sc)
        sf, sc = info["sf_at_event"][idx], info["sc_at_event"][idx]
    status = "PROVISIONAL_" + str(mapping.get("status", "UNKNOWN"))
    if sample is None:
        status += "_NO_FRESH_MCU_SAMPLE"
    return (sample[1] if sample else None, sample[2] if sample else None,
            sf, sc, status)


def generate(root, record, result, output, manifest):
    """Write an untruncated per-event Markdown table and a matching CSV."""
    root, output = Path(root), Path(output)
    info = context(root, record)
    existing = {r["id"]: r for r in read_csv(output / "review_decisions.csv")}
    previous = {}
    for a, b in zip(result["accepted_pulses"], result["accepted_pulses"][1:]):
        elapsed = float(b["time_s"])-float(a["time_s"])
        previous[b["id"]] = (elapsed*1000, 1/elapsed if elapsed > 0 else None)
    rows = []
    for kind, prefix, key in (
            ("accepted", "P", "accepted_pulses"),
            ("rejected", "R", "rejected"),
            ("suppressed", "S", "suppressed")):
        for n, event in enumerate(result[key], 1):
            eid = f"{prefix}{n:05d}"
            timestamp = float(event["time_s"])
            pressure, rate, sf, sc, quality = associate(
                info, timestamp, float(result["capture_start_s"]),
                float(result["capture_end_s"]))
            flag = ("None" if kind == "accepted" and
                    event["status"] == "CANDIDATE_REVIEW" else event["status"])
            if kind == "accepted" and flag != "None":
                fig = f"figures/flagged_accepted/{eid}_raw.png"
            elif kind in ("rejected", "suppressed"):
                fig = f"figures/rejected/{eid}_t{timestamp*1000:010.4f}ms.png"
            else:
                fig = ""
            interval, inverse = previous.get(eid, (None, None))
            decision = existing.get(eid, {})
            rows.append({
                "id": eid, "kind": kind, "time_s": timestamp,
                "sample_index": int(event["sample_index"]),
                "voltage_v": event["voltage_v"],
                "detector_status": event["status"], "flag": flag,
                "decision": decision.get("decision", ""),
                "note": decision.get("note", ""),
                "preceding_period_ms": interval,
                "provisional_inverse_period_hz": inverse,
                "taxel_value": pressure, "mcu_requested_hz": rate,
                "latest_sf_ack_hz": sf, "latest_sc_ack": sc,
                "clock_context_status": quality, "raw_figure": fig,
            })
    rows.sort(key=lambda r: (r["time_s"], r["sample_index"], r["id"]))
    with (output / "pulse_inventory_context.csv").open(
            "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    meta = {"schema": "matrix32_event_review_context_v1",
            "record_index": int(record["index"]),
            "source_ch1_sha256": manifest["source_ch1_sha256"],
            "selected_taxel": info["taxel"], "encoding": info["encoding"],
            "clock_status": (info["mapping"] or {}).get("status", "UNAVAILABLE"),
            "total_events": len(rows),
            "flagged_events": sum(r["flag"] != "None" for r in rows)}
    (output / "pulse_inventory_context.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    lines = [
        "## Complete S01/S02-style event review table", "",
        "Every accepted (P), rejected (R) and suppressed (S) event is listed; "
        "no first-N truncation. Download the matching "
        "[complete per-event CSV](pulse_inventory_context.csv) for filtering.",
        "",
        "Flag **None** means no special automatic detector flag for that "
        "accepted onset. It does NOT mean operator-approved. Review the "
        "entire raw CH1 trace and decide all flagged events individually. "
        "Accepted unflagged P rows may be approved collectively at signoff.",
        "",
        f"MCU selected taxel: {info['taxel']}; encoding: {info['encoding']}; "
        f"clock: {meta['clock_status']}. MCU/ACK event associations are "
        "provisional and must not be interpreted as physical apply timestamps.",
        "",
        "| ID | Kind | Onset ms | CH1 V | Flag | Previous P period ms | "
        "Taxel* | Requested Hz* | SF ACK Hz* | SC ACK* | Decision | Raw zoom |",
        "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        fig = row["raw_figure"]
        link = f"[zoom]({fig})" if fig and (output / fig).exists() else "-"
        lines.append(
            f"| {row['id']} | {row['kind']} | {row['time_s']*1000:.3f} | "
            f"{fmt(row['voltage_v'])} | {md(row['flag'])} | "
            f"{fmt(row['preceding_period_ms'], 3)} | "
            f"{fmt(row['taxel_value'], 1)} | {fmt(row['mcu_requested_hz'])} | "
            f"{fmt(row['latest_sf_ack_hz'], 0)} | {fmt(row['latest_sc_ack'], 0)} | "
            f"{md(row['decision'] or 'PENDING')} | {link} |"
        )
    lines += [
        "",
        "*All MCU and ACK context is based on provisional scope-to-host clock "
        "mapping. See clock_context_status in the CSV; blank values mean "
        "unknown or no sufficiently recent input. The full waveform figure "
        "is a min/max envelope; flagged details use original samples.",
        "",
        "Edit only [review_decisions.csv](review_decisions.csv), not the "
        "derived table. Threshold-sensitive Q candidates and optional "
        "manual M additions retain their separate review workflow.",
    ]
    return lines
