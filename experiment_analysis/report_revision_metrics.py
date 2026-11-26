"""Verified aggregation of the completed timing, BLE and electrical diagnostics.

This is a presentation calculation, not a new waveform detector or scheduler
model. Retain reference and replay populations separately. Reuse the signed
pulse inventory and existing versioned diagnostics; refuse stale provenance.
"""
from __future__ import annotations

from collections import Counter
import csv
import json
from pathlib import Path
import statistics

from experiment_analysis.audit_ack_physical_observability import verify_approved_record, event_stream
from experiment_analysis.paths import analysis_root, raw_run_dir, sha256
from experiment_analysis.analyze_command_chain import transaction_rows

REFERENCE = "20261124T114542660847Z_integrated_hw_10M_20s_resume_check"
RUNS = {
    "Reference": REFERENCE,
    "A1": "20261126T090848918698Z_ble_ab_baseline",
    "B1": "20261126T091212099927Z_ble_ab_shared",
    "A2": "20261126T094712943788Z_ble_ab_baseline_20vdiv",
    "B2": "20261126T095023427339Z_ble_ab_shared_20vdiv",
}
PAIRS = (("A1","B1"),("A2","B2"))
SCALES = ("1.0","2.0","3.0")
TOLERANCES = ("original","tight","very_tight")
CHRON = "cycle_boundary_observability_chronological"
RESTART = "restart_observability"
BOUNDARIES = "refined_ack_observability_boundaries"
PIECEWISE = "piecewise_command_analysis_integer"
REFINEMENT = "timing_refinement/timing_refinement_summary.json"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def csv_rows(path):
    with Path(path).open(newline="",encoding="utf-8") as f:
        return list(csv.DictReader(f))


class Evidence:
    def __init__(self, run_dir):
        self.root = raw_run_dir(run_dir)
        self.analysis = analysis_root(self.root, legacy_ok=False)
        self.hashes = {}
        self.inputs = {}
        meta = read_json(self.root/"metadata.json")
        require(len(meta["records"])==1, f"{self.root.name}: expected one scope record")
        self.record = meta["records"][0]
        self.approval, onsets, _, self.excluded = verify_approved_record(self.root,self.record)
        self.approved = len(onsets)
        # The shared verifier has already hashed this complete raw waveform.
        ch1 = (self.root/self.record["channels"]["CH1"]["path"]).resolve()
        self.hashes[ch1] = self.approval["source_ch1_sha256"]
        self.trace, self.events = event_stream(self.root)
        self.transactions, issues = transaction_rows(self.events, [], [])
        require(not issues, f"{self.root.name}: ambiguous command transactions")
        self.refined = self.load(REFINEMENT,"timing_refinement_v1","recording")
        self.check_raw(self.refined["source_sha256"])
        require(len(self.refined["records"])==1, "Expected one timing refinement record")
        self.refinement_hash = self.digest(self.analysis/REFINEMENT)

    def digest(self,path):
        path = Path(path).resolve()
        if path not in self.hashes:
            self.hashes[path] = sha256(path)
        return self.hashes[path]

    def check_raw(self,hashes):
        for name,expected in hashes.items():
            path = (self.root/name).resolve()
            require(self.root in path.parents and self.digest(path)==expected,
                    f"{self.root.name}: raw fingerprint mismatch: {name}")

    def load(self,relative,schema=None,identity=None):
        p = self.analysis/relative
        obj = read_json(p)
        self.inputs[relative] = self.digest(p)
        if schema:
            require(obj.get("schema")==schema,f"{p}: unexpected schema")
        if identity:
            require(obj.get(identity)==self.root.name,f"{p}: wrong recording")
        return obj

    def rows(self,relative):
        p = self.analysis/relative
        rows = csv_rows(p)
        self.inputs[relative] = self.digest(p)
        return rows

    def verify_audit(self,obj,*,refinement_key="refinement_sha256"):
        require(obj["source_sha256"]==self.refined["source_sha256"],
                f"{self.root.name}: clock source fingerprints differ")
        require(obj[refinement_key]==self.refinement_hash,
                f"{self.root.name}: timing refinement changed")
        require(obj["trace_sha256"]==self.digest(self.trace),
                f"{self.root.name}: command trace changed")
        require(obj["approved_onsets_sha256"]==self.approval["approved_onsets_sha256"],
                f"{self.root.name}: signed onsets changed")
        require(obj["absolute_alignment_calibrated"] is False and
                obj["physical_application_proven"] is False,
                "Unexpected qualification in exploratory audit")

    def collect(self):
        chronological = self.load(CHRON+"/summary.json","cycle_boundary_latest_pending_sf_chronological_v2","run")
        restart = self.load(RESTART+"/summary.json","matrix32_restart_observability_v1","run")
        bounds = self.load(BOUNDARIES+"/summary.json","matrix32_refined_per_ack_boundaries_v2","run")
        for obj in (chronological,restart):
            self.verify_audit(obj)
        self.verify_audit(bounds,refinement_key="timing_refinement_sha256")
        stable = self.load(PIECEWISE+"/summary.json","matrix32_piecewise_command_association_integer_v2","source_run")
        require(stable["source_sha256"]==self.refined["source_sha256"] and
                stable["timing_refinement_sha256"]==self.refinement_hash and
                stable["total_approved_intervals"]==max(0,self.approved-1),
                f"{self.root.name}: stable frequency provenance mismatch")
        # Stable CSV is bound by recorded onset IDs and count. Source approval
        # and raw CH1 are verified above; this legacy summary has no onset hash.
        stable_rows = self.rows(PIECEWISE+"/all_intervals.csv")
        require(len(stable_rows)==self.approved-1,"Stable interval inventory mismatch")
        folder = self.analysis/f"record_{int(self.record['index']):05d}"/"pulse_review"
        approved_rows = csv_rows(folder/"approved_intervals.csv")
        require(all((a["start_onset_id"],a["end_onset_id"])==(b["start_id"],b["end_id"])
                    and abs(float(a["period_ms"])/1000-float(b["period_s"]))<1e-9
                    for a,b in zip(stable_rows,approved_rows)), "Stable CSV differs from signed periods")
        for key,kind in (("ack","ack"),("controller_target","mcu")):
            observed = [abs(float(r[kind+"_error_hz_if_stable"])) for r in stable_rows
                        if r[kind+"_assessment"]=="STABLE_ACROSS_TESTED_OFFSETS"]
            require(len(observed)==stable[key]["stable_intervals"],f"{key}: stable count differs")
            if observed:
                require(abs(statistics.median(observed)-stable[key]["median_abs_error_hz"])<1e-8,
                        f"{key}: median differs")
        sf = [r for r in self.transactions if r["family"]=="sf"]
        sc = [r for r in self.transactions if r["family"]=="sc"]
        data = {"run":self.root.name,"approved_onsets":self.approved,
                "approved_intervals":self.approved-1,"excluded_negative_only":self.excluded,
                "sf_requests":len(sf),"sf_ok":sum(r["success"] for r in sf),
                "sc_requests":len(sc),"sc_ok":sum(r["success"] for r in sc),
                "muted_scheduler_full":sum(r["mode"]=="mute_scheduler" for r in sf),
                "stable":{"ack":stable["ack"],"controller_target":stable["controller_target"],
                          "both_stable_intervals":stable["both_stable_intervals"],
                          "same_interval_comparison":stable.get("same_interval_comparison")},
                "scenarios":{}}
        for scale in SCALES:
            c = chronological["scenarios"][scale]
            original = c["tolerances"]["original"]
            b = bounds["scenarios"][scale]["boundaries"]
            re = restart["scenarios"][scale]
            rows = self.rows(f"{CHRON}/target_ack_investigation_scale_{scale}.csv")
            require(len(rows)==original["target_acks_inside"] and
                    len({r["sf_id"] for r in rows})==len(rows) and
                    all(r["scope_membership"]=="inside" for r in rows),
                    "Target ACK inventory differs from summary")
            require(original["all_live_sf_acks"]==data["sf_ok"] and
                    sum(original["cycle_classifications"].values())==data["approved_intervals"] and
                    sum(int(r["possible_matching_cycles"])>0 for r in rows)==original["target_any_matching"],
                    "Chronological totals differ from raw/CSV evidence")
            require(all(v["target_sf_inside"]["n"]==len(rows) for v in b.values()),
                    "Settled and chronological target denominators differ")
            types = {}
            for purpose in ("ordinary","restore"):
                subset = [r for r in rows if r["purpose"]==purpose]
                types[purpose] = {"n":len(subset),"local_matching":sum(int(r["possible_matching_cycles"])>0 for r in subset),
                                  "required_on_paths":sum(r["chronological_status"]=="REQUIRED_ON_ALL_MATCHING_PATHS" for r in subset),
                                  "optional_on_paths":sum(r["chronological_status"]=="OPTIONAL_ON_MATCHING_PATHS" for r in subset)}
            require(sum(t["n"] for t in types.values())==len(rows),"Unexpected target command purpose")
            require(types["restore"]["n"]==re["restore_sf_acks_inside"] and
                    types["restore"]["local_matching"]==re["inside_prior_matching"],
                    "Restart and chronological restore counts differ")
            re_rows = self.rows(f"{RESTART}/sc_restarts_scale_{scale}.csv")
            no_pulse = [r for r in re_rows if r["classification"]=="NO_SUPPORTED_PULSE_BEFORE_NEXT_CONTROL"]
            windows = [(float(r["active_window_end_ns"])-float(r["restore_sc_ack_ns"]))/1e6 for r in no_pulse]
            require(all(w>=0 for w in windows),"Negative restart window")
            path = original["chronological"]
            data["scenarios"][scale] = {
                "target_acks":len(rows),"types":types,
                "unique_local":original["target_unique_matching"],"any_local":original["target_any_matching"],
                "required_on_paths":len(path["required_target_ids"]),
                "optional_on_paths":len(set(path["possible_target_ids"])-set(path["required_target_ids"])),
                "local_unmatched_reasons":original["unmatched_reason_counts"],
                "possible_replacement_brackets":sum(bool(r["possible_replacement_brackets"]) for r in rows if int(r["possible_matching_cycles"])==0),
                "strict_replacement_brackets":sum(bool(r["strict_replacement_brackets"]) for r in rows if int(r["possible_matching_cycles"])==0),
                "settled":{name:{"with_guard":v["target_sf_inside"]["one_or_more_matching"],
                                  "without_guard":v["target_sf_inside"]["matching_without_guard"]}
                           for name,v in b.items()},
                "restart":{k:v for k,v in re.items() if k!="clock"},
                "no_pulse_windows_ms":windows,
                "no_pulse_shorter_than_period":sum(w<1000/float(r["restore_frequency_hz"]) for w,r in zip(windows,no_pulse)),
                "tolerances":{name:{"min":v["chronological"]["min_distinct_target_acks"],
                                    "max":v["chronological"]["max_distinct_target_acks"],
                                    "assessed":v["assessed_cycles"],"matching":v["assessed_cycles_with_match"],
                                    "status":v["chronological"]["status"],
                                    "incompatible_cycle_ids":v["chronological"]["incompatible_cycle_ids"]}
                              for name,v in c["tolerances"].items()},
                "clock":c["clock"]}
        rec = self.refined["records"][0]
        model = rec["models"]["piecewise_linear"]
        data["timing"] = {"gray_frames":rec["frame_count"],"original_status":rec["original_status"],
                          "piecewise_holdout_rms_us":{k:v["rms_us"] for k,v in model["holdouts"].items()},
                          "affine_holdout_rms_us":rec["models"]["affine"]["holdouts"]["single_frame"]["rms_us"],
                          "max_local_rate_deviation_ppm":model["clock_rate_stability"]["max_local_slope_deviation_from_median_ppm"]}
        return data

    def charge(self,quiet):
        folder = f"transient_charge_diagnostic_quiet_{str(float(quiet)).replace('.','p')}v"
        d = self.load(folder+"/summary.json","resistor_branch_transient_charge_v1","run")
        require(d["metadata_sha256"]==self.digest(self.root/"metadata.json") and
                d["resistance_ohm"]==100000 and d["quiet_limit_v"]==quiet and
                d["primary_post_us"]==1000 and d["pre_us"]==100 and
                d["records"][0]["storage_rail_samples"]==0,"Charge setup/provenance mismatch")
        for name,expected in d["source_sha256"].items():
            base = self.analysis if name.startswith("record_") else self.root
            require(self.digest(base/name)==expected,f"Charge source changed: {name}")
        rows = self.rows(folder+"/event_charge_windows.csv")
        output = {"groups":d["groups"],"records":d["records"],"quiet_limit_v":quiet,"windows":{}}
        for post in d["post_windows_us"]:
            subset = [r for r in rows if float(r["window_post_us"])==post and r["status"]=="OK"]
            output["windows"][str(post)] = {}
            for kind in ("accepted","rejected"):
                events = [r for r in subset if r["kind"]==kind]
                output["windows"][str(post)][kind] = {
                    "eligible":len(events),
                    "median_net_nc":statistics.median(float(r["net_charge_nc"]) for r in events) if events else None}
                if post==1000:
                    require(len(events)==d["groups"][kind]["eligible"],"Charge count mismatch")
        primary = [r for r in rows if float(r["window_post_us"])==1000 and r["kind"]=="rejected"]
        require(len(primary)==self.excluded,"Charge candidate and signed exclusion counts differ")
        output["rejected_near_2p5ms"] = sum(
            bool(r["after_previous_biphasic_ms"]) and abs(float(r["after_previous_biphasic_ms"])-2.5)<=.1
            for r in primary)
        return output


def pool(data,labels,scale="1.0",tolerance="original"):
    items = [data[k]["scenarios"][scale] for k in labels]
    p = [r["tolerances"][tolerance] for r in items]
    counts = lambda field: dict(sum((Counter(r[field]) for r in items),Counter()))
    types = {k:{f:sum(r["types"][k][f] for r in items)
                for f in ("n","local_matching","required_on_paths","optional_on_paths")}
             for k in ("ordinary","restore")}
    windows = [v for r in items for v in r["no_pulse_windows_ms"]]
    return {"target_acks":sum(r["target_acks"] for r in items),"types":types,
            "unique_local":sum(r["unique_local"] for r in items),"any_local":sum(r["any_local"] for r in items),
            "min":sum(r["min"] for r in p) if all(r["min"] is not None for r in p) else None,
            "max":sum(r["max"] for r in p) if all(r["max"] is not None for r in p) else None,
            "assessed":sum(r["assessed"] for r in p),"matching":sum(r["matching"] for r in p),
            "path_statuses":[r["status"] for r in p],
            "unmatched_reasons":counts("local_unmatched_reasons"),
            "restart_reasons":dict(sum((Counter(r["restart"]["inside_attempt_reasons"]) for r in items),Counter())),
            "restart_unmatched_reasons":dict(sum((Counter(r["restart"]["inside_unmatched_reasons"]) for r in items),Counter())),
            "new_restart_matches":sum(r["restart"]["inside_new_local_matching"] for r in items),
            "restart_union_matches":sum(r["restart"]["inside_union_matching"] for r in items),
            "no_pulse_windows_count":len(windows),
            "no_pulse_window_median_ms":statistics.median(windows) if windows else None,
            "no_pulse_shorter_than_period":sum(r["no_pulse_shorter_than_period"] for r in items)}


def collect(recordings_dir):
    evidence = {k:Evidence(Path(recordings_dir)/run) for k,run in RUNS.items()}
    data = {k:v.collect() for k,v in evidence.items()}
    reference = evidence["Reference"]
    chain = reference.load("command_chain_diagnostic/summary.json","matrix32_command_chain_diagnostic_v1","run")
    reference.check_raw(chain["source_sha256"])
    for name,digest in chain["derived_sha256"].items():
        require(reference.digest(reference.analysis/PIECEWISE/name)==digest,
                f"Reference command-chain diagnostic uses different {name}")
    replay = {}
    source_identity = None
    for a,b in PAIRS:
        obj = evidence[b].load("ble_ab_comparison/summary.json","matrix32_ble_replay_ab_v1")
        for label,role,shared in ((a,"baseline",False),(b,"shared",True)):
            row = obj[role]
            ev = evidence[label]
            require(row["run"]==ev.root.name and row["shared"] is shared and
                    row["input_values_and_schedule_verified"] and not row["pairing_issues"],
                    "Replay comparison identity or verification failed")
            ev.check_raw(row["source_sha256"])
            for family in ("sf","sc"):
                g = row["latency_groups"][family]
                require(g["count"]==data[label][family+"_requests"] and
                        g["success"]==data[label][family+"_ok"],"Replay protocol totals differ")
            prov = row["replay_source"]
            require(Path(prov["source_run"]).name==reference.root.name,"Different replay source")
            identity = {k:v for k,v in prov.items() if k!="source_run"}
            if source_identity is None:
                source_identity = identity
            require(identity==source_identity,"Replays do not share one source window")
            for suffix,field in (("_tactile_rates.csv","source_rates_sha256"),("_settings.json","source_settings_sha256")):
                matches = list((reference.root/"mcu_matrix32").glob("*"+suffix))
                require(len(matches)==1 and reference.digest(matches[0])==prov[field],"Replay source hash mismatch")
            replay[label] = {k:v for k,v in row.items() if k not in ("subscription_groups","source_sha256","replay_source")}
    charge = {label:{str(q):evidence[label].charge(q) for q in (2.5,4.)} for label in ("A2","B2")}
    pools = {scale:{tol:{"baseline":pool(data,("A1","A2"),scale,tol),
                        "shared":pool(data,("B1","B2"),scale,tol)} for tol in TOLERANCES} for scale in SCALES}
    return {"schema":"matrix32_report_revision_metrics_v1","runs":data,"replay":replay,
            "replay_source":source_identity,"reference_command_chain":chain,
            "charge":charge,"pooled_replays":pools,
            "evidence_sha256":{k:v.inputs for k,v in evidence.items()},
            "signed_onsets_sha256":{k:v.approval["approved_onsets_sha256"] for k,v in evidence.items()},
            "raw_metadata_sha256":{k:v.digest(v.root/"metadata.json") for k,v in evidence.items()},
            "physical_application_proven":False,"absolute_alignment_calibrated":False}
