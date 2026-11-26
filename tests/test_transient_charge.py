"""No-hardware tests for finite-window resistor-branch integration."""
import unittest

import numpy as np

from oscilloscope.analyze_transient_charge import integral, measure


class ChargeTests(unittest.TestCase):
    def test_linear_sign_crossing(self):
        result = integral(np.array([0., .001]), np.array([-10., 10.]), 100000.)
        self.assertAlmostEqual(result["negative_charge_nc"], -25.)
        self.assertAlmostEqual(result["positive_charge_nc"], 25.)
        self.assertAlmostEqual(result["net_charge_nc"], 0.)

    def test_asymmetric_sign_crossing(self):
        result = integral(np.array([0., .001]), np.array([20., -10.]), 100000.)
        self.assertAlmostEqual(result["negative_charge_nc"], -100/6)
        self.assertAlmostEqual(result["positive_charge_nc"], 200/3)
        self.assertAlmostEqual(result["net_charge_nc"], 50.)

    def test_baseline_removed_and_rail_rejected(self):
        t = np.arange(-.001, .002, .000002)
        v = np.full_like(t, 2.)
        v[(t >= 0) & (t < .0001)] -= 10
        raw = np.zeros_like(t)
        result = measure(t, v, raw, 0., .001, 100000.)
        self.assertEqual(result["status"], "OK")
        self.assertAlmostEqual(result["baseline_v"], 2.)
        self.assertAlmostEqual(result["negative_charge_nc"], -10., delta=.21)
        self.assertAlmostEqual(result["positive_charge_nc"], 0.)
        raw[np.argmin(v)] = -32767
        self.assertEqual(measure(t,v,raw,0.,.001,100000.)["status"], "STORAGE_RAIL")

    def test_boundary_neighbor_and_nonquiet_baseline(self):
        t = np.arange(-.001, .002, .000002)
        v = np.zeros_like(t)
        raw = np.zeros_like(t)
        self.assertEqual(measure(t,v,raw,-.0009,.001,100000.)["status"],"BOUNDARY")
        self.assertEqual(measure(t,v,raw,0.,.001,100000.,
                                 neighbor_times=[.0002])["status"],"NEIGHBOR_OVERLAP")
        v[np.argmin(abs(t+.0003))] = 10.
        self.assertEqual(measure(t,v,raw,0.,.001,100000.)["status"],"NONQUIET_BASELINE")

    def test_tail_flag(self):
        t = np.arange(-.001, .002, .000002)
        v = np.zeros_like(t)
        v[t >= 0] = -2.
        row = measure(t,v,np.zeros_like(t),0.,.001,100000.)
        self.assertEqual(row["status"],"END_NOT_QUIET")


if __name__ == "__main__":
    unittest.main()
