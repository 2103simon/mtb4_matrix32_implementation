"""Hardware-free contract tests for the integrated bench recorder."""
import io
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "oscilloscope"))
from record_integrated_matrix32 import main, parser, plan, StimulationChild
from record_scope import configure


class IntegratedBenchTests(unittest.TestCase):
    def test_default_dry_run_no_device_access(self):
        with patch("record_integrated_matrix32.run",
                   side_effect=AssertionError("Devices opened")):
            with redirect_stdout(io.StringIO()):
                result = main([])
        self.assertEqual(result["channels"], [1, 2])
        self.assertEqual(result["nominal_window_s"], 20)
        self.assertFalse(result["matrix32_armed"])
        self.assertEqual(result["trigger_position_pct"], 0)
        self.assertEqual(result["epoch_sync_count"], 30)

    def test_explicit_two_gate_arming(self):
        for argv in (["--arm"], ["--arm", "--run"],
                     ["--dummy-load-confirmed", "--run"]):
            with self.subTest(argv=argv):
                p = parser()
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    plan(p.parse_args(argv), p)
        p = parser()
        data = plan(p.parse_args(["--arm", "--run",
                                   "--dummy-load-confirmed"]), p)
        self.assertTrue(data["matrix32_armed"])

    def test_scope_ch1_matrix32_ch2_nano(self):
        p = parser()
        args = p.parse_args([])
        plan(args, p)
        with patch("record_scope.write") as write:
            configure(None, args)
        commands = [call.args[1] for call in write.call_args_list]
        self.assertIn("SELect:CH1 ON", commands)
        self.assertIn("SELect:CH2 ON", commands)
        self.assertIn("CH1:SCAle 10", commands)
        self.assertIn("CH2:SCAle 2", commands)
        self.assertIn("TRIGger:A:EDGE:SOUrce CH2", commands)
        self.assertLess(commands.index("HORizontal:SCALe 2"),
                        commands.index("HORizontal:POSition 0"))
        self.assertLess(commands.index("HORizontal:RECOrdlength 1000000"),
                        commands.index("HORizontal:POSition 0"))

    def test_invalid_limits(self):
        p = parser()
        for args in (["--points", "1"], ["--sync-count", "2"],
                     ["--trigger-position-pct", "1000"],
                     ["--ch1-v-div", "nan"], ["--stale-ms", "2"],
                     ["--acquire-timeout", "nan"]):
            with self.subTest(args=args):
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                    plan(p.parse_args(args), p)

    def test_child_stop_no_start(self):
        child = StimulationChild(Path("not_used"), arm=False, stale_ms=250)
        self.assertEqual(child.stop(), {"started": False})


if __name__ == "__main__":
    unittest.main()
