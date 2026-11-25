"""Host-only throughput, SC-state segmentation and Markdown caption tests."""
from __future__ import annotations

import unittest
import numpy as np

from oscilloscope.host_timing_views import (
    host_series, throughput_bins, state_at, state_for_span,
    sc_history, state_timing_samples, state_timing_stats,
)
from oscilloscope.report_figure_captions import (
    add_figure_captions, caption_for,
)


def event(name, seconds, **kw):
    return {'event': name, 'pc_monotonic_ns': int(round(seconds*1e9)), **kw}


class HostStateTimingTests(unittest.TestCase):
    def test_throughput_preserves_every_event_and_mixed_state_seconds(self):
        events=[
            event('sf_requested', .10, frequency_hz=10),
            event('sf_ack', .12, frequency_hz=10, status='OK',
                  evidence={'ack': {'write_begin_monotonic_s': .105,
                                    'ack_monotonic_s': .119}}),
            event('sc_ack', .25, current_command=50, status='OK'),
            event('sf_requested', .50, frequency_hz=20),
            event('sf_ack', .53, frequency_hz=20, status='OK'),
            event('sc_ack', .75, current_command=0, status='OK'),
        ]
        ctx={'events':events, 'mcu':{'pc_ns':np.array(
             [0,100_000_000,900_000_000],dtype=np.int64)}}
        bins=throughput_bins(ctx)
        self.assertEqual(len(bins),1)
        self.assertAlmostEqual(bins[0]['unknown_fraction'],.25)
        self.assertAlmostEqual(bins[0]['active_fraction'],.50)
        self.assertAlmostEqual(bins[0]['muted_fraction'],.25)
        for key,series in host_series(ctx).items():
            self.assertEqual(sum(row[key] for row in bins),len(series))
        self.assertEqual(bins[0]['sf_ble_writes'],1)

    def test_ack_state_spans_never_hide_transitions_or_unknown(self):
        history=sc_history([
            event('sc_ack',.10,current_command=50,status='OK'),
            event('sc_ack',.20,current_command=0,status='OK'),
            event('sc_ack',.30,current_command=50,status='OK')])
        self.assertEqual(state_at(0,history),'UNKNOWN')
        self.assertEqual(state_for_span(0,150_000_000,history),'UNKNOWN')
        self.assertEqual(state_for_span(110_000_000,190_000_000,history),'ACTIVE')
        self.assertEqual(state_for_span(190_000_000,210_000_000,history),'CROSSES_SC_ACK')
        self.assertEqual(state_for_span(210_000_000,270_000_000,history),'MUTED')

    def test_state_stratification_keeps_excluded_spans_in_csv(self):
        events=[event('sc_ack',.10,current_command=50,status='OK'),
                event('sf_ack',.15,frequency_hz=15,status='OK'),
                event('sc_ack',.20,current_command=0,status='OK'),
                event('sf_ack',.25,frequency_hz=20,status='OK')]
        ctx={'events':events,'mcu':{'pc_ns':np.array([
                 110_000_000,160_000_000,180_000_000,
                 230_000_000,250_000_000],dtype=np.int64)}}
        sf=[{'request_host_ns':120_000_000,'ack_host_ns':150_000_000},
            {'request_host_ns':190_000_000,'ack_host_ns':250_000_000}]
        samples=state_timing_samples(ctx,sf)
        labels={(r['distribution'],r['state']) for r in samples}
        self.assertIn(('mcu_arrival_dt_ms','CROSSES_SC_ACK'),labels)
        self.assertIn(('sf_request_to_ack_ms','CROSSES_SC_ACK'),labels)
        stats=state_timing_stats(samples)
        self.assertEqual(sum(x['n'] for x in stats
             if x['distribution']=='sf_request_to_ack_ms'),1)

    def test_every_generated_figure_gets_a_nonduplicated_caption(self):
        source=('## Example\n\n'
                '![Throughput](figures/throughput_per_second.png)\n\n'
                '![Timing](figures/active_muted_timing_boxplots.png)\n')
        rendered=add_figure_captions(source)
        self.assertEqual(rendered.count('*Figure — '),2)
        self.assertIn('![Throughput](figures/throughput_per_second.png)\n\n*Figure — ',rendered)
        self.assertEqual(add_figure_captions(rendered),rendered)
        with self.assertRaisesRegex(ValueError,'No caption specified'):
            caption_for('unknown_figure.png')


if __name__=='__main__':
    unittest.main()
