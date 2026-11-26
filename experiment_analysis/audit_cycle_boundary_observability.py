"""Test the cycle-boundary/latest-pending-SF model against signed physical pulses.

Availability of a command to the scheduler is modeled between host BLE write
begin and receipt of its application ACK. This is a hypothesis, NOT a proven
internal apply-time bound. Clock envelopes are independent of frequency match.
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np

from experiment_analysis.audit_refined_ack_observability import (
    load_refined_evidence, map_refined_evidence,
)
from experiment_analysis.audit_ack_physical_observability import (
    MATCH_ABSOLUTE_HZ, MATCH_RELATIVE_FRACTION, MAX_REGULAR_PERIOD_S,
)
from experiment_analysis.analyze_command_chain import transaction_rows
from experiment_analysis.paths import ensure_derived_destination, sha256
from experiment_analysis.offset_robust_command_matching import _csv
from oscilloscope.record_scope import atomic_json


def prepare_commands(events):
    """Build ordered prefix states, retaining individual SF transaction identities."""
    ordered = sorted(events,key=lambda e:int(e["pc_monotonic_ns"]))
    transactions, issues = transaction_rows(ordered, [], [])
    if issues:
        raise ValueError(f"Ambiguous transactions: {issues}")
    starts = [e for e in ordered if e["event"]=="start_write_completed"]
    if len(starts)!=1:
        raise ValueError("Expected exactly one successful START")
    if any(e["event"] in ("sf_failed","sc_failed","configuration_failed",
                          "start_failed","stop_failed","stop_unavailable",
                          "shutdown_stop_failed","ble_lost") for e in ordered):
        raise ValueError("Control failure: separate investigation required")
    start = int(starts[0]["pc_monotonic_ns"])
    hz = starts[0].get("frequency_hz")
    if type(hz) is not int or not 1<=hz<=400:
        raise ValueError("Invalid START frequency")
    stop_events = [int(e["pc_monotonic_ns"]) for e in ordered
                   if e["event"] in ("stop_requested","shutdown_stop_requested",
                                     "stop_write_completed","shutdown_stop_write_completed")
                   and int(e["pc_monotonic_ns"])>=start]
    stop = min(stop_events,default=float("inf"))
    commands = sorted([r for r in transactions if r["family"] in ("sf","sc")],
                      key=lambda r:r["request_ns"])
    states = [(None,hz,True)]
    last_ack = start
    sf_by_id = {}
    for i,r in enumerate(commands):
        if not r["success"] or not r["embedded_timing_valid"]:
            raise ValueError("Missing or invalid embedded SF/SC timing")
        if r["write_begin_ns"]<last_ack or r["request_ns"]<start:
            raise ValueError("Overlapping or pre-START SF/SC transaction")
        last_ack = r["ack_ns"]
        owner, freq, active = states[-1]
        if r["family"]=="sf":
            freq = r["requested_sf_hz"]
            if type(freq) is not int or not 1<=freq<=400:
                raise ValueError("Invalid SF")
            owner = i
            sf_by_id[i] = r
        else:
            current = r["requested_sc"]
            if type(current) is not int or current<0:
                raise ValueError("Invalid SC")
            active = current>0
        states.append((owner,freq,active))
    return {"commands":commands,"states":states,"sf_by_id":sf_by_id,
            "start_ns":start,"stop_ns":stop,
            "writes":np.asarray([r["write_begin_ns"] for r in commands],dtype=np.int64),
            "acks":np.asarray([r["ack_ns"] for r in commands],dtype=np.int64)}


def boundary_states(model, lower, upper):
    """All serial command prefixes possible under the tested availability model."""
    if not (np.isfinite(lower) and np.isfinite(upper) and lower<=upper):
        return []
    if lower<model["start_ns"] or upper>=model["stop_ns"]:
        return []
    first = int(np.searchsorted(model["acks"],lower,side="left"))
    last = int(np.searchsorted(model["writes"],upper,side="right"))
    if first>last:
        raise ValueError("Inconsistent command order")
    return list(dict.fromkeys(model["states"][first:last+1]))


def sc_overlap(model, start, end):
    return any(r["family"]=="sc" and r["write_begin_ns"]<=end
               and r["ack_ns"]>=start for r in model["commands"])


def compatible(measured, hz):
    return abs(measured-hz)<=max(MATCH_ABSOLUTE_HZ,MATCH_RELATIVE_FRACTION*hz)


def analyze_cycles(model,times,ids,mapped,support):
    lo,hi = mapped["lower"],mapped["upper"]
    _,support_start,support_end,_ = support
    candidates = [boundary_states(model,a,b) if v and a>=support_start and b<=support_end else []
                  for a,b,v in zip(lo,hi,mapped["valid"])]
    rows = []
    for i,dt in enumerate(np.diff(times)):
        states = candidates[i]
        active = [(owner,hz) for owner,hz,on in states if on]
        hz = 1/float(dt)
        matches = [(owner,freq) for owner,freq in active if compatible(hz,freq)]
        # Do not infer frequency from gaps or intervals where current may change.
        if not mapped["interval_valid"][i] or not states or not candidates[i+1]:
            label = "OUTSIDE_SUPPORTED_BOUNDARIES"
        elif dt>MAX_REGULAR_PERIOD_S:
            label = "LONG_GAP"
        elif sc_overlap(model,lo[i],hi[i+1]):
            label = "SC_TRANSITION"
        elif not active:
            label = "MUTED_MODEL_CONTEXT"
        elif len(active)!=len(states):
            label = "CURRENT_STATE_AMBIGUOUS"
        elif not matches:
            label = "NO_CANDIDATE_MATCH"
        elif len(states)==1:
            label = "UNIQUE_INITIAL_MATCH" if active[0][0] is None else "UNIQUE_SF_MATCH"
        elif len(matches)==len(active):
            label = "ALL_CANDIDATES_MATCH_IDENTITY_AMBIGUOUS"
        else:
            label = "SOME_CANDIDATES_MATCH"
        assessed = label in ("NO_CANDIDATE_MATCH","UNIQUE_INITIAL_MATCH","UNIQUE_SF_MATCH",
                              "ALL_CANDIDATES_MATCH_IDENTITY_AMBIGUOUS","SOME_CANDIDATES_MATCH")
        def encode(items):
            return ";".join("initial" if owner is None else str(owner) for owner,_ in items)
        rows.append({"start_id":ids[i],"end_id":ids[i+1],"start_scope_s":float(times[i]),
                     "end_scope_s":float(times[i+1]),"period_ms":float(dt*1000),
                     "inverse_period_hz":hz,"start_host_lower_ns":float(lo[i]) if np.isfinite(lo[i]) else None,
                     "start_host_upper_ns":float(hi[i]) if np.isfinite(hi[i]) else None,
                     "candidate_states":";".join(f"{'initial' if o is None else o}:{f}:{'active' if a else 'muted'}"
                                                 for o,f,a in states),
                     "active_candidate_ids":encode(active),"matching_candidate_ids":encode(matches),
                     "candidate_count":len(states),"matching_count":len(matches) if assessed else None,
                     "classification":label,"assessed":assessed,
                     "physical_application_proven":False})
    return rows,candidates


def classify_commands(model,cycles,candidates,times,mapped,support):
    lo,hi = mapped["lower"],mapped["upper"]
    start_lo,start_hi,end_lo,end_hi = support
    unique, possible, incompatible = Counter(),Counter(),Counter()
    selected = Counter(o for states in candidates for o,_,on in states if o is not None and on)
    for row in cycles:
        if not row["assessed"]:
            continue
        ids = {int(x) for x in row["active_candidate_ids"].split(";") if x and x!="initial"}
        matches = {int(x) for x in row["matching_candidate_ids"].split(";") if x and x!="initial"}
        possible.update(matches)
        incompatible.update(ids-matches)
        if row["classification"]=="UNIQUE_SF_MATCH":
            unique.update(matches)
    sf = model["sf_by_id"]
    result = []
    for owner,tr in sf.items():
        ack = tr["terminal_ns"]  # same denominator as earlier per-ACK audit
        membership = ("inside" if start_hi<=ack<=end_lo else
                      "outside" if ack<start_lo or ack>end_hi else "edge_uncertain")
        successor = next((k for k in sf if k>owner),None)
        witness = None
        if successor is not None and tr["mode"]=="ordinary" and sf[successor]["mode"]=="ordinary":
            nxt = sf[successor]
            for i,row in enumerate(cycles):
                # Both commands lie strictly between observed boundaries for
                # every tested clock value. No SC change or long gap. Require
                # the preceding observed period to be compatible with every
                # candidate, rather than infer an invisible pulse in a gap.
                if (row["classification"] not in ("UNIQUE_INITIAL_MATCH","UNIQUE_SF_MATCH",
                                                  "ALL_CANDIDATES_MATCH_IDENTITY_AMBIGUOUS")
                        or not candidates[i+1] or not all(s[2] for s in candidates[i+1])):
                    continue
                if (hi[i]<tr["write_begin_ns"] and nxt["ack_ns"]<lo[i+1]
                        and not sc_overlap(model,hi[i],lo[i+1])):
                    witness = f"{row['start_id']}->{row['end_id']}"
                    break
        if membership!="inside":
            label = "OUTSIDE_OR_EDGE"
        elif tr["mode"]=="mute_scheduler":
            label = "MUTED_SCHEDULER"
        elif unique[owner]:
            label = "UNIQUE_MATCHING_CYCLE"
        elif possible[owner]:
            label = "AMBIGUOUS_MATCHING_CANDIDATE"
        elif witness is not None and not selected[owner]:
            label = "SUPERSEDED_BETWEEN_OBSERVED_BOUNDARIES"
        elif incompatible[owner]:
            label = "CANDIDATE_WITHOUT_MATCH"
        elif selected[owner]:
            label = "CANDIDATE_WITHOUT_ASSESSED_PERIOD"
        else:
            label = "NO_SUPPORTED_ACTIVE_BOUNDARY"
        result.append({"sf_id":owner,"request_ns":tr["request_ns"],"write_ns":tr["write_begin_ns"],
                       "embedded_ack_ns":tr["ack_ns"],"trace_ack_ns":ack,
                       "frequency_hz":tr["requested_sf_hz"],"purpose":tr["mode"],
                       "scope_membership":membership,"classification":label,
                       "possible_active_boundaries":selected[owner],"unique_matching_cycles":unique[owner],
                       "possible_matching_cycles":possible[owner],"nonmatching_candidate_cycles":incompatible[owner],
                       "successor_sf_id":successor,"supersession_witness":witness,
                       "physical_application_proven":False})
    return result


def summarize(cycles,commands):
    targets = [r for r in commands if r["scope_membership"]=="inside" and r["purpose"]!="mute_scheduler"]
    counts = Counter(r["classification"] for r in targets)
    return {"all_live_sf_acks":len(commands),"target_acks_inside":len(targets),
            "target_classifications":dict(counts),
            "target_unique_matching":counts["UNIQUE_MATCHING_CYCLE"],
            "target_any_matching":counts["UNIQUE_MATCHING_CYCLE"]+counts["AMBIGUOUS_MATCHING_CANDIDATE"],
            "target_supersession_candidates":counts["SUPERSEDED_BETWEEN_OBSERVED_BOUNDARIES"],
            "command_scope_membership":dict(Counter(r["scope_membership"] for r in commands)),
            "cycle_classifications":dict(Counter(r["classification"] for r in cycles)),
            "assessed_cycles":sum(r["assessed"] for r in cycles),
            "assessed_cycles_with_match":sum(r["assessed"] and r["matching_count"]>0 for r in cycles)}


def run(run_dir):
    ctx = load_refined_evidence(run_dir)
    root = ctx["root"]
    dest = ensure_derived_destination(ctx["analysis"]/"cycle_boundary_observability",root)
    if dest.exists():
        raise FileExistsError(dest)
    model = prepare_commands(ctx["events"])
    ids = [r["id"] for r in ctx["onsets"]]
    times = ctx["times"]
    scenarios,outputs = {},{}
    for scale in (1.,2.,3.):
        mapped,support,clock = map_refined_evidence(ctx,scale)
        cycles,candidates = analyze_cycles(model,times,ids,mapped,support)
        commands = classify_commands(model,cycles,candidates,times,mapped,support)
        scenarios[str(scale)] = {**summarize(cycles,commands),"clock":clock}
        outputs[str(scale)] = (cycles,commands)
    dest.mkdir(parents=True,exist_ok=False)
    for scale,(cycles,commands) in outputs.items():
        if cycles:
            _csv(dest/f"physical_cycles_scale_{scale}.csv",cycles,list(cycles[0]))
        if commands:
            _csv(dest/f"sf_ack_attribution_scale_{scale}.csv",commands,list(commands[0]))
    atomic_json(dest/"summary.json",{
        "schema":"cycle_boundary_latest_pending_sf_v1","run":root.name,
        "source_sha256":ctx["hashes"],"trace_sha256":sha256(ctx["trace"]),
        "refinement_sha256":sha256(ctx["refinement_path"]),
        "approved_onsets_sha256":ctx["approval"]["approved_onsets_sha256"],
        "physical_application_proven":False,"absolute_alignment_calibrated":False,
        "model":"latest available SF at approved negative onset controls following cycle",
        "availability_assumption":"between host write begin and embedded ACK receipt",
        "tolerance_abs_hz":MATCH_ABSOLUTE_HZ,"tolerance_fraction":MATCH_RELATIVE_FRACTION,
        "max_regular_period_s":MAX_REGULAR_PERIOD_S,"scenarios":scenarios})
    lines = ["# Exploratory cycle-boundary observability","",f"Run: {root.name}","",
             "The scheduler is modeled as selecting the latest available SF at each approved",
             "negative onset. Availability between BLE write begin and ACK is an assumption;",
             "neither ACK nor clock alignment proves internal application. No clock fit was",
             "chosen using frequency agreement. Commands during a physical cycle do not",
             "invalidate that cycle's witness. SC-overlap intervals and long gaps are excluded.",
             "Unique matches require a single candidate identity, not just one matching value.",
             "Supersession candidates require two successive SF commands strictly between",
             "compatible observed boundaries, no SC change and no possible active boundary",
             "for the earlier SF. Unobserved internal boundaries remain possible.",
             "Muted scheduler ACKs are counted but excluded from the target denominator.",
             "Initial START frequency is context, not a live SF ACK.","",
             "| Gray scale | Target ACKs | Unique match | Any candidate match | Supersession candidates | Assessed cycles matching |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for scale,s in scenarios.items():
        lines.append(f"| {scale} | {s['target_acks_inside']} | {s['target_unique_matching']} | {s['target_any_matching']} | {s['target_supersession_candidates']} | {s['assessed_cycles_with_match']}/{s['assessed_cycles']} |")
    (dest/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(f"[CYCLE BOUNDARY] {dest/'report.md'}")


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir",required=True,type=Path)
    run(p.parse_args(argv).run_dir)


if __name__=="__main__":
    main()
