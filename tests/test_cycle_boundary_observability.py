"""Synthetic no-hardware tests of cycle-boundary attribution."""
import unittest

import numpy as np

from experiment_analysis.audit_cycle_boundary_observability import (
    prepare_commands, boundary_states, analyze_cycles, classify_commands, summarize,
)


def event(kind, ms, **fields):
    return {"event":kind,"pc_monotonic_ns":round(ms*1e6),**fields}


def transaction(family, request, write, ack, value, **flags):
    key = "frequency_hz" if family=="sf" else "current_command"
    return [event(family+"_requested",request,**{key:value},**flags),
            event(family+"_ack",ack+.1,**{key:value},status="OK",
                  evidence={"write_begin_monotonic_s":write/1000,
                            "ack_monotonic_s":ack/1000},**flags)]


def analyze(events, ms):
    model = prepare_commands(events)
    times = np.array(ms,dtype=float)/1000
    ids = [f"P{i}" for i in range(len(times))]
    mapped = {"lower":times*1e9-100_000,"upper":times*1e9+100_000,
              "valid":np.ones(len(times),dtype=bool),
              "interval_valid":np.ones(len(times)-1,dtype=bool)}
    support = (0.,1e6,999e6,1000e6)
    cycles,candidates = analyze_cycles(model,times,ids,mapped,support)
    commands = classify_commands(model,cycles,candidates,times,mapped,support)
    return cycles,commands


class CycleBoundaryTests(unittest.TestCase):
    def test_commands_inside_cycle_do_not_invalidate_prior_frequency(self):
        events = [event("start_write_completed",0,frequency_hz=20)]
        events += transaction("sf",20,21,25,30)
        events += transaction("sf",30,31,35,60)
        events += transaction("sf",40,41,45,80)
        # At 60 ms latest SF is 80; the next command during its cycle
        # must not invalidate the 12.5 ms period.
        events += transaction("sf",64,65,69,100)
        cycles,commands = analyze(events,[10,60,72.5,82.5])
        self.assertEqual([r["classification"] for r in cycles],
                         ["UNIQUE_INITIAL_MATCH","UNIQUE_SF_MATCH","UNIQUE_SF_MATCH"])
        self.assertEqual([r["classification"] for r in commands],
                         ["SUPERSEDED_BETWEEN_OBSERVED_BOUNDARIES"]*2+
                         ["UNIQUE_MATCHING_CYCLE"]*2)
        self.assertEqual(summarize(cycles,commands)["target_unique_matching"],2)

    def test_write_ack_overlap_keeps_both_identities(self):
        events = [event("start_write_completed",0,frequency_hz=100)]
        events += transaction("sf",15,16,25,80)
        model = prepare_commands(events)
        self.assertEqual(boundary_states(model,19.9e6,20.1e6),
                         [(None,100,True),(0,80,True)])
        cycles,commands = analyze(events,[20,32.5,45])
        self.assertEqual(cycles[0]["classification"],"SOME_CANDIDATES_MATCH")
        self.assertEqual(cycles[1]["classification"],"UNIQUE_SF_MATCH")
        self.assertEqual(commands[0]["possible_matching_cycles"],2)
        self.assertEqual(commands[0]["unique_matching_cycles"],1)

    def test_same_frequency_different_identity_remains_ambiguous(self):
        events = [event("start_write_completed",0,frequency_hz=100)]
        events += transaction("sf",5,6,8,100)
        events += transaction("sf",15,16,25,100)
        cycles,commands = analyze(events,[20,30])
        self.assertEqual(cycles[0]["classification"],
                         "ALL_CANDIDATES_MATCH_IDENTITY_AMBIGUOUS")
        self.assertEqual([r["classification"] for r in commands],
                         ["AMBIGUOUS_MATCHING_CANDIDATE"]*2)

    def test_mute_and_restore_not_supersession(self):
        events = [event("start_write_completed",0,frequency_hz=100)]
        events += transaction("sc",15,16,18,0)
        events += transaction("sf",20,21,23,400,zero_current_workaround=True)
        events += transaction("sf",45,46,48,80,restoring_current=True)
        events += transaction("sc",50,51,53,50)
        cycles,commands = analyze(events,[10,20,60,72.5])
        self.assertEqual([r["classification"] for r in cycles],
                         ["SC_TRANSITION","SC_TRANSITION","UNIQUE_SF_MATCH"])
        self.assertEqual([r["classification"] for r in commands],
                         ["MUTED_SCHEDULER","UNIQUE_MATCHING_CYCLE"])

    def test_nonmatching_period_and_invalid_timing(self):
        events = [event("start_write_completed",0,frequency_hz=100)]
        events += transaction("sf",5,6,8,80)
        cycles,commands = analyze(events,[20,30])
        self.assertEqual(cycles[0]["classification"],"NO_CANDIDATE_MATCH")
        self.assertEqual(commands[0]["classification"],"CANDIDATE_WITHOUT_MATCH")
        events[-1]["evidence"]["write_begin_monotonic_s"]=.001
        with self.assertRaisesRegex(ValueError,"invalid embedded"):
            prepare_commands(events)


if __name__=="__main__":
    unittest.main()
