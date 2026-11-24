"""Hardware-free invariants and optional signed-recording regression."""
from pathlib import Path
import csv
import json
import os
import tempfile
import unittest

import numpy as np

from oscilloscope.extended_analysis_common import (
    compatible, quantiles, raw_windows_by_index, approved_intervals,
)
from oscilloscope.analyze_frequency_transitions import evaluate
from oscilloscope.analyze_stimulation_shutdown import compute_events
from oscilloscope.analyze_command_timing import pair_commands, analyze_host_events
from oscilloscope.run_extended_analysis import run as run_all


def event(kind, at_s, **k):
    return {'event':kind,'pc_monotonic_ns':int(round(at_s*1e9)),**k}


def synthetic_context(events, timestamps=(.01,.05)):
    return dict(axis=(0.,1.,0,1_000_000_000),events=events,
        onsets=np.asarray(timestamps,dtype=float),
        approved=[{'id':f'P{i+1:05d}'} for i in range(len(timestamps))],
        mcu={'pc_ns':np.asarray([1_000_000,20_000_000],dtype=np.int64),
             'rates':np.asarray([15.,25.])})


class ExtendedAnalysisTests(unittest.TestCase):
    def test_frequency_match_tolerance(self):
        self.assertTrue(compatible(25.01,25))
        self.assertFalse(compatible(29,25))
        self.assertFalse(compatible(25,0))

    def test_in_interval_frequency_match_not_execution_proof(self):
        ctx=synthetic_context([
            event('start_write_completed',.0,frequency_hz=15),
            event('sf_ack',.0,frequency_hz=15,status='OK'),
            event('sf_ack',.020,frequency_hz=25,status='OK'),
        ])
        rows=evaluate(ctx)
        self.assertEqual(len(rows),1)
        row=rows[0]
        self.assertEqual(row['classification'],'IN_INTERVAL_ACK_COMPATIBLE_ONLY')
        self.assertEqual(row['in_interval_matching_hz'],'25')
        self.assertFalse(row['physical_apply_time_proven'])
        self.assertEqual(len(approved_intervals(ctx)),1)

    def test_long_pause_is_not_low_frequency_stimulation(self):
        ctx=synthetic_context([
            event('start_write_completed',.0,frequency_hz=30),
            event('sf_ack',0.,frequency_hz=30,status='OK')],(.01,.71))
        self.assertEqual(evaluate(ctx)[0]['classification'],
                         'LONG_GAP_NOT_A_STIMULATION_PERIOD')

    def test_muted_ack_not_cycle_evidence(self):
        ctx=synthetic_context([
            event('start_write_completed',.0,frequency_hz=25),
            event('sf_ack',.0,frequency_hz=25,status='OK'),
            event('sc_ack',.005,current_command=0,status='OK')])
        self.assertEqual(evaluate(ctx)[0]['classification'],
                         'SC0_CONTEXT_NOT_RATE_EVIDENCE')

    def test_source_negative_only_review_must_be_explicitly_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            directory=Path(td)
            with (directory/'rejected_onsets.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=['sample_index','time_s','voltage_v',
                      'status','positive_threshold_v','positive_max_v'])
                w.writeheader();w.writerow({'sample_index':100,'time_s':.01,
                    'voltage_v':-13,'status':'REJECTED_MISSING_POSITIVE_PHASE',
                    'positive_threshold_v':8,'positive_max_v':.1})
            (directory/'review_decisions.csv').write_text(
               'id,kind,sample_index,time_s,detector_status,decision,note\n'
               'R00001,rejected,100,0.01,REJECTED_MISSING_POSITIVE_PHASE,EXCLUDE,raw\n')
            ctx=dict(review_dir=directory,negative_only=1,
                onsets=np.asarray([.005,.02]),axis=(0.,1.,0,1_000_000_000),events=[
                event('sc_ack',.008,status='OK',current_command=0)])
            result=compute_events(ctx)
            self.assertEqual(len(result),1)
            self.assertEqual(result[0]['last_sc_ack_value'],0)
            self.assertEqual(result[0]['review_decision'],'EXCLUDE')
            (directory/'review_decisions.csv').write_text(
               'id,kind,sample_index,time_s,detector_status,decision,note\n'
               'R00001,rejected,100,0.01,REJECTED_MISSING_POSITIVE_PHASE,INCLUDE,raw\n')
            with self.assertRaisesRegex(ValueError,'explicitly excluded'):
                compute_events(ctx)

    def test_command_pairing_is_strict(self):
        ok=[event('sf_requested',.01,frequency_hz=25),
            event('sf_ack',.025,frequency_hz=25,status='OK')]
        self.assertEqual(len(pair_commands(ok,'sf')),1)
        bad=[ok[0],dict(ok[1],frequency_hz=26)]
        with self.assertRaisesRegex(ValueError,'mismatch'):
            pair_commands(bad,'sf')
        with self.assertRaisesRegex(ValueError,'count differs'):
            pair_commands(ok[:1],'sf')

    def test_host_latency_not_scope_application_latency(self):
        ctx=synthetic_context([
            event('sf_requested',.010,frequency_hz=25,source_sample_ns=20_000_000),
            event('sf_ack',.025,frequency_hz=25,status='OK'),
        ])
        sf,sc=analyze_host_events(ctx)
        self.assertAlmostEqual(sf[0]['request_to_ack_ms'],15)
        self.assertFalse(sf[0]['physical_application_latency_measured'])
        self.assertTrue(sf[0]['rounded_source_hz_equals_sf'])
        self.assertEqual(sc,[])

    def test_raw_sparse_windows_keep_original_values_and_timestamps(self):
        with tempfile.TemporaryDirectory() as td:
            ch=Path(td)/'raw.csv'
            ch.write_text('time_s,voltage_v,raw\n' +
                ''.join(f'{i*.001},{i-5},{i}\n' for i in range(1000)))
            parts=raw_windows_by_index(ch,{'one':(3,7),'two':(5,8)})
            self.assertEqual(parts['one'][1].tolist(),[-2.,-1.,0.,1.])
            self.assertEqual(len(parts['two'][0]),3)

    def test_quantile_counts_are_json_serializable(self):
        x=quantiles(np.array([1,2,3],dtype=np.int64))
        self.assertIsInstance(x['n'],int)
        json.dumps(x,allow_nan=False)

    @unittest.skipUnless(os.environ.get('MTB4_EXTENDED_RUN'),
                         'Set MTB4_EXTENDED_RUN for signed real-data regression')
    def test_signed_recording_end_to_end(self):
        run_dir=Path(os.environ['MTB4_EXTENDED_RUN'])
        approval=run_dir/'analysis/record_00001/pulse_review/review_approval.json'
        previous=approval.read_bytes()
        manifest=run_all(run_dir)
        self.assertEqual(approval.read_bytes(),previous)
        self.assertEqual(manifest['approved_onsets'],418)
        root=run_dir/'analysis'
        trans=json.loads((root/'frequency_transitions/summary.json').read_text())
        shut=json.loads((root/'stimulation_shutdown/summary.json').read_text())
        timing=json.loads((root/'command_timing/summary.json').read_text())
        self.assertEqual(trans['classification_counts']['IN_INTERVAL_ACK_COMPATIBLE_ONLY'],67)
        self.assertEqual(shut['operator_excluded_negative_only'],11)
        self.assertEqual(timing['sf_request_ack_pairs'],266)
        self.assertTrue((root/'final_experiment_report.md').exists())
        for name in ('frequency_transitions','stimulation_shutdown','command_timing'):
            s=json.loads((root/name/'summary.json').read_text())
            for pic in s['figures']:
                self.assertTrue((root/name/pic).exists(),pic)


if __name__=='__main__':unittest.main()
