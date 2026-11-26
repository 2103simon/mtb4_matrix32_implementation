"""Synthetic, no-hardware per-command observability checks."""
import unittest
import numpy as np

from experiment_analysis.audit_refined_ack_observability import (
    assess_window, classify, summarize, longest_consecutive,
    control_boundaries, next_boundary, transition_periods,

)


def ev(kind, ms, **fields):
    return {"event":kind,"pc_monotonic_ns":int(ms*1e6),**fields}


def sf(request, ack, hz, **fields):
    return [ev("sf_requested",request,frequency_hz=hz,**fields),
            ev("sf_ack",ack,frequency_hz=hz,status="OK",**fields)]


def sc(request, ack, current):
    return [ev("sc_requested",request,current_command=current),
            ev("sc_ack",ack,current_command=current,status="OK")]


def evidence(ms, uncertainty=.1):
    t=np.asarray(ms,dtype=float)/1000
    n=t*1e9
    return t, [f"P{i}" for i in range(len(t))], {
        "lower":n-uncertainty*1e6,"upper":n+uncertainty*1e6,
        "interval_valid":np.ones(len(t)-1,dtype=bool),
    }


class RefinedObservabilityTests(unittest.TestCase):
    def test_guard_and_request_boundary(self):
        t,_,m=evidence([20,30,40,50,60,70])
        w=assess_window(np.diff(t),m["lower"],m["upper"],
                        m["interval_valid"],20e6,60e6,100, .01)
        self.assertEqual(w["eligible"],[2])
        self.assertEqual(w["matching"],[2])
        self.assertEqual(w["relaxed_matching"],[1,2])

    def test_restore_association_and_scheduler_exclusion(self):
        events=[ev("start_write_completed",0,frequency_hz=100)]
        events+=sf(5,10,100)
        events+=sc(55,60,0)
        events+=sf(65,70,400,zero_current_workaround=True)
        events+=sf(100,105,100,restoring_current=True)
        events+=sc(110,115,50)
        events+=sf(175,180,90)
        t,ids,m=evidence([25,35,45,130,140,150,160,170])
        rows=classify(events,t,ids,m,(0.,1e6,199e6,200e6))
        self.assertEqual(len(rows),4)
        self.assertEqual(rows[0]["matching_periods"],2)
        self.assertEqual(rows[1]["classification"],"MUTED_SCHEDULER_NO_ACTIVE_WINDOW")
        self.assertEqual(rows[2]["purpose"],"restore")
        self.assertEqual(rows[2]["matching_periods"],4)
        self.assertEqual(rows[2]["max_consecutive_matching_periods"],4)
        self.assertEqual(rows[3]["matching_periods"],0)
        s=summarize(rows)
        self.assertEqual(s["target_sf_inside"]["n"],3)
        self.assertEqual(s["target_sf_inside"]["two_or_more_consecutive_matching"],2)

    def test_repeated_frequency_requests_are_distinct(self):
        events=[ev("start_write_completed",0,frequency_hz=100)]
        events+=sf(5,10,100)+sf(45,50,100)
        t,ids,m=evidence([25,35,45,55,65,75,85])
        rows=classify(events,t,ids,m,(0.,1e6,99e6,100e6))
        self.assertEqual(len(rows),2)
        self.assertEqual([r["matching_periods"] for r in rows],[1,2])

    def test_scope_edge_and_consecutive(self):
        events=[ev("start_write_completed",0,frequency_hz=100)]
        events+=sf(5,10,100)
        t,ids,m=evidence([25,35,45])
        rows=classify(events,t,ids,m,(9e6,11e6,99e6,100e6))
        self.assertEqual(rows[0]["classification"],"GRAY_EDGE_UNCERTAIN")
        self.assertEqual(summarize(rows)["target_sf_inside"]["n"],0)
        self.assertEqual(longest_consecutive([0,2,3,5]),2)
    def test_validated_write_ack_boundaries_extend_only_next_command(self):
        events=[ev("start_write_completed",0,frequency_hz=100)]
        events+=sf(5,10,100)
        events+=sf(45,70,90)
        events[-1]["evidence"]={"write_begin_monotonic_s":.060,
                                "ack_monotonic_s":.069}
        t,ids,m=evidence([25,35,45,55,65,75,85])
        support=(0.,1e6,99e6,100e6)
        results=[classify(events,t,ids,m,support,boundary=b)[0]
                 for b in ("request","write","ack")]
        self.assertEqual([r["matching_periods"] for r in results],[1,3,4])
        # Missing/invalid timing falls back to request, never to trace ACK.
        events[-1]["evidence"]["write_begin_monotonic_s"]=.040
        r=classify(events,t,ids,m,support,boundary="write")[0]
        self.assertEqual(r["matching_periods"],1)
        self.assertIn("request_missing_or_invalid_ble_timing",r["active_windows_json"])

    def test_stop_request_caps_extended_boundary(self):
        events=[ev("sf_requested",45,frequency_hz=90),
                ev("stop_requested",58)]
        tr=[{"family":"sf","request_ns":45_000_000,"success":True,
             "embedded_timing_valid":True,"write_begin_ns":60_000_000,
             "ack_ns":69_000_000}]
        controls=control_boundaries(events,tr,"ack")
        self.assertEqual(next_boundary(controls,10_000_000,100_000_000),
                         (58_000_000,58_000_000,"stop_requested","request"))

    def test_transition_intersections_not_unique_witnesses(self):
        events=[ev("start_write_completed",0,frequency_hz=100)]
        events+=sf(45,70,90)
        t,ids,m=evidence([45,55,65,75,85])
        tr=[{"family":"sf","request_ns":45_000_000,"terminal_ns":70_000_000,
             "success":True,"embedded_timing_valid":True,
             "write_begin_ns":60_000_000,"ack_ns":69_000_000}]
        rows=transition_periods(events,t,ids,m,tr)
        self.assertEqual([(r["start_id"],r["end_id"]) for r in rows],
                         [("P1","P2"),("P2","P3")])
        self.assertTrue(all(r["compatible_previous"] for r in rows))
        self.assertTrue(all(not r["compatible_next"] for r in rows))
        self.assertTrue(all(not r["unique_application_proven"] for r in rows))


if __name__=="__main__":
    unittest.main()
