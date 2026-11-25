"""Ensure post-recording Gray diagnostics never overwrite raw clock evidence."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiment_analysis.paths import use_results_root
from oscilloscope import compare_gray_schedules
from oscilloscope import analyze_gray_epoch_experiment
from oscilloscope import align_gray_records, analyze_nano_clock, decode_gray_timecode


class GrayOutputSeparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.results = self.home / 'results'
        self.baseline = self.home / 'data/recordings/base'
        self.candidate = self.home / 'data/recordings/candidate'
        for run, period in ((self.baseline, .1), (self.candidate, .2)):
            run.mkdir(parents=True)
            (run/'metadata.json').write_text(json.dumps({
                'status':'complete', 'run_id':run.name, 'records':[],
                'plan':{'nominal_frame_s':period}}))
            (run/'nano_serial.jsonl').write_text('')
            analysis = self.results/run.name/'analysis'
            analysis.mkdir(parents=True)
            (analysis/'gray_pulse_geometry_summary.json').write_text(
                json.dumps({'nominal_frame_s':period, 'records':[]}))
            (run/'epoch_clock_analysis.json').write_text(
                '{"capture_evidence":"untouched"}')

    def test_schedule_comparison_reads_external_results_and_writes_outside_raw(self):
        values = (
            'start_width_std_us', 'data_width_std_us',
            'end_width_std_us', 'physical_frame_spacing_std_us',
            'nano_frame_spacing_std_us',
            'nano_minus_rate_scaled_physical_spacing_std_us',
            'scope_to_nano_affine_rms_us', 'nano_to_host_midpoint_rms_us',
            'usb_offset_interval_width_us',
        )
        def rows(name, *args):
            return [dict(run=name, record=1,
                         scope_to_nano_100us_gate_passes=True,
                         **{n:5.0 for n in values})]
        with use_results_root(self.results):
            with patch.object(compare_gray_schedules, 'read_nano_frames',
                              return_value=[]), patch.object(
                    compare_gray_schedules, 'rows_for_run', side_effect=rows):
                loaded = compare_gray_schedules._load(self.baseline)
                self.assertEqual(loaded[2]['nominal_frame_s'], .1)
                compare_gray_schedules.run(self.baseline, self.candidate)
                report = (self.results/'candidate/analysis'/
                          'gray_schedule_comparison.md')
                self.assertTrue(report.is_file())
                self.assertFalse((self.candidate/'analysis').exists())
                self.assertEqual(
                    (self.candidate/'epoch_clock_analysis.json').read_text(),
                    '{"capture_evidence":"untouched"}')
                with self.assertRaisesRegex(ValueError, 'raw recording'):
                    compare_gray_schedules.run(
                        self.baseline, self.candidate,
                        output=self.candidate/'analysis')

    def test_offline_alignment_defaults_to_results_not_recording(self):
        original = (self.candidate/'epoch_clock_analysis.json').read_bytes()
        with use_results_root(self.results):
            with patch.object(align_gray_records, 'read_nano_frames',
                              return_value=[]), patch.object(
                    align_gray_records, 'build_alignment',
                    return_value={'decoded_frames_total':0,
                                  'source_record_count':0, 'records':[]}):
                align_gray_records.main([str(self.candidate)])
                target = (self.results/'candidate/analysis'/
                          'gray_alignment_diagnostics.json')
                self.assertTrue(target.is_file())
                self.assertFalse((self.candidate/'gray_alignment.json').exists())
                with self.assertRaisesRegex(ValueError, 'raw recordings'):
                    align_gray_records.main([
                        str(self.candidate), '--output',
                        str(self.candidate/'gray_alignment.json')])
        self.assertEqual((self.candidate/'epoch_clock_analysis.json').read_bytes(),
                         original)

    def test_offline_nano_clock_defaults_to_results(self):
        report = {'status':'passive_drift_only',
                  'passive':{'sample_count':0,
                             'drift_ppm_nano_to_host':0},
                  'active':None, 'scope_records':[]}
        with use_results_root(self.results):
            with patch.object(analyze_nano_clock, 'analyze_run',
                              return_value=report):
                analyze_nano_clock.main([str(self.candidate)])
                self.assertTrue((self.results/'candidate/analysis'/
                                 'nano_host_clock_diagnostics.json').is_file())
                self.assertFalse((self.candidate/'nano_host_clock.json').exists())
                with self.assertRaisesRegex(ValueError, 'raw recordings'):
                    analyze_nano_clock.main([
                        str(self.candidate), '--output',
                        str(self.candidate/'nano_host_clock.json')])

    def test_offline_gray_decoder_rejects_explicit_raw_output(self):
        csv = self.candidate/'waveforms/ch2.csv'
        csv.parent.mkdir()
        csv.write_text('time_s,voltage_v\\n')
        with patch.object(decode_gray_timecode, 'decode_csv', return_value={}):
            with self.assertRaisesRegex(ValueError, 'raw recordings'):
                decode_gray_timecode.main([
                    str(csv), '--output', str(self.candidate/'decoded.json')])

    def test_standalone_epoch_reanalysis_does_not_overwrite_capture_time_map(self):
        original = (self.candidate/'epoch_clock_analysis.json').read_bytes()
        alignment = {'diagnostic':'alignment'}
        report = {'run_id':self.candidate.name, 'records':[],
                  'qualified_provisional_records':0,
                  'exploratory_local_fit_records':0}
        with use_results_root(self.results):
            with patch.object(analyze_gray_epoch_experiment, 'analyze',
                              return_value=(alignment, report)):
                analyze_gray_epoch_experiment.main([str(self.candidate)])
        output = (self.results/'candidate/analysis'/
                  'gray_epoch_diagnostics/epoch_clock_analysis.json')
        self.assertTrue(output.is_file())
        self.assertEqual((self.candidate/'epoch_clock_analysis.json').read_bytes(),
                         original)
        self.assertFalse((self.candidate/'gray_alignment.json').exists())


if __name__ == '__main__':
    unittest.main()
