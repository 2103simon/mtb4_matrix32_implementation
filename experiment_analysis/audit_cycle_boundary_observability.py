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


def compatible(measured, hz, *, absolute_hz=MATCH_ABSOLUTE_HZ,
               fraction=MATCH_RELATIVE_FRACTION):
    return abs(measured-hz)<=max(absolute_hz,fraction*hz)


def analyze_cycles(model,times,ids,mapped,support, *,
                   absolute_hz=MATCH_ABSOLUTE_HZ, fraction=MATCH_RELATIVE_FRACTION):
    lo,hi = mapped["lower"],mapped["upper"]
    _,support_start,support_end,_ = support
    candidates = [boundary_states(model,a,b) if v and a>=support_start and b<=support_end else []
                  for a,b,v in zip(lo,hi,mapped["valid"])]
    rows = []
    for i,dt in enumerate(np.diff(times)):
        states = candidates[i]
        active = [(owner,hz) for owner,hz,on in states if on]
        hz = 1/float(dt)
        matches = [(owner,freq) for owner,freq in active
                   if compatible(hz,freq,absolute_hz=absolute_hz,fraction=fraction)]
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


TOLERANCES = (
    ("original", MATCH_ABSOLUTE_HZ, MATCH_RELATIVE_FRACTION),
    ("tight", .5, .01),
    ("very_tight", .25, .005),
)


def candidate_ids(value):
    """Initial START precedes every live SF identity; retain repeated values."""
    return {-1 if x=="initial" else int(x) for x in value.split(";") if x}


def chronological_attribution(cycles, target_ids):
    """Exact extrema and identity membership over nondecreasing owner paths.

    A target contributes once, when first entered. Nondecreasing IDs ensure
    it cannot be revisited after leaving. Each state carries min/max counts
    and the intersection/union of target IDs across all paths to that state.
    No choice of clock offset or actual BLE availability is inferred.
    Excluded periods do not constrain owners. Incompatible assessed periods
    are reported and omitted only for the explicitly conditional subset.
    """
    target_ids = set(target_ids)
    assessed = [r for r in cycles if r["assessed"]]
    matching = [r for r in assessed if r["matching_count"]>0]
    base = {"assessed_cycles":len(assessed),"matching_cycles":len(matching),
            "incompatible_cycles":len(assessed)-len(matching),
            "incompatible_cycle_ids":[r["start_id"]+"->"+r["end_id"]
                                      for r in assessed if r["matching_count"]==0],
            "clock_realization_validated":False}
    empty = {**base,"path_exists":False,"all_assessed_cycles_have_path":False,
             "min_distinct_target_acks":None,"max_distinct_target_acks":None,
             "required_target_ids":[],"possible_target_ids":[]}
    if not matching:
        return {**empty,"status":"NO_MATCHING_CYCLES"}
    # prior owner, then (minimum count, maximum count, required IDs, possible IDs)
    dp = {-1:(0,0,set(),set())}
    for row in matching:
        following = {}
        for owner in candidate_ids(row["matching_candidate_ids"]):
            paths = []
            for previous,(low,high,required,possible) in dp.items():
                if previous>owner:
                    continue
                add = int(owner!=previous and owner in target_ids)
                visited = {owner} if owner in target_ids else set()
                paths.append((low+add,high+add,required|visited,possible|visited))
            if paths:
                following[owner] = (min(p[0] for p in paths),max(p[1] for p in paths),
                                    set.intersection(*(p[2] for p in paths)),
                                    set.union(*(p[3] for p in paths)))
        if not following:
            return {**empty,"status":"NO_NONDECREASING_PATH",
                    "first_order_conflict_cycle":row["start_id"]+"->"+row["end_id"]}
        dp = following
    final = list(dp.values())
    return {**base,"path_exists":True,
            "all_assessed_cycles_have_path":len(matching)==len(assessed),
            "status":"COMPLETE_ASSESSED_PATH" if len(matching)==len(assessed)
                     else "MATCHING_SUBSET_ONLY",
            "min_distinct_target_acks":min(p[0] for p in final),
            "max_distinct_target_acks":max(p[1] for p in final),
            "required_target_ids":sorted(set.intersection(*(p[2] for p in final))),
            "possible_target_ids":sorted(set.union(*(p[3] for p in final)))}


def command_investigation(model, cycles, candidates, commands, mapped, chronology):
    """Explain missing matches using per-command evidence, not inferred execution."""
    lo,hi = mapped["lower"],mapped["upper"]
    required,possible = set(chronology["required_target_ids"]),set(chronology["possible_target_ids"])
    targets = [r for r in commands if r["scope_membership"]=="inside"
               and r["purpose"]!="mute_scheduler"]
    rows = []
    for row in targets:
        owner = row["sf_id"]
        tr = model["sf_by_id"][owner]
        successor = row["successor_sf_id"]
        nxt = model["sf_by_id"].get(successor)
        active = [i for i,states in enumerate(candidates)
                  if any(o==owner and on for o,_,on in states)]
        muted = [i for i,states in enumerate(candidates)
                 if any(o==owner and not on for o,_,on in states)]
        relevant = [r for r in cycles if str(owner) in r["active_candidate_ids"].split(";")]
        assessed = [r for r in relevant if r["assessed"]]
        errors = [abs(r["inverse_period_hz"]-row["frequency_hz"]) for r in assessed]
        strict,possible_brackets = [],[]
        if nxt is not None and tr["mode"]=="ordinary" and nxt["mode"]=="ordinary":
            for i,r in enumerate(cycles):
                if not r["assessed"] or not r["matching_count"] or not candidates[i+1]:
                    continue
                if not all(s[2] for s in candidates[i]+candidates[i+1]):
                    continue
                if sc_overlap(model,lo[i],hi[i+1]):
                    continue
                witness = r["start_id"]+"->"+r["end_id"]
                # A weaker possible bracket allows the actual boundary and
                # command availability to lie anywhere in their tested windows.
                # This is a local possibility, not a joint clock solution.
                if lo[i]<tr["ack_ns"] and nxt["write_begin_ns"]<hi[i+1]:
                    possible_brackets.append(witness)
                if hi[i]<tr["write_begin_ns"] and nxt["ack_ns"]<lo[i+1]:
                    strict.append(witness)
        local_match = row["possible_matching_cycles"]>0
        if not chronology["path_exists"]:
            order_status = "NO_CHRONOLOGICAL_SOLUTION"
        elif owner in required:
            order_status = "REQUIRED_ON_ALL_MATCHING_PATHS"
        elif owner in possible:
            order_status = "OPTIONAL_ON_MATCHING_PATHS"
        elif local_match:
            order_status = "LOCAL_MATCH_EXCLUDED_BY_ORDER"
        else:
            order_status = "NO_LOCAL_MATCH"
        if local_match:
            reason = "LOCAL_MATCH_PRESENT"
        elif assessed:
            reason = ("OTHER_CANDIDATE_MATCHES" if all(r["matching_count"]>0 for r in assessed)
                      else "INCOMPATIBLE_ASSESSED_CYCLE")
        elif active:
            reason = "ACTIVE_BOUNDARY_WITHOUT_ASSESSED_FOLLOWING_CYCLE"
        elif strict:
            reason = "STRICT_BETWEEN_CYCLE_REPLACEMENT_CANDIDATE"
        elif possible_brackets:
            reason = "POSSIBLE_BETWEEN_CYCLE_REPLACEMENT"
        elif muted:
            reason = "MUTED_BOUNDARY_ONLY"
        elif nxt is None:
            reason = "NO_SUCCESSOR_OR_SUPPORTED_BOUNDARY"
        else:
            reason = "NO_SUPPORTED_BOUNDARY_UNRESOLVED"
        rows.append({**row,"chronological_status":order_status,"investigation_reason":reason,
                     "possible_muted_boundaries":len(muted),
                     "assessed_candidate_cycles":len(assessed),
                     "candidate_cycle_ids":";".join(r["start_id"]+"->"+r["end_id"] for r in relevant),
                     "candidate_cycle_classes":";".join(sorted({r["classification"] for r in relevant})),
                     "nearest_candidate_error_hz":min(errors) if errors else None,
                     "next_sf_write_ns":nxt["write_begin_ns"] if nxt else None,
                     "next_sf_embedded_ack_ns":nxt["ack_ns"] if nxt else None,
                     "ack_to_next_sf_write_ms":(nxt["write_begin_ns"]-tr["ack_ns"])/1e6 if nxt else None,
                     "strict_replacement_brackets":";".join(strict),
                     "possible_replacement_brackets":";".join(possible_brackets)})
    return rows


def run(run_dir):
    ctx = load_refined_evidence(run_dir)
    root = ctx["root"]
    dest = ensure_derived_destination(ctx["analysis"]/"cycle_boundary_observability_chronological",root)
    if dest.exists():
        raise FileExistsError(dest)
    model = prepare_commands(ctx["events"])
    ids = [r["id"] for r in ctx["onsets"]]
    times = ctx["times"]
    scenarios,outputs = {},{}
    for scale in (1.,2.,3.):
        mapped,support,clock = map_refined_evidence(ctx,scale)
        scenarios[str(scale)] = {"clock":clock,"tolerances":{}}
        for name,absolute,fraction in TOLERANCES:
            cycles,candidates = analyze_cycles(model,times,ids,mapped,support,
                                               absolute_hz=absolute,fraction=fraction)
            commands = classify_commands(model,cycles,candidates,times,mapped,support)
            target_ids = {r["sf_id"] for r in commands if r["scope_membership"]=="inside"
                          and r["purpose"]!="mute_scheduler"}
            chronology = chronological_attribution(cycles,target_ids)
            result = {**summarize(cycles,commands),"chronological":chronology,
                      "tolerance_abs_hz":absolute,"tolerance_fraction":fraction}
            scenarios[str(scale)]["tolerances"][name] = result
            if name=="original":
                details = command_investigation(model,cycles,candidates,commands,mapped,chronology)
                result["chronological_status_counts"] = dict(Counter(r["chronological_status"] for r in details))
                result["unmatched_reason_counts"] = dict(Counter(
                    r["investigation_reason"] for r in details if r["possible_matching_cycles"]==0))
                outputs[str(scale)] = details
    dest.mkdir(parents=True,exist_ok=False)
    for scale,details in outputs.items():
        if details:
            _csv(dest/f"target_ack_investigation_scale_{scale}.csv",details,list(details[0]))
    atomic_json(dest/"summary.json",{
        "schema":"cycle_boundary_latest_pending_sf_chronological_v2","run":root.name,
        "source_sha256":ctx["hashes"],"trace_sha256":sha256(ctx["trace"]),
        "refinement_sha256":sha256(ctx["refinement_path"]),
        "approved_onsets_sha256":ctx["approval"]["approved_onsets_sha256"],
        "physical_application_proven":False,"absolute_alignment_calibrated":False,
        "model":"latest available SF at approved negative onset controls following cycle",
        "availability_assumption":"between host write begin and embedded ACK receipt",
        "chronology_assumption":"nondecreasing SF identity across matching assessed cycles",
        "common_clock_realization_validated":False,
        "max_regular_period_s":MAX_REGULAR_PERIOD_S,"scenarios":scenarios})
    lines = ["# Exploratory chronological cycle-boundary observability","",f"Run: {root.name}","",
             "Original clock envelopes and signed pulse inventory are unchanged.",
             "Each matching assessed cycle chooses one candidate SF identity in chronological order.",
             "Distinct target ACK extrema count repeated cycles of one command only once.",
             "Required identities appear on every feasible path; optional identities on some.",
             "These are combinatorial bounds under the availability model and per-boundary",
             "envelopes, NOT measured execution counts or a jointly calibrated clock solution.",
             "A path through matching cycles only is explicitly conditional if any assessed",
             "cycle is incompatible. Excluded intervals remain unobserved.",
             "Replacement brackets are candidate explanations, not proof of supersession.","",
             "| Gray scale | Tolerance | Target ACKs | Unique local | Any local | Min distinct | Max distinct | Matching/assessed cycles | Path status |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for scale,s in scenarios.items():
        for name,r in s["tolerances"].items():
            p = r["chronological"]
            lines.append(f"| {scale} | {name} | {r['target_acks_inside']} | {r['target_unique_matching']} | {r['target_any_matching']} | {p['min_distinct_target_acks']} | {p['max_distinct_target_acks']} | {r['assessed_cycles_with_match']}/{r['assessed_cycles']} | {p['status']} |")
    lines += ["","## Primary scenario: 1x, original tolerance","",
              "Unmatched local command evidence:"]
    primary = scenarios["1.0"]["tolerances"]["original"]
    for reason,count in sorted(primary["unmatched_reason_counts"].items()):
        lines.append(f"- {reason}: {count}")
    lines += ["","Chronological identity membership:"]
    for reason,count in sorted(primary["chronological_status_counts"].items()):
        lines.append(f"- {reason}: {count}")
    (dest/"report.md").write_text("\\n".join(lines)+"\\n",encoding="utf-8")
    print(f"[CYCLE BOUNDARY] {dest/'report.md'}")


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir",required=True,type=Path)
    run(p.parse_args(argv).run_dir)


if __name__=="__main__":
    main()
