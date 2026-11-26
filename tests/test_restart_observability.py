"""Hardware-free restart attribution checks."""
import unittest

import numpy as np

from experiment_analysis.audit_cycle_boundary_observability import (
    prepare_commands, analyze_cycles, classify_commands,
)
from experiment_analysis.audit_restart_observability import (
    restart_sequences, first_pulse_indices, analyze_restart,
    classify_restore_attempts, summarize,
)


def event(kind,ms,**fields):
    return {"event":kind,"pc_monotonic_ns":round(ms*1e6),**fields}


def transaction(family,request,write,ack,value,**flags):
    key = "frequency_hz" if family=="sf" else "current_command"
    return [event(family+"_requested",request,**{key:value},**flags),
            event(family+"_ack",ack+.1,**{key:value},status="OK",
                  evidence={"write_begin_monotonic_s":write/1000,
                            "ack_monotonic_s":ack/1000},**flags)]


def base():
    events = [event("start_write_completed",0,frequency_hz=100)]
    events += transaction("sc",15,16,18,0)
    events += transaction("sf",20,21,23,400,zero_current_workaround=True)
    return events


def evaluate(events,ms):
    model = prepare_commands(events)
    times = np.array(ms,dtype=float)/1000
    ids = [f"P{i}" for i in range(len(times))]
    mapped = {"lower":times*1e9-100_000,"upper":times*1e9+100_000,
              "valid":np.ones(len(times),dtype=bool),
              "interval_valid":np.ones(len(times)-1,dtype=bool)}
    support = (0.,1e6,999e6,1000e6)
    cycles,candidates = analyze_cycles(model,times,ids,mapped,support)
    commands = classify_commands(model,cycles,candidates,times,mapped,support)
    restarts,detail = [],[]
    for sequence in restart_sequences(model):
        r,d = analyze_restart(model,sequence,times,ids,mapped,support,cycles)
        restarts.append(r)
        detail.extend(d)
    attempts = classify_restore_attempts(model,commands,restarts)
    return restarts,detail,attempts


class RestartTests(unittest.TestCase):
    def test_restore_overlap_recovers_previously_excluded_cycle(self):
        events = base()
        events += transaction("sf",40,41,43,80,restoring_current=True)
        events += transaction("sc",49,50,55,50)
        # First possible onset is during SC restore. The following period
        # matches SF80, but the next possible first onset has no following
        # pulse in the capture, so the result must stay conditional.
        r,d,a = evaluate(events,[10,52,64.5])
        self.assertEqual(r[0]["classification"],"RESTORE_MATCH_POSSIBLE")
        self.assertEqual(d[0]["original_cycle_classification"],"SC_TRANSITION")
        self.assertTrue(d[0]["recovered_restore_sc_overlap"])
        self.assertFalse(a[0]["prior_cycle_match"])
        self.assertTrue(a[0]["new_local_matching_evidence"])
        self.assertEqual(summarize(r,d,a)["inside_union_matching"],1)

    def test_replaced_before_sc_restore(self):
        events = base()
        events += transaction("sf",30,31,33,60,restoring_current=True)
        events += transaction("sf",35,36,38,400,zero_current_workaround=True)
        events += transaction("sf",40,41,43,80,restoring_current=True)
        events += transaction("sc",49,50,55,50)
        r,d,a = evaluate(events,[10,60,72.5,85])
        self.assertEqual(a[0]["restart_reason"],"REPLACED_BEFORE_SC_RESTORE")
        self.assertFalse(a[0]["any_local_matching_evidence"])
        self.assertEqual(r[0]["classification"],"RESTORE_MATCH_ALL_FIRST_CYCLE_CANDIDATES")
        self.assertTrue(a[1]["prior_cycle_match"])
        self.assertFalse(a[1]["new_local_matching_evidence"])

    def test_newer_sf_before_first_observed_pulse(self):
        events = base()
        events += transaction("sf",40,41,43,80,restoring_current=True)
        events += transaction("sc",49,50,55,50)
        events += transaction("sf",57,58,60,100)
        r,d,a = evaluate(events,[10,65,75,85])
        self.assertEqual(r[0]["classification"],"NEWER_SF_ONLY_AT_ALL_FIRST_CYCLE_CANDIDATES")
        self.assertFalse(a[0]["any_local_matching_evidence"])
        self.assertTrue(d[0]["newer_sf_matches"])

    def test_next_mute_censors_second_pulse(self):
        events = base()
        events += transaction("sf",40,41,43,80,restoring_current=True)
        events += transaction("sc",49,50,55,50)
        events += transaction("sc",65,66,68,0)
        r,d,a = evaluate(events,[10,60,72.5,85])
        self.assertEqual(r[0]["classification"],"FIRST_CYCLE_UNRESOLVED")
        self.assertEqual(d[0]["classification"],"SECOND_PULSE_AT_NEXT_CONTROL_OR_SUPPORT_EDGE")

    def test_first_candidate_list_stops_at_definite_post_ack_onset(self):
        mapped = {"lower":np.array([10.,49.,52.,54.9,60.,70.]),
                  "upper":np.array([11.,50.,53.,55.1,61.,71.]),
                  "valid":np.ones(6,dtype=bool)}
        self.assertEqual(first_pulse_indices(mapped,50.,55.,100.),[1,2,3,4])


if __name__=="__main__":
    unittest.main()
