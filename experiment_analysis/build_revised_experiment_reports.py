"""Rebuild both review reports using the approved 2026-11-25 publication and
the completed five-recording investigation. Never modify an approved report,
raw data, signed pulse review, or existing diagnostic. No hardware access.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import numpy as np

from experiment_analysis.paths import REPO_ROOT, analysis_root, ensure_derived_destination, sha256
from experiment_analysis.publish_reports import REPORTS, IMAGE_RE
from experiment_analysis.report_revision_metrics import (
    REFERENCE, RUNS, SCALES, TOLERANCES, CHRON, RESTART, BOUNDARIES,
    PIECEWISE, collect, read_json, require,
)
from oscilloscope.record_scope import atomic_json

REVISION_FOLDER = "report_revision"
APPROVED_TAG = "2026-11-25-approved"


def sections(markdown):
    """Use exact approved section bodies; fail on missing/duplicate headings."""
    headings = list(re.finditer(r"^## ([^\n]+)$",markdown,re.M))
    result = {}
    for i,h in enumerate(headings):
        name = h.group(1)
        require(name not in result,f"Duplicate report heading: {name}")
        result[name] = markdown[h.end():headings[i+1].start() if i+1<len(headings) else len(markdown)].strip()
    return result


def historical_body(markdown,heading):
    body = sections(markdown)[heading]
    # Published local supporting evidence paths are already explicit text.
    # Reuse the unchanged images in the existing reference analysis folder.
    return IMAGE_RE.sub(lambda m:f"![{m.group(1)}](../{m.group(2)})",body)


def load_approved_reference(metrics,analysis):
    source = REPO_ROOT/"reports"/REFERENCE/APPROVED_TAG
    manifest = read_json(source/"publication_manifest.json")
    require(manifest["run_id"]==REFERENCE and manifest["tag"]==APPROVED_TAG and
            manifest["metadata_sha256"]==metrics["raw_metadata_sha256"]["Reference"],
            "Approved publication is not bound to the reference recording")
    reports = {}
    for name in REPORTS:
        p = source/name
        require(sha256(p)==manifest["files"][name]["published_sha256"],
                f"Approved report changed: {name}")
        reports[name] = p.read_text(encoding="utf-8")
    return reports,manifest,sha256(source/"publication_manifest.json")


def pct(n,d):
    return f"{100*n/d:.2f}%" if d and n is not None else "not assessable"


def count_range(lo,hi):
    return "unresolved" if lo is None or hi is None else str(lo) if lo==hi else f"{lo}–{hi}"


def ratio_range(lo,hi,n):
    if lo is None or hi is None or not n:
        return "not assessable"
    return pct(lo,n) if lo==hi else f"{pct(lo,n)}–{pct(hi,n)}"


def table(headers,rows):
    return "\n".join(["| "+" | ".join(headers)+" |",
                      "| "+" | ".join("---" for _ in headers)+" |"]+
                     ["| "+" | ".join(str(x) for x in row)+" |" for row in rows])


def primary(m,label):
    return m["runs"][label]["scenarios"]["1.0"]


def main_observability(m):
    rows = []
    for label,d in m["runs"].items():
        s = primary(m,label)
        p = s["tolerances"]["original"]
        rows.append((label,d["sf_ok"],s["target_acks"],s["unique_local"],
                     count_range(p["min"],p["max"]),ratio_range(p["min"],p["max"],s["target_acks"]),
                     f"{p['matching']}/{p['assessed']}"))
    for label,p in m["pooled_replays"]["1.0"]["original"].items():
        rows.append((f"Replay {label} pooled","—",p["target_acks"],p["unique_local"],
                     count_range(p["min"],p["max"]),ratio_range(p["min"],p["max"],p["target_acks"]),
                     f"{p['matching']}/{p['assessed']}"))
    return table(("Run","SF OK ACKs","Target ACKs inside support","Unique local","Distinct chronological","Chronological coverage","Matching/assessed cycles"),rows)


def type_table(m):
    rows = []
    for label,p in m["pooled_replays"]["1.0"]["original"].items():
        for purpose,t in p["types"].items():
            rows.append((label,purpose,t["n"],t["local_matching"],pct(t["local_matching"],t["n"]),
                         t["required_on_paths"],t["optional_on_paths"]))
    return table(("Replay transport","SF purpose","Target ACKs","Local matches","Local coverage","Required on all paths","Optional on paths"),rows)


def stable_table(m):
    rows=[]
    for label,d in m["runs"].items():
        for key,name in (("controller_target","Integer controller target"),("ack","ACK-indicated SF")):
            v=d["stable"][key]
            def fmt(x):
                return f"{x:.3f}" if x is not None else "—"
            rows.append((label,name,f"{v['stable_intervals']}/{d['approved_intervals']}",
                         fmt(v["median_abs_error_hz"]),fmt(v["p95_abs_error_hz"]),fmt(v["max_abs_error_hz"])))
    return table(("Run","Stable reference","Eligible/all intervals","Median |error| Hz","P95 Hz","Max Hz"),rows)


def settled_table(m):
    rows=[]
    for label,d in m["runs"].items():
        s=primary(m,label)
        for boundary,v in s["settled"].items():
            rows.append((label,boundary,s["target_acks"],v["with_guard"],v["without_guard"]))
    return table(("Run","Next control boundary","Target ACKs","Matching with guard","Matching without guard"),rows)


def sensitivity_table(m):
    rows=[]
    for scale in SCALES:
        for tol in TOLERANCES:
            p=m["pooled_replays"][scale][tol]
            a,b=p["baseline"],p["shared"]
            rows.append((scale,tol,count_range(a["min"],a["max"]),count_range(b["min"],b["max"]),
                         f"{a['matching']+b['matching']}/{a['assessed']+b['assessed']}",
                         "; ".join(sorted(set(a["path_statuses"]+b["path_statuses"])))))
    return table(("Gray margin","Tolerance","Baseline distinct","Shared distinct","Matching/assessed cycles","Path status"),rows)


def timing_table(m):
    rows=[]
    for label,d in m["runs"].items():
        t=d["timing"]
        h=t["piecewise_holdout_rms_us"]
        clock=primary(m,label)["clock"]
        rows.append((label,t["gray_frames"],f"{t['affine_holdout_rms_us']:.2f}",
                     f"{h['single_frame']:.2f}",f"{h['1s']:.2f}",f"{h['2s']:.2f}",
                     f"{t['max_local_rate_deviation_ppm']:.1f}",
                     f"{clock['max_tested_host_envelope_width_ms']:.3f}"))
    return table(("Run","Gray frames","Affine 1-frame RMS µs","Piecewise 1-frame RMS µs",
                  "1 s RMS µs","2 s RMS µs","Local rate deviation ppm","Max tested host envelope ms"),rows)


def transport_table(m,detail=False):
    rows=[]
    for label in ("A1","B1","A2","B2"):
        d=m["replay"][label]
        def val(group,key,stat="median"):
            return d["latency_groups"][group]["metrics_ms"][key][stat]
        row=[label,d["sample_count_verified"],d["target_changes"],d["unreferenced_changes"],
             d["measured_subscription_opens"],f"{val('sc','request_to_terminal_ms'):.2f}",
             f"{val('sf_after_sc','request_to_terminal_ms'):.2f}",
             f"{val('sf_after_sf','request_to_terminal_ms'):.2f}"]
        if detail:
            row += [f"{val('sc','request_to_write_ms'):.2f}",
                    f"{val('sc','write_to_ack_ms'):.2f}",
                    f"{val('sf_after_sc','request_to_write_ms'):.2f}",
                    f"{val('sf_after_sc','write_to_ack_ms'):.2f}",
                    f"{d['delivery_lateness_ms']['p95']:.3f}"]
        rows.append(row)
    headers=["Run","Verified inputs","Target changes","Unreferenced changes","Measured opens",
             "SC median ms","SF after SC median ms","SF after SF median ms"]
    if detail:
        headers += ["SC req→write ms","SC write→ACK ms","SF after SC req→write ms","SF after SC write→ACK ms","Replay lateness P95 ms"]
    return table(headers,rows)


def transport_reduction(m):
    out=[]
    for a,b in (("A1","B1"),("A2","B2")):
        def metric(label,group):
            return m["replay"][label]["latency_groups"][group]["metrics_ms"]["request_to_terminal_ms"]["median"]
        out.append((a+"/"+b,100*(1-metric(b,"sc")/metric(a,"sc")),
                    100*(1-metric(b,"sf_after_sc")/metric(a,"sf_after_sc"))))
    return " and ".join(f"{a}: SC {sc:.1f}%, SF after SC {sf:.1f}%" for a,sc,sf in out)


def reason_table(m,key):
    p=m["pooled_replays"]["1.0"]["original"]
    a,b=p["baseline"][key],p["shared"][key]
    return table(("Classification","Baseline","Shared"),[(k,a.get(k,0),b.get(k,0)) for k in sorted(set(a)|set(b))])


def restart_table(m):
    rows=[]
    for label,p in m["pooled_replays"]["1.0"]["original"].items():
        t=p["types"]["restore"]
        med=p["no_pulse_window_median_ms"]
        rows.append((label,t["n"],t["local_matching"],p["new_restart_matches"],
                     p["restart_union_matches"],p["no_pulse_windows_count"],
                     f"{med:.2f}" if med is not None else "—",
                     p["no_pulse_shorter_than_period"]))
    return table(("Replay transport","Restore SF ACKs","Prior matches","New local matches","Union matches",
                  "No supported pulse episodes","Median host window ms","Window < one period"),rows)


def charge_table(m):
    rows=[]
    for label,values in m["charge"].items():
        for q,d in values.items():
            for kind,g in d["groups"].items():
                med=g["median"]
                rows.append((label,q,kind,f"{g['eligible']}/{g['candidate_count']}",
                             f"{med['negative_charge_nc']:.3f}",f"{med['positive_charge_nc']:.3f}",
                             f"{med['net_charge_nc']:.3f}",f"{med['negative_peak_v']:.1f}",
                             f"{med['positive_peak_v']:.1f}"))
    return table(("Run","Quiet threshold V","Detector group","Eligible/candidates","Q− median nC",
                  "Q+ median nC","Net median nC","Negative peak V","Positive peak V"),rows)


def charge_sensitivity(m):
    rows=[]
    for label,values in m["charge"].items():
        for q,d in values.items():
            for post,groups in d["windows"].items():
                rows.append((label,q,post,*(f"{groups[k]['median_net_nc']:.3f} ({groups[k]['eligible']})"
                            if groups[k]["median_net_nc"] is not None else "—" for k in ("accepted","rejected"))))
    return table(("Run","Quiet V","Post-onset µs","Biphasic net median nC (n)","Negative-only net median nC (n)"),rows)


def sources_table(m):
    rows=[]
    for label,run in RUNS.items():
        d=m["runs"][label]
        rows.append((label,run,d["approved_onsets"],d["approved_intervals"],d["excluded_negative_only"],
                     f"{d['sf_ok']}/{d['sf_requests']}",f"{d['sc_ok']}/{d['sc_requests']}"))
    return table(("Label","Recording ID","Approved pulses","Intervals","Excluded negative-only","SF OK/requested","SC OK/requested"),rows)


def summary_paragraph(m):
    p=m["pooled_replays"]["1.0"]["original"]
    a,b=p["baseline"],p["shared"]
    ordinary=b["types"]["ordinary"]
    return (f"Across the shared replay recordings, **{count_range(b['min'],b['max'])} of {b['target_acks']}** "
            f"target SF ACKs ({ratio_range(b['min'],b['max'],b['target_acks'])}) are represented in the assessed "
            f"physical output under the cycle-boundary/latest-pending-SF model and chronological attribution. "
            f"The corresponding baseline estimate is **{count_range(a['min'],a['max'])}/{a['target_acks']}** "
            f"({ratio_range(a['min'],a['max'],a['target_acks'])}). Ordinary shared SF updates have "
            f"{ordinary['local_matching']}/{ordinary['n']} local matching coverage ({pct(ordinary['local_matching'],ordinary['n'])}). "
            f"Across the four replays, {a['matching']+b['matching']}/{a['assessed']+b['assessed']} assessed periods "
            "match a candidate. These are model-conditioned observability estimates, not independently measured device execution or failure rates.")


def method_text():
    return ("A frequency selection is modeled at an approved negative pulse onset for the following "
            "cycle. Latest available SF wins; a subsequent SF within that cycle does not invalidate "
            "the period. Availability is assumed between host BLE write begin and embedded ACK receipt; "
            "the ACK does not establish the internal application instant. Candidate identities are "
            "enumerated independently of frequency error and constrained to nondecreasing chronological "
            "order. Each distinct live target ACK is counted once; repeated numeric values remain "
            "different command identities. The target denominator is successful SF ACKs with trace ACK "
            "inside Gray support, excluding muted-scheduler SF400 but including restore SF attempts. "
            "Initial START frequency is context, not a live SF ACK. SC transitions, unsupported boundaries "
            "and long pauses (>0.5 s) are excluded from primary cycle attribution. Matching tolerance is "
            "max(1 Hz, 2.5%); tighter tolerances and 1×/2×/3× timing margins are sensitivity tests. "
            "A path through individual boundary envelopes is not proof that one jointly feasible "
            "clock realization or one exact firmware history produced every period.")


def clock_text():
    return ("CH1 and CH2 share the scope clock. Hardware-timed Nano Gray START edges are logged by an "
            "ISR; its timestamp latency is not calibrated. Scope-to-Nano piecewise interpolation is "
            "evaluated by blocked held-out anchors, not selected by frequency agreement. Nano-to-PC "
            "four-timestamp USB exchanges constrain an affine slope/offset envelope without assuming "
            "symmetric USB transport. Widening by held-out Gray P95 is heuristic. No extrapolation outside "
            "Gray anchors is allowed. Local rate deviation still exceeds the 250 ppm qualification "
            "criterion; absolute cross-clock error and physical application latency remain uncalibrated. "
            "Physical periods themselves use only the scope clock.")


def charge_text(m):
    a,b=m["charge"]["A2"]["2.5"],m["charge"]["B2"]["2.5"]
    return (f"The 20 V/div repetition avoids storage-rail samples; rejected negative-only candidates "
            f"number {a['groups']['rejected']['candidate_count']} and {b['groups']['rejected']['candidate_count']}. "
            "For a nominal 100 kΩ resistor branch, integrate baseline-subtracted voltage divided by "
            "resistance from 100 µs before onset to the selected endpoint. Only quiet, nonoverlapping, "
            "non-rail windows enter summaries. The primary diagnostic uses 2.5 V quiet threshold and "
            "1 ms post-onset; 4 V and 0.55/1.5 ms test sensitivity. The negative-only finite-window net "
            "charge is negative in the candidate-group median. These are resistor-branch estimates, "
            "not total stimulator current, complete charge recovery, proof of safety, or a mechanism. "
            "The 2.5 ms spacing subset is consistent with the muted 400 Hz scheduler but does not "
            f"establish causation ({a['rejected_near_2p5ms']} baseline, {b['rejected_near_2p5ms']} shared "
            "rejected candidates within ±0.1 ms of that spacing).")


def evidence_index(m):
    rows=[]
    for label,run in RUNS.items():
        rows.append((label,f"`results/{run}/analysis/`"))
    return table(("Recording","Local evidence root"),rows)+"\n\n"+(
        "Per-run modules: `timing_refinement`, `piecewise_command_analysis_integer`, "
        "`refined_ack_observability_boundaries`, `cycle_boundary_observability_chronological`, "
        "`restart_observability`. B1/B2 also contain `ble_ab_comparison`; A2/B2 contain "
        "`transient_charge_diagnostic_quiet_2p5v` and `transient_charge_diagnostic_quiet_4p0v`. "
        "The reference retains all historical extended audits and `command_chain_diagnostic`. "
        "The generated `report_revision_metrics.json` records the numerical aggregation, "
        "source fingerprints and each input summary/table hash. The approved publication is unchanged.")


def figure(name,caption):
    return f"![{name}]({name}.png)\n\n*{caption}*"


def render_condensed(m,old):
    ref=m["runs"]["Reference"]
    chain=m["reference_command_chain"]
    p=m["pooled_replays"]["1.0"]["original"]
    a,b=p["baseline"],p["shared"]
    lines=["# Matrix32 tactile-to-electrical stimulation: consolidated experiment report",
           "**Review revision — not yet approved.** Reference recording: 24 November 2026; controlled replay validation: 26 November 2026.",
           "> **Principal conclusion.** "+summary_paragraph(m),
           "## At a glance: research questions and evidence",
           table(("Question","Result"),[
               ("Reference SF/SC protocol delivery",f"{ref['sf_ok']}/{ref['sf_requests']} SF; {ref['sc_ok']}/{ref['sc_requests']} SC OK"),
               ("Reference settled frequency",f"{ref['stable']['ack']['stable_intervals']} selected intervals; median |error| {ref['stable']['ack']['median_abs_error_hz']:.3f} Hz"),
               ("Shared replay physical observability",f"{count_range(b['min'],b['max'])}/{b['target_acks']} under model; {b['matching']}/{b['assessed']} assessed periods match"),
               ("BLE transport",transport_reduction(m)),
               ("Electrical safety","Not established; verified dummy-load bench only")]),
           "## Experiment and measurement windows",historical_body(old,"Experiment and measurement windows"),
           "The following original measurement description and figures concern the reference run. Four independent scope records replayed the same recorded input window; they are not additional portions of the original host session.",
           sources_table(m),
           f"Replay input: {m['replay_source']['sample_count']} snapshots over {m['replay_source']['duration_s']} s, identical verified source values and schedule. The first pair used 10 V/div and the second 20 V/div on CH1. Each run is a separate dummy-load recording.",
           "## 1. Frequency requests and physical observability",main_observability(m),
           figure("observability","Target ACKs counted once; solid bars show the lower chronological count, hatched extension the optional count, and remaining targets lack a selected matching witness. This is not a success/failure rate."),
           method_text(),
           type_table(m),
           "## 2. Settled-state electrical frequency agreement",
           f"The previously approved affine-map analysis reported 85 selected intervals (median 0.039 Hz; P95 0.094 Hz; max 0.155 Hz). That is retained as a historical result, not pooled with the refined analysis. The revised calculation uses actual integer controller targets and stable ACK states within the piecewise/USB envelopes.",
           stable_table(m),
           figure("settled_accuracy","Absolute frequency differences in eligible subsets only; median and P95 are shown separately for integer controller target and acknowledged SF. The subset sizes differ."),
           "The controller-target and ACK subsets differ. A target error includes tracking delay and supersession, not necessarily inaccurate physical frequency generation.",
           "## 3. Rapid frequency updates and timing uncertainty",clock_text(),
           f"Reference and replay results are separately computed from signed physical periods. The four-replay primary chronological estimates are {count_range(a['min'],a['max'])} baseline and {count_range(b['min'],b['max'])} shared; see the detailed report for all timing/tolerance scenarios and unmatched categories.",
           "## 4. Host scheduling and BLE communication",historical_body(old,"4. Host scheduling and BLE communication"),
           "The preceding host-cadence result is from the original reference session. In the controlled paired replays, actual input values/schedule are verified; changed target selection and output are outcomes, not fixed command streams.",
           transport_table(m),
           figure("transport_latency","Median request-to-trace-terminal durations for matched replay conditions; latency is host protocol timing, not electrical application."),
           "The shared implementation reduces subscription overhead and is now the sole live SF/SC transport. The 7.5 ms inter-command guard and zero-current mute ordering remain unchanged; no firmware change was made. No demonstrated degradation in ordinary-frequency matching coverage is established from these two descriptive repetitions.",
           "## 5. Current shutdown, restart and incomplete waveforms",historical_body(old,"5. Current shutdown, restart and incomplete waveforms"),
           restart_table(m),
           "First-pulse alternatives are conditional on SC availability; they may include residual pulses. The reported host window is not a measured current-on duration. Local restart matches are not added to chronological counts without joint attribution.",
           charge_text(m),charge_table(m),
           "## Conclusions and remaining questions",summary_paragraph(m),
           "The shared persistent session is supported for the verified dummy-load research setup. Insufficient observation time, current transitions and possible between-cycle replacement explain much of the missing evidence; an unmatched ACK is not a failed command. Exact physical application latency, internally superseded command identities, complete current/charge balance and safety remain unproven. No additional hardware experiment is required to answer the present model-conditioned observability question.",
           "## Detailed evidence, limitations and reproducibility",
           "[Detailed technical consolidation](extended_analysis_technical_report.md). "+evidence_index(m)]
    return "\n\n".join(lines)+"\n"


def render_detailed(m,old):
    ref=m["runs"]["Reference"]
    chain=m["reference_command_chain"]
    coverage=chain["sample_coverage_counts"]
    count_rows=[(k,v) for k,v in coverage.items()]
    lines=["# Matrix32 — consolidated experimental analysis",
           "**Review revision — not yet approved.** Signed reference and four independent replay recordings. Historical approved analyses are preserved; revised calculations do not redetect pulses or recalibrate clocks.",
           "## Requested → ACKed → physically observed (executive scorecard)",summary_paragraph(m),
           sources_table(m),main_observability(m),
           figure("observability","Chronological distinct ACK count bounds for the reference and each replay. Target counts exclude muted-scheduler SF commands and outside-support ACKs."),
           type_table(m),
           "## 1. Approved steady-state frequency and per-ACK observability",method_text(),
           "The earlier approved reference analysis reported 59 in-scope ACKs with isolated settled evidence and 85 eligible intervals (85 within tolerance). These are historical affine-map/settled-window quantities, not the revised denominator or execution count. The revised settled reference uses integer controller targets rather than raw float MCU rates.",
           stable_table(m),
           figure("settled_accuracy","Median and P95 absolute error for stable integer target and ACK states across the signed records. Error comparisons use different eligible subsets."),
           "Historical original reference per-ACK overview (unchanged figure; provisional affine placement, retained for audit history):",
           "![Original per-ACK overview](../ack_physical_observability/command_physical_timeline.png)",
           "## 2. Rapid frequency transitions and clock-model sensitivity",clock_text(),timing_table(m),
           "### Conservative settled-window comparison",
           "Request, actual validated BLE write and embedded ACK endpoints are tested. The next-write boundary is physically better motivated than request; the next-ACK endpoint may include an already-available newer command. The previous-cycle guard is a settling assumption, not a measured bound. Missing BLE timing falls back to request.",
           settled_table(m),
           "### Cycle-boundary attribution and chronology",
           "Tolerance scenarios are original max(1 Hz, 2.5%), tight max(0.5 Hz, 1%), very tight max(0.25 Hz, 0.5%). A path through matching periods only is explicitly conditional if an assessed period is incompatible.",
           sensitivity_table(m),
           "Unmatched local target-command evidence (1× original):",reason_table(m,"unmatched_reasons"),
           "Possible and strict between-cycle replacement brackets are candidate explanations, may overlap these classifications, and do not demonstrate an internal discard. The full per-target CSV retains identity, candidate intervals, error, and successor timestamps.",
           "### Historical affine-map transition analysis (reference only)",
           "The original transition diagnostic is retained for reproducibility, not used as the primary physical attribution model. The following values and plots are unchanged historical results.",
           historical_body(old,"2. Rapid frequency transitions"),
           "## 3. Current shutdown and restart",historical_body(old,"3. Current shutdown and restart"),
           "### Restart-specific analysis",restart_table(m),reason_table(m,"restart_unmatched_reasons"),
           "Positive SF restore attempts can be replaced before actual SC restoration, or a newer SF can be the only candidate compatible with the first observable cycle. If no supported pulse occurs before the next SC write, STOP or support edge, electrical restart cannot be inferred. The first-cycle analysis relaxes only the associated SC overlap and does not automatically change chronological bounds.",
           "### Resistor-branch transient charge",charge_text(m),charge_table(m),charge_sensitivity(m),
           "## 4. Host command timing and cycle opportunities",historical_body(old,"4. Host command timing and cycle opportunities"),
           "### Reference command chain (integer target reconstruction)",
           f"Taxel {chain['taxel']}, {chain['encoding']}; {chain['host_session_samples']} snapshots. Controller conversion is round-half-up to integer, below 2 Hz mute, above 400 Hz clamp. Exact source-sample identity and request/terminal pairing are used; unreferenced changed targets are not failed commands.",
           table(("Coverage class","Count"),count_rows),
           f"Stable target/ACK disagreements: {chain['physical_target_ack_disagreements']} of {chain['physical_both_stable_intervals']} intervals where both states are stable. Different command-state timing is a plausible cause, not evidence of a Matrix32 frequency error.",
           "### Controlled replay and transport comparison",
           f"Same {m['replay_source']['sample_count']} source snapshots and {m['replay_source']['duration_s']} s half-open replay window in all four runs. Initial positive snapshot held until START and settling; preroll is excluded from source replay counts. Transport is the intended A/B variable; repeat CH1 scale changed for rail-free morphology.",
           transport_table(m,detail=True),figure("transport_latency","Host request-to-terminal medians by command context for the two A/B pairs. Not physical latency."),
           f"SC and SF-after-SC relative median reductions: {transport_reduction(m)}. The shared SF/SC ACK session avoids repeated subscription setup, retains mandatory ACKs, one outstanding exchange, failure poisoning, independent STOP and the 7.5 ms guard. Firmware, mute/resume ordering and the tactile conversion policy are not modified.",
           "## Confidence and remaining limitations",
           "Direct measurements: signed scope-clock periods and morphology; same-host request/write/ACK timing; replay delivery values and timing. Conditional evidence: stable target/ACK attribution, chronological distinct counts and restart candidates. Unknowns: USB asymmetry, Nano ISR delay, exact scheduler availability/application, unobserved internal boundaries, precise physical STOP and complete electrical charge balance. Neither the bench test nor the transport change is a safety qualification. Pooled counts are descriptive sums of two repeated replays per transport, not independent population statistics.",
           "## Reproducibility",evidence_index(m)]
    return "\n\n".join(lines)+"\n"


def make_figures(dest,m):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels=list(m["runs"])
    n=np.array([primary(m,k)["target_acks"] for k in labels])
    lo=[primary(m,k)["tolerances"]["original"]["min"] for k in labels]
    hi=[primary(m,k)["tolerances"]["original"]["max"] for k in labels]
    require(all(x is not None for x in lo+hi),"Primary chronological path unresolved: review diagnostic before report")
    lo,hi=np.array(lo),np.array(hi)
    fig,ax=plt.subplots(figsize=(8,4.4))
    x=np.arange(len(labels))
    ax.bar(x,lo,label="Minimum distinct matching",color="#277da1")
    ax.bar(x,hi-lo,bottom=lo,label="Additional possible count",color="#90be6d",hatch="//")
    ax.bar(x,n-hi,bottom=hi,label="Remaining targets",color="#d9d9d9")
    for i,(a,b,c) in enumerate(zip(lo,hi,n)):
        ax.text(i,c+3,count_range(a,b)+f"/{c}",ha="center",fontsize=9)
    ax.set_ylim(0,max(n)*1.15); ax.set_xticks(x,labels)
    ax.set_ylabel("Target SF ACKs");ax.set_title("Model-conditioned physical observability")
    ax.legend(loc="upper center",bbox_to_anchor=(.5,-.12),ncol=3,fontsize=8)
    fig.tight_layout();fig.savefig(dest/"observability.png",dpi=160,bbox_inches="tight");plt.close(fig)

    fig,ax=plt.subplots(figsize=(8,4.4))
    for key,label,marker in (("controller_target","Controller target","o"),("ack","ACK state","s")):
        offset=-.13 if key=="controller_target" else .13
        med=[m["runs"][k]["stable"][key]["median_abs_error_hz"] for k in labels]
        p95=[m["runs"][k]["stable"][key]["p95_abs_error_hz"] for k in labels]
        ax.plot(x+offset,med,marker=marker,linestyle="none",label=label+" median")
        ax.plot(x+offset,p95,marker=marker,linestyle="none",markerfacecolor="none",label=label+" P95")
    ax.set_yscale("log");ax.set_xticks(x,labels);ax.set_ylabel("Absolute frequency difference (Hz)")
    ax.set_title("Stable-state frequency agreement (selected intervals)")
    ax.grid(axis="y",alpha=.3);ax.legend(fontsize=8,ncol=2);fig.tight_layout()
    fig.savefig(dest/"settled_accuracy.png",dpi=160);plt.close(fig)

    labels=["A1","B1","A2","B2"]; x=np.arange(4)
    fig,ax=plt.subplots(figsize=(8,4.4))
    for j,(key,label) in enumerate((("sc","SC"),("sf_after_sc","SF after SC"),("sf_after_sf","SF after SF"))):
        v=[m["replay"][k]["latency_groups"][key]["metrics_ms"]["request_to_terminal_ms"]["median"] for k in labels]
        ax.bar(x+(j-1)*.25,v,width=.24,label=label)
    ax.set_xticks(x,labels);ax.set_ylabel("Median request-to-terminal (ms)")
    ax.set_title("Host protocol timing in controlled replays");ax.legend()
    fig.tight_layout();fig.savefig(dest/"transport_latency.png",dpi=160);plt.close(fig)


def build(recordings_dir):
    recordings_dir=Path(recordings_dir).resolve()
    analysis=analysis_root(recordings_dir/REFERENCE,legacy_ok=False)
    dest=ensure_derived_destination(analysis/REVISION_FOLDER,*(recordings_dir/run for run in RUNS.values()))
    if dest.exists():
        raise FileExistsError(f"Review revision already exists: {dest}; preserve it before rebuilding")
    m=collect(recordings_dir)
    old,publication,publication_hash=load_approved_reference(m,analysis)
    reports={REPORTS[0]:render_condensed(m,old[REPORTS[0]]),
             REPORTS[1]:render_detailed(m,old[REPORTS[1]])}
    # Retain the exact bytes of approved reference figures; no extra copies in
    # results. Fail if a previous regeneration changed a reused figure.
    reused={}
    for text in reports.values():
        for match in IMAGE_RE.finditer(text):
            rel=match.group(2)
            if rel.startswith("../"):
                original=rel[3:]
                expected=publication["files"][original]["source_sha256"]
                require(sha256(analysis/original)==expected,f"Approved figure changed: {original}")
                reused[original]=expected
    dest.mkdir(parents=True,exist_ok=False)
    make_figures(dest,m)
    for name,text in reports.items():
        for match in IMAGE_RE.finditer(text):
            require((dest/match.group(2)).is_file(),f"Missing report figure: {match.group(2)}")
        (dest/name).write_text(text,encoding="utf-8")
    atomic_json(dest/"report_revision_metrics.json",m)
    atomic_json(dest/"report_revision_manifest.json",{
        "schema":"matrix32_report_revision_v1","reference_run":REFERENCE,
        "source_publication":APPROVED_TAG,"source_publication_manifest_sha256":publication_hash,
        "raw_metadata_sha256":m["raw_metadata_sha256"],"signed_onsets_sha256":m["signed_onsets_sha256"],
        "metrics_sha256":sha256(dest/"report_revision_metrics.json"),
        "reused_reference_figures":reused,
        "files_sha256":{p.name:sha256(p) for p in dest.iterdir() if p.is_file()},
        "review_required":True,"approved":False,"raw_data_copied":False})
    print(f"[REVIEW REPORTS] {dest}")
    return dest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--recordings-dir",type=Path,default=REPO_ROOT/"data"/"recordings")
    build(p.parse_args(argv).recordings_dir)


if __name__=="__main__":
    main()
