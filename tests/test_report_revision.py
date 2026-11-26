"""Hardware-free tests of review aggregation and publication isolation."""
import json
from pathlib import Path
import tempfile
import unittest

from experiment_analysis.paths import sha256, use_results_root
from experiment_analysis.publish_reports import publish, REPORTS
from experiment_analysis.report_revision_metrics import pool
from experiment_analysis.build_revised_experiment_reports import (
    historical_body, number_condensed_figures, count_range, ratio_range,
)


def item(n,lo,hi,windows):
    return {"scenarios":{"1.0":{
        "target_acks":n,"muted_scheduler_inside":2,"outside_or_edge":1,
        "types":{"ordinary":{"n":n,"local_matching":hi,"required_on_paths":lo,"optional_on_paths":hi-lo},
                 "restore":{"n":0,"local_matching":0,"required_on_paths":0,"optional_on_paths":0}},
        "unique_local":lo,"any_local":hi,"local_unmatched_reasons":{"OTHER":n-hi},
        "no_pulse_windows_ms":windows,"no_pulse_shorter_than_period":len(windows),
        "restart":{"inside_attempt_reasons":{},"inside_unmatched_reasons":{},
                   "inside_new_local_matching":0,"inside_union_matching":0},
        "tolerances":{"original":{"min":lo,"max":hi,"assessed":10,"matching":10,
                                  "status":"COMPLETE_ASSESSED_PATH"}}
    }}}


class ReportRevisionTests(unittest.TestCase):
    def test_pool_counts_and_windows_not_average_of_percentages(self):
        data={"A1":item(10,5,6,[1,2,100]),"A2":item(20,12,13,[3]),
              "Reference":item(1000,999,999,[])}
        p=pool(data,("A1","A2"))
        self.assertEqual((p["target_acks"],p["min"],p["max"]),(30,17,19))
        self.assertEqual(p["types"]["ordinary"]["local_matching"],19)
        self.assertEqual(p["no_pulse_window_median_ms"],2.5)
        self.assertEqual(p["unmatched_reasons"],{"OTHER":11})
        self.assertEqual(p["muted_scheduler_inside"],4)
        self.assertEqual(count_range(p["min"],p["max"]),"17–19")
        self.assertEqual(ratio_range(p["min"],p["max"],30),"56.67%–63.33%")

    def test_historical_section_retains_text_and_rebases_only_figure(self):
        source="# report\n\n## Host\n\nOriginal result.\n\n![Figure 1](plot.png)\n\n*Figure 1 - Original caption.*\n\n## Other\n\nNo."
        actual=historical_body(source,"Host")
        self.assertEqual(actual,"Original result.\n\n![Figure 1](../plot.png)\n\n*Figure 1 - Original caption.*")

    def test_condensed_renumbers_six_figures(self):
        text="\n\n".join(f"![Old](plot{i}.png)\n\n*Figure 9 - caption {i}.*" for i in range(6))
        result=number_condensed_figures(text)
        for i in range(1,7):
            self.assertIn(f"![Figure {i}](plot{i-1}.png)",result)
            self.assertIn(f"*Figure {i} — caption {i-1}.*",result)
        with self.assertRaisesRegex(ValueError,"six captioned figures"):
            number_condensed_figures("No figures")

    def test_revision_export_reuses_reference_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            raw=root/"raw"/"example"
            raw.mkdir(parents=True)
            (raw/"metadata.json").write_text("{}")
            analysis=root/"results"/raw.name/"analysis"
            rev=analysis/"report_revision"
            rev.mkdir(parents=True)
            (analysis.parent/"recording_manifest.json").write_text(json.dumps(
                {"run_id":raw.name,"metadata_sha256":sha256(raw/"metadata.json")}))
            (analysis/"old.png").write_bytes(b"old figure")
            (rev/"new.png").write_bytes(b"new figure")
            (rev/"report_revision_metrics.json").write_text("{}")
            for name in REPORTS:
                (rev/name).write_text("**Review revision — not yet approved.**\n\n"
                                     "![old](../old.png)\n\n![new](new.png)\n\n"
                                     "[Technical](extended_analysis_technical_report.md)\n")
            manifest={"schema":"matrix32_report_revision_v1","reference_run":raw.name,
                      "raw_metadata_sha256":{"Reference":sha256(raw/"metadata.json")},
                      "signed_onsets_sha256":{"Reference":"example"},
                      "review_required":True,
                      "metrics_sha256":sha256(rev/"report_revision_metrics.json"),
                      "files_sha256":{p.name:sha256(p) for p in rev.iterdir()},
                      "reused_reference_figures":{"old.png":sha256(analysis/"old.png")}}
            (rev/"report_revision_manifest.json").write_text(json.dumps(manifest))
            with use_results_root(root/"results"):
                out=publish(raw,revision=True,reports_root=root/"export",tag="review")
                self.assertEqual((out/"old.png").read_bytes(),b"old figure")
                self.assertEqual((out/"report_revision"/"new.png").read_bytes(),b"new figure")
                self.assertFalse((out/"report_revision_metrics.json").exists())
                self.assertIn("![old](old.png)",(out/REPORTS[0]).read_text())
                self.assertIn("![new](report_revision/new.png)",(out/REPORTS[0]).read_text())
                self.assertIn("[Technical](extended_analysis_technical_report.md)",(out/REPORTS[0]).read_text())
                p=json.loads((out/"publication_manifest.json").read_text())
                self.assertFalse(p["review_approved"])
                with self.assertRaises(FileExistsError):
                    publish(raw,revision=True,reports_root=root/"export",tag="review")
                approved=publish(raw,revision=True,reports_root=root/"export",
                                 tag="approved",approved_by="operator")
                self.assertTrue(json.loads((approved/"publication_manifest.json").read_text())["review_approved"])
                self.assertIn("**Approved revision.**",(approved/REPORTS[0]).read_text())
                self.assertIn("not yet approved",(rev/REPORTS[0]).read_text())
                (analysis/"old.png").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError,"source figure changed"):
                    publish(raw,revision=True,reports_root=root/"export",tag="changed")


if __name__=="__main__":
    unittest.main()
