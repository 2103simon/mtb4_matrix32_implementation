"""Offline report-presentation tests; source-locked evidence never changes."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from oscilloscope.present_scientific_report import (
    _check_figures, _frequency_pairs, _image, present,
)


class ScientificReportPresentationTests(unittest.TestCase):
    def test_markdown_figure_caption_is_descriptive(self):
        value = _image('test.png', 'Compare measured and commanded frequencies.', 3)
        self.assertIn('![Figure 3](test.png)', value)
        self.assertIn('*Figure 3 - Compare measured and commanded frequencies.*', value)

    def test_frequency_pairs_respect_signed_eligibility(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'frequency_robustness_all_intervals.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                w = csv.DictWriter(stream, fieldnames=['baseline_ack_diagnostic',
                    'ack_indicated_hz_stable', 'measured_frequency_hz', 'frequency_error_hz'])
                w.writeheader()
                w.writerow(dict(baseline_ack_diagnostic='WITHIN_EXPLORATORY_TOLERANCE',
                                ack_indicated_hz_stable='50', measured_frequency_hz='50.05',
                                frequency_error_hz='0.05'))
                w.writerow(dict(baseline_ack_diagnostic='NOT_COMPARABLE',
                                ack_indicated_hz_stable='', measured_frequency_hz='51',
                                frequency_error_hz=''))
                w.writerow(dict(baseline_ack_diagnostic='WITHIN_EXPLORATORY_TOLERANCE',
                                ack_indicated_hz_stable='60', measured_frequency_hz='59.98',
                                frequency_error_hz='-0.02'))
            c, m, d = _frequency_pairs(Path(temp), 2)
            self.assertEqual((len(c),len(m),len(d)), (2,2,2))
            with self.assertRaises(ValueError):
                _frequency_pairs(Path(temp), 3)

    def test_frequency_error_is_source_consistent(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)/'frequency_robustness_all_intervals.csv'
            p.write_text('baseline_ack_diagnostic,ack_indicated_hz_stable,measured_frequency_hz,frequency_error_hz\n'
                'WITHIN_EXPLORATORY_TOLERANCE,50,52,0.01\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                _frequency_pairs(Path(temp), 1)

    def test_figure_link_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                _check_figures(Path(temp), '![Only one](missing.png)')

    def test_presentation_cannot_be_archived_as_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            analysis = Path(temp)/'analysis'
            analysis.mkdir()
            (analysis/'final_experiment_report.md').write_text(
                '# Matrix32 tactile-to-electrical stimulation: consolidated experiment report\n'
                '## At a glance: research questions and evidence\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Refusing to replace'):
                present(analysis)

    def test_signed_recording_report_and_legacy(self):
        sample = os.environ.get('MTB4_SCI_REPORT_RUN')
        if not sample:
            self.skipTest('Set MTB4_SCI_REPORT_RUN to run source-locked recording regression')
        root = Path(sample)
        source = root/'analysis'
        required = [
            'final_experiment_report_legacy.md',
            'command_delivery_scorecard.json', 'command_delivery_scorecard.png',
            'extended_analysis_manifest.json', 'frequency_robustness_summary.json',
            'frequency_robustness_all_intervals.csv',
            'frequency_transitions/summary.json',
            'stimulation_shutdown/summary.json',
            'stimulation_shutdown/figures/incomplete_pulse_morphology.png',
            'command_timing/summary.json',
            'command_timing/figures/throughput_per_second.png',
            'command_timing/figures/message_cadence_boxplots.png',
            'ack_physical_observability/summary.json',
            'ack_physical_observability/restart_02_detail.png',
        ]
        if not all((source/rel).is_file() for rel in required):
            self.skipTest('Sample must contain completed extended/per-ACK audit outputs')
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            shutil.copy2(root/'metadata.json',dest/'metadata.json')
            a = dest/'analysis'
            for rel in required:
                p = a/rel
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source/rel,p)
            original = (a/'final_experiment_report_legacy.md').read_bytes()
            (a/'final_experiment_report.md').write_bytes(
                original + b'\n<!-- regenerated technical index; preserve existing legacy -->\n')
            copy_hash = hashlib.sha256((a/'frequency_robustness_all_intervals.csv').read_bytes()).hexdigest()
            result = present(a)
            self.assertEqual(result['figure_count'], 6)
            text = (a/'final_experiment_report.md').read_text()
            self.assertEqual(len(re.findall(r'\*Figure \d+ -', text)),6)
            self.assertEqual(len(re.findall(r'^!\[Figure \d+\]',text,re.M)),6)
            self.assertIn('85/85 eligible',text)
            self.assertIn('266/266 SF OK ACKs',text)
            self.assertIn('41/41 SC OK ACKs',text)
            self.assertIn('## Conclusions and remaining questions',text)
            self.assertIn('Operator cue schedule',text)
            self.assertIn('hypothetical alignment scenarios',text.lower())
            self.assertEqual((a/'final_experiment_report_legacy.md').read_bytes(),original)
            self.assertEqual(hashlib.sha256((a/'frequency_robustness_all_intervals.csv').read_bytes()).hexdigest(),copy_hash)
            self.assertEqual(len(list((a/'presentation_figures').glob('*.png'))),3)
            # A second direct presentation call must not silently replace legacy.
            with self.assertRaisesRegex(ValueError, 'Refusing to replace'):
                present(a)


if __name__ == '__main__':
    unittest.main()
