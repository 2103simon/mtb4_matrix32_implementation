"""Hardware-free tests for signed integrated-overview plot repair."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oscilloscope.plot_integrated_overview import (
    generate, host_to_scope, sha256, waveform_envelope,
)


class IntegratedOverviewTests(unittest.TestCase):
    def test_envelope_keeps_extrema_and_verifies_exact_ch1_sha(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'wave.csv'
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['time_s', 'voltage_v', 'raw'])
                for i in range(100):
                    writer.writerow([i*.001, -35 if i == 20 else 25 if i == 80 else 0, 0])
            meta = {'samples': 100, 'first_time_s': 0.,
                    'last_time_s': .099, 'sample_interval_s': .001}
            x, low, high, first, last = waveform_envelope(
                path, meta, sha256(path), bins=10)
            self.assertEqual(len(x), 10)
            self.assertEqual(float(low[2]), -35.)
            self.assertEqual(float(high[8]), 25.)
            self.assertEqual((first, last), (0., .099))
            with self.assertRaisesRegex(ValueError, 'signed approval'):
                waveform_envelope(path, meta, 'invalid', bins=10)

    def test_host_conversion_maintains_nanosecond_epoch_precision(self):
        origin = 84_640_030_000_000
        mapped = host_to_scope(
            np.asarray([origin, origin+50_000_000, origin+100_000_000],
                       dtype=np.int64),
            (origin, origin+100_000_000), 0., .1)
        self.assertTrue(np.allclose(mapped, [0., .05, .1]))

    def _fixture(self, root):
        root = Path(root)
        (root/'waveforms').mkdir()
        (root/'mcu_matrix32').mkdir()
        review = root/'analysis/record_00001/pulse_review'
        review.mkdir(parents=True)
        wave = root/'waveforms/record_00001_ch1.csv'
        with wave.open('w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['time_s', 'voltage_v', 'raw'])
            for i in range(100):
                v = -20 if i in (20,21,60,61) else 18 if i in (23,24,63,64) else 0
                writer.writerow([i*.001,v,0])
        (root/'metadata.json').write_text(json.dumps({
            'records': [{'index':1,'status':'captured','channels':{
                'CH1': {'path':'waveforms/record_00001_ch1.csv', 'waveform':{
                    'samples':100, 'first_time_s':0., 'last_time_s':.099,
                    'sample_interval_s':.001}}}}]}))
        (root/'epoch_clock_analysis.json').write_text(json.dumps({
            'records':[{'record_index':1,'status':'unqualified',
              'first_sample_host_monotonic_ns_est':1000000000,
              'last_sample_host_monotonic_ns_est':1099000000}]}))
        rates = root/'mcu_matrix32/fixture_tactile_rates.csv'
        with rates.open('w', newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['pc_arrival_monotonic_ns',
                             'taxel_11','sa_rate_hz_11'])
            writer.writeheader()
            for ns in (1000000000,1045000000,1090000000):
                writer.writerow({'pc_arrival_monotonic_ns':ns,
                                 'taxel_11':20,'sa_rate_hz_11':25.})
        (root/'mcu_matrix32/fixture_settings.json').write_text(json.dumps({
            'configuration':{'stimulation_source':{'taxel':11,'encoding':'sa_ii'}}}))
        (root/'mcu_matrix32/fixture_matrix32_commands.jsonl').write_text(
            json.dumps({'event':'sf_ack','status':'OK',
                        'frequency_hz':25,'pc_monotonic_ns':1045000000})+'\n')
        onsets=review/'approved_onsets.csv'
        onsets.write_text('id,time_s,review_decision\nP00001,.02,INCLUDE\nP00002,.06,INCLUDE\n')
        (review/'approved_intervals.csv').write_text('period_s\n.04\n')
        source_hashes={}
        for filename in ('accepted_onsets.csv', 'rejected_onsets.csv',
                         'suppressed_onsets.csv', 'periods_unverified.csv',
                         'threshold_sensitive_onsets.csv'):
            (review/filename).write_text('id\n')
            source_hashes[filename]=sha256(review/filename)
        manifest={'source_ch1_sha256':sha256(wave),
                  'source_tables_sha256':source_hashes}
        (review/'review_manifest.json').write_text(json.dumps(manifest))
        (review/'review_decisions.csv').write_text('id,decision\nP00001,INCLUDE\n')
        (review/'manual_additions.csv').write_text('id,time_s,decision,note\n')
        approval={'human_signed_off':True,'source_ch1_sha256':sha256(wave),
                  'source_tables_sha256':source_hashes,'included_onsets':2}
        for filename,key in (
            ('review_manifest.json','review_manifest_sha256'),
            ('review_decisions.csv','review_decisions_sha256'),
            ('manual_additions.csv','manual_additions_sha256'),
            ('approved_onsets.csv','approved_onsets_sha256'),
            ('approved_intervals.csv','approved_intervals_sha256')):
            approval[key]=sha256(review/filename)
        (review/'review_approval.json').write_text(json.dumps(approval))
        (review/'report.md').write_text('[Integrated overview](../integrated_overview.png)\n')
        return root, wave, review

    def test_generate_fills_legacy_report_link_without_editing_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root,wave,review = self._fixture(temp)
            evidence = {p.name: sha256(p) for p in review.iterdir() if p.is_file()}
            figure = generate(root, bins=20)
            self.assertTrue(figure.is_file())
            self.assertGreater(figure.stat().st_size, 1000)
            self.assertEqual(figure.name, 'integrated_overview.png')
            self.assertTrue((review.parent/'integrated_overview.png').is_file())
            self.assertEqual(evidence, {p.name:sha256(p)
                                        for p in review.iterdir() if p.is_file()})

    def test_edited_approved_onsets_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root,wave,review = self._fixture(temp)
            with (review/'approved_onsets.csv').open('a') as f:
                f.write('P00003,.07,INCLUDE\n')
            with self.assertRaisesRegex(ValueError,'Signed review evidence changed'):
                generate(root,bins=20)
            self.assertFalse((review.parent/'integrated_overview.png').exists())

    def test_modified_original_waveform_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root,wave,review = self._fixture(temp)
            wave.write_text(wave.read_text().replace('0.0,0,0', '0.0,1,0'))
            with self.assertRaisesRegex(ValueError,'Original CH1 waveform differs'):
                generate(root,bins=20)
            self.assertFalse((review.parent/'integrated_overview.png').exists())


if __name__ == '__main__':
    unittest.main()