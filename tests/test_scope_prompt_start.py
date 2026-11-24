"""The cue epoch precedes potentially blocking USB queries; no hardware used."""
import threading
import unittest
from unittest.mock import Mock, patch

from oscilloscope import record_scope as scope
from oscilloscope.operator_prompts import OperatorPrompts, normalize_operator_prompts


class ScopePromptStartTests(unittest.TestCase):
    def test_run_hook_precedes_queries_and_preserves_verified_armed_callback(self):
        order = []
        replies = iter(["1", "0"])
        def query(file, command):
            order.append("query")
            return next(replies)
        def written(observation):
            order.append("run_written")
            self.assertLessEqual(observation["run_write_begin_monotonic_s"],
                                 observation["run_write_end_monotonic_s"])
        with patch.object(scope, "write", side_effect=lambda f, c: order.append(c)), \
                patch.object(scope, "query", side_effect=query):
            scope.acquire(None, 1, on_run_written=written,
                          on_armed=lambda: order.append("verified_armed"))
        self.assertLess(order.index("ACQuire:STATE RUN"), order.index("run_written"))
        self.assertLess(order.index("run_written"), order.index("query"))
        self.assertLess(order.index("query"), order.index("verified_armed"))

    def test_failed_run_write_does_not_start_timer(self):
        callback = Mock()
        def write(file, command):
            if command == "ACQuire:STATE RUN":
                raise OSError("RUN write failed")
        with patch.object(scope, "write", side_effect=write), \
                patch.object(scope, "query") as query:
            with self.assertRaisesRegex(OSError, "RUN write failed"):
                scope.acquire(None, 1, on_run_written=callback)
        callback.assert_not_called()
        query.assert_not_called()

    def test_timer_delivers_cues_while_first_scope_query_waits(self):
        delivered = threading.Event()
        lines = []
        def emit(line):
            lines.append(line)
            if "RELEASE" in line:
                delivered.set()
        prompts = OperatorPrompts(
            normalize_operator_prompts({0: "HOLD", 0.03: "RELEASE"}, 0.5),
            duration_s=0.5, emit=emit,
        )
        calls = []
        def query(file, command):
            calls.append(command)
            if len(calls) == 1:
                self.assertTrue(delivered.wait(0.8), "cue was held up by the scope query")
                return "1"
            return "0"
        try:
            with patch.object(scope, "write"), patch.object(scope, "query", side_effect=query):
                scope.acquire(None, 2, on_run_written=prompts.start)
        finally:
            prompts.finish("scope_stopped")
            self.assertTrue(prompts.join(1))
        self.assertEqual(len(lines), 2)
        self.assertTrue(prompts.snapshot()["delivery_complete"])


if __name__ == "__main__":
    unittest.main()
