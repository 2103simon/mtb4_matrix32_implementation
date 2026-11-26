"""Offline diagnostic of SC0 / SF restore / SC restore and first biphasic cycles.

The first-pulse candidates are conditional on the existing write-to-ACK
availability model. SC availability is not verified electrical current
application, and a pulse in that window could be residual output. No clock
parameters or tolerances are fitted to the waveform.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np

from experiment_analysis.audit_refined_ack_observability import (
    load_refined_evidence, map_refined_evidence,
)
from experiment_analysis.audit_cycle_boundary_observability import (
    prepare_commands, analyze_cycles, classify_commands, boundary_states,
    compatible, candidate_ids, MAX_REGULAR_PERIOD_S,
)
from experiment_analysis.paths import ensure_derived_destination, sha256
from experiment_analysis.offset_robust_command_matching import _csv
from oscilloscope.record_scope import atomic_json


def restart_sequences(model):
    """Use the shared prefix states to associate actual SC restores with SF IDs."""
    sequences = []
    mute = None
    for i,tr in enumerate(model["commands"]):
        if tr["family"] != "sc":
            continue
        before = model["states"][i]
        after = model["states"][i+1]
        if before[2] and not after[2]:
            mute = i
        elif not before[2] and after[2]:
            if mute is None:
                raise ValueError("SC restoration has no preceding SC0")
            later_sc = next((j for j in range(i+1,len(model["commands"]))
                             if model["commands"][j]["family"]=="sc"),None)
            scheduler_ids = [j for j in range(mute+1,i)
                             if j in model["sf_by_id"]
                             and model["sf_by_id"][j]["mode"]=="mute_scheduler"]
            restore_ids = [j for j in range(mute+1,i)
                           if j in model["sf_by_id"]
                           and model["sf_by_id"][j]["mode"]=="restore"]
            sequences.append({"mute_id":mute,"restore_sc_id":i,"sf_id":after[0],
                              "frequency_hz":after[1],"next_sc_id":later_sc,
                              "scheduler_ids":scheduler_ids,"restore_attempt_ids":restore_ids})
            mute = None
    return sequences


def first_pulse_indices(mapped, write, ack, end):
    """Possible first observed pulse after restore availability, not physical onset proof.

    Include onsets intersecting the write-to-ACK window and the first onset
    definitely after ACK. A later onset cannot be the first under this model
    if a preceding onset is definitely after ACK. The active window is capped
    at the next SC write or STOP, not extended to its ACK.
    """
    result = []
    for i,(lo,hi,valid) in enumerate(zip(mapped["lower"],mapped["upper"],mapped["valid"])):
        if not valid or not (np.isfinite(lo) and np.isfinite(hi)):
            continue
        if hi < write:
            continue
        if lo >= end:
            break
        result.append(i)
        if lo >= ack:
            break
    return result


def analyze_restart(model,sequence,times,ids,mapped,support,cycles):
    commands = model["commands"]
    mute = commands[sequence["mute_id"]]
    restore = commands[sequence["restore_sc_id"]]
    write,ack = restore["write_begin_ns"],restore["ack_ns"]
    _,support_start,support_end,_ = support
    next_sc = sequence["next_sc_id"]
    end = min(support_end,model["stop_ns"],
              commands[next_sc]["write_begin_ns"] if next_sc is not None else float("inf"))
    owner = sequence["sf_id"]
    sf = model["sf_by_id"].get(owner)
    last_scheduler = (commands[sequence["scheduler_ids"][-1]]
                      if sequence["scheduler_ids"] else None)
    row = {
        "restore_sc_id":sequence["restore_sc_id"],"mute_sc_id":sequence["mute_id"],
        "mute_write_ns":mute["write_begin_ns"],"mute_ack_ns":mute["ack_ns"],
        "last_scheduler_sf_id":sequence["scheduler_ids"][-1] if sequence["scheduler_ids"] else None,
        "last_scheduler_sf_ack_ns":last_scheduler["ack_ns"] if last_scheduler else None,
        "restore_sf_id":owner,"restore_frequency_hz":sequence["frequency_hz"],
        "restore_sf_ack_ns":sf["ack_ns"] if sf else None,
        "restore_sf_mode":sf["mode"] if sf else "initial",
        "restore_attempt_ids":";".join(map(str,sequence["restore_attempt_ids"])),
        "restore_sc_write_ns":write,"restore_sc_ack_ns":ack,
        "next_sc_id":next_sc,"active_window_end_ns":end,
        "sf_ack_to_sc_ack_ms":(ack-sf["ack_ns"])/1e6 if sf else None,
        "physical_application_proven":False,
    }
    detail = []
    # If the restore itself is not fully bracketed, do not claim first-pulse
    # coverage or infer a missing cycle from the edge of a recording.
    if not (support_start<=write<=ack<end):
        return {**row,"classification":"RESTORE_OUTSIDE_COMPLETE_TIMING_SUPPORT",
                "first_pulse_candidates":0,"assessed_first_cycles":0,
                "restore_compatible_first_cycles":0,"newer_compatible_first_cycles":0,
                "newly_recovered_sc_overlap_cycles":0},detail
    indices = first_pulse_indices(mapped,write,ack,end)
    lo,hi = mapped["lower"],mapped["upper"]
    for i in indices:
        states = boundary_states(model,lo[i],hi[i])
        # Condition on SC restoration being available at this candidate
        # onset. A muted prefix is not an active-frequency witness.
        active = [(o,hz) for o,hz,on in states if on]
        matches = []
        measured = None
        original = cycles[i]["classification"] if i<len(cycles) else None
        if hi[i]>=end:
            status = "FIRST_PULSE_AT_NEXT_CONTROL_OR_SUPPORT_EDGE"
        elif not active:
            status = "NO_ACTIVE_PREFIX_AT_FIRST_PULSE"
        elif i+1>=len(times) or not mapped["interval_valid"][i] or not np.isfinite(hi[i+1]):
            status = "SECOND_PULSE_OUTSIDE_SUPPORT"
        elif hi[i+1]>=end:
            status = "SECOND_PULSE_AT_NEXT_CONTROL_OR_SUPPORT_EDGE"
        elif times[i+1]-times[i]>MAX_REGULAR_PERIOD_S:
            status = "LONG_GAP_NOT_CYCLE"
        else:
            measured = 1/float(times[i+1]-times[i])
            matches = [(o,hz) for o,hz in active if compatible(measured,hz)]
            status = "MATCHING_FIRST_CYCLE" if matches else "NO_MATCHING_FIRST_CYCLE"
        restored_match = any(o==owner for o,_ in matches)
        newer_match = any(o is not None and (owner is None or o>owner) for o,_ in matches)
        restored_possible = any(o==owner for o,_ in active)
        # Restore overlap is relaxed ONLY for this restart. The end cap
        # excludes the next SC, and the first onset must be within support.
        detail.append({
            "restore_sc_id":sequence["restore_sc_id"],"restore_sf_id":owner,
            "first_onset_id":ids[i],"first_scope_s":float(times[i]),
            "second_onset_id":ids[i+1] if i+1<len(ids) else None,
            "first_after_ack_lower_ms":float(lo[i]-ack)/1e6,
            "first_after_ack_upper_ms":float(hi[i]-ack)/1e6,
            "first_after_write_lower_ms":float(lo[i]-write)/1e6,
            "first_after_write_upper_ms":float(hi[i]-write)/1e6,
            "second_after_ack_lower_ms":float(lo[i+1]-ack)/1e6 if i+1<len(ids) and np.isfinite(lo[i+1]) else None,
            "second_after_ack_upper_ms":float(hi[i+1]-ack)/1e6 if i+1<len(ids) and np.isfinite(hi[i+1]) else None,
            "first_overlaps_restore_transaction":bool(lo[i]<=ack and hi[i]>=write),
            "first_definitely_after_ack":bool(lo[i]>=ack),
            "period_ms":float((times[i+1]-times[i])*1000) if measured is not None else None,
            "inverse_period_hz":measured,
            "active_candidate_ids":";".join("initial" if o is None else str(o) for o,_ in active),
            "matching_candidate_ids":";".join("initial" if o is None else str(o) for o,_ in matches),
            "active_candidate_hz":";".join(str(hz) for _,hz in active),
            "matching_candidate_hz":";".join(str(hz) for _,hz in matches),
            "restore_sf_candidate":restored_possible,"restore_sf_matches":restored_match,
            "newer_sf_matches":newer_match,"classification":status,
            "original_cycle_classification":original,
            "recovered_restore_sc_overlap":bool(restored_match and original=="SC_TRANSITION"),
            "physical_application_proven":False,
        })
    assessed = [r for r in detail if r["inverse_period_hz"] is not None]
    restored = [r for r in assessed if r["restore_sf_matches"]]
    newer = [r for r in assessed if r["newer_sf_matches"]]
    all_assessed = bool(detail) and len(assessed)==len(detail)
    if not detail:
        classification = "NO_SUPPORTED_PULSE_BEFORE_NEXT_CONTROL"
    elif not assessed:
        classification = "FIRST_CYCLE_UNRESOLVED"
    elif all_assessed and all(r["restore_sf_matches"] and
                              candidate_ids(r["active_candidate_ids"])=={owner if owner is not None else -1}
                              for r in assessed):
        classification = "RESTORE_MATCH_ALL_FIRST_CYCLE_CANDIDATES"
    elif restored:
        classification = "RESTORE_MATCH_POSSIBLE"
    elif all_assessed and all(r["newer_sf_matches"] and not r["restore_sf_candidate"]
                              for r in assessed):
        classification = "NEWER_SF_ONLY_AT_ALL_FIRST_CYCLE_CANDIDATES"
    elif newer:
        classification = "NEWER_SF_MATCH_POSSIBLE"
    elif all_assessed:
        classification = "NO_COMPATIBLE_FIRST_CYCLE"
    else:
        classification = "FIRST_CYCLE_PARTIALLY_ASSESSED"
    return {**row,"classification":classification,"first_pulse_candidates":len(detail),
            "assessed_first_cycles":len(assessed),"restore_compatible_first_cycles":len(restored),
            "newer_compatible_first_cycles":len(newer),
            "newly_recovered_sc_overlap_cycles":sum(r["recovered_restore_sc_overlap"] for r in detail)},detail


def classify_restore_attempts(model,commands,restarts):
    """Inventory every restore SF, including attempts cancelled while muted."""
    by_owner = {r["restore_sf_id"]:r for r in restarts}
    rows = []
    for row in commands:
        if row["purpose"]!="restore":
            continue
        owner = row["sf_id"]
        associated = by_owner.get(owner)
        successor = row["successor_sf_id"]
        next_sf = model["sf_by_id"].get(successor)
        # A newer SF replacing the pending restore before any actual SC
        # restoration is an acknowledged sequence fact, not electrical proof.
        next_sc_restore = next((i for i in range(owner+1,len(model["commands"]))
                                if model["commands"][i]["family"]=="sc"
                                and not model["states"][i][2] and model["states"][i+1][2]),None)
        prior_match = row["possible_matching_cycles"]>0
        first_match = bool(associated and associated["restore_compatible_first_cycles"])
        recovered = bool(associated and associated["newly_recovered_sc_overlap_cycles"])
        if associated is not None:
            reason = associated["classification"]
        elif next_sf is not None and (next_sc_restore is None or successor<next_sc_restore):
            reason = "REPLACED_BEFORE_SC_RESTORE"
        else:
            reason = "NO_ASSOCIATED_SC_RESTORE"
        rows.append({**row,"restart_reason":reason,
                     "associated_restore_sc_id":associated["restore_sc_id"] if associated else None,
                     "next_sf_mode":next_sf["mode"] if next_sf else None,
                     "next_sf_hz":next_sf["requested_sf_hz"] if next_sf else None,
                     "prior_cycle_match":prior_match,"possible_first_cycle_match":first_match,
                     "recovered_sc_overlap":recovered,
                     "any_local_matching_evidence":prior_match or first_match,
                     "new_local_matching_evidence":first_match and not prior_match})
    return rows


def summarize(restarts,detail,attempts):
    inside = [r for r in attempts if r["scope_membership"]=="inside"]
    return {
        "all_sc_restorations":len(restarts),
        "sc_restore_classifications":dict(Counter(r["classification"] for r in restarts)),
        "first_pulse_candidate_rows":len(detail),
        "first_cycle_assessed_rows":sum(r["inverse_period_hz"] is not None for r in detail),
        "all_restore_sf_acks":len(attempts),"restore_sf_acks_inside":len(inside),
        "inside_prior_matching":sum(r["prior_cycle_match"] for r in inside),
        "inside_possible_first_matching":sum(r["possible_first_cycle_match"] for r in inside),
        "inside_new_local_matching":sum(r["new_local_matching_evidence"] for r in inside),
        "inside_union_matching":sum(r["any_local_matching_evidence"] for r in inside),
        "inside_recovered_sc_overlap":sum(r["recovered_sc_overlap"] for r in inside),
        "inside_attempt_reasons":dict(Counter(r["restart_reason"] for r in inside)),
        "inside_unmatched_reasons":dict(Counter(r["restart_reason"] for r in inside
                                               if not r["any_local_matching_evidence"])),
    }


def run(run_dir):
    ctx = load_refined_evidence(run_dir)
    root = ctx["root"]
    dest = ensure_derived_destination(ctx["analysis"]/"restart_observability",root)
    if dest.exists():
        raise FileExistsError(dest)
    model = prepare_commands(ctx["events"])
    sequences = restart_sequences(model)
    times = ctx["times"]
    ids = [r["id"] for r in ctx["onsets"]]
    scenarios,outputs = {},{}
    for scale in (1.,2.,3.):
        mapped,support,clock = map_refined_evidence(ctx,scale)
        cycles,candidates = analyze_cycles(model,times,ids,mapped,support)
        commands = classify_commands(model,cycles,candidates,times,mapped,support)
        restarts,detail = [],[]
        for sequence in sequences:
            row,items = analyze_restart(model,sequence,times,ids,mapped,support,cycles)
            restarts.append(row)
            detail.extend(items)
        attempts = classify_restore_attempts(model,commands,restarts)
        scenarios[str(scale)] = {**summarize(restarts,detail,attempts),"clock":clock}
        outputs[str(scale)] = (restarts,detail,attempts)
    dest.mkdir(parents=True,exist_ok=False)
    for scale,items in outputs.items():
        for name,rows in zip(("sc_restarts","first_cycle_candidates","restore_sf_attempts"),items):
            if rows:
                # Edge-censored rows and assessed rows share the same schema.
                fields = list(dict.fromkeys(k for row in rows for k in row))
                _csv(dest/f"{name}_scale_{scale}.csv",rows,fields)
    atomic_json(dest/"summary.json",{
        "schema":"matrix32_restart_observability_v1","run":root.name,
        "source_sha256":ctx["hashes"],"trace_sha256":sha256(ctx["trace"]),
        "refinement_sha256":sha256(ctx["refinement_path"]),
        "approved_onsets_sha256":ctx["approval"]["approved_onsets_sha256"],
        "absolute_alignment_calibrated":False,"physical_application_proven":False,
        "availability_assumption":"host BLE write begin to embedded ACK",
        "scenarios":scenarios})
    lines = ["# Exploratory mute/resume observability","",f"Run: {root.name}","",
             "All SC restores and restore SF attempts are inventoried separately.",
             "First observed pulse candidates include restore write-to-ACK overlaps",
             "and the first definitely post-ACK onset, conditional on availability.",
             "The first following period may overlap that SC restore, but not a",
             "subsequent SC write, STOP, unsupported boundary or long pause.",
             "Residual pulses or delayed electrical current application are not ruled out.",
             "A matching candidate is not a proven restart or a unique execution.",
             "Local union counts are not new chronological distinct-ACK bounds.","",
             "| Gray scale | SC restores | Inside restore SF ACKs | Previous local matches | Newly matched | Local union |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for scale,r in scenarios.items():
        lines.append(f"| {scale} | {r['all_sc_restorations']} | {r['restore_sf_acks_inside']} | {r['inside_prior_matching']} | {r['inside_new_local_matching']} | {r['inside_union_matching']} |")
    lines += ["","## Unmatched restore SF attempts (1x)",""]
    for reason,count in sorted(scenarios["1.0"]["inside_unmatched_reasons"].items()):
        lines.append(f"- {reason}: {count}")
    (dest/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(f"[RESTART OBSERVABILITY] {dest/'report.md'}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir",required=True,type=Path)
    run(p.parse_args(argv).run_dir)


if __name__=="__main__":
    main()
