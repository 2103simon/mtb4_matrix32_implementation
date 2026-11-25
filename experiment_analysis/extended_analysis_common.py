"""Shared read-only evidence loader for the three Matrix32 follow-up audits.

Never detects pulses or edits approvals. The exact source-locked CH1 hash is
verified by the established per-ACK audit before any result is produced.
"""
from __future__ import annotations
from experiment_analysis.paths import analysis_root

import bisect
from collections import Counter
import csv
import json
from pathlib import Path

import numpy as np

from experiment_analysis.audit_ack_physical_observability import (
    digest, event_stream, host_to_scope, load_json, mcu_stream,
    save_csv, scope_axis, verify_approved_record,
)

VERSION = 'matrix32_extended_audits_v1'
DEFAULT_MARGIN_MS = 1.8  # Sensitivity assumption, NEVER a qualified bound.
MATCH_ABS_HZ = 1.0
MATCH_FRAC = 0.025
REGULAR_PERIOD_MAX_S = 0.5


def compatible(measured, commanded):
    return (commanded is not None and float(commanded) > 0 and
            abs(float(measured)-float(commanded)) <=
            max(MATCH_ABS_HZ, MATCH_FRAC*float(commanded)))


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, indent=2, allow_nan=False, default=lambda v: v.item() if isinstance(v, np.generic) else v.tolist() if isinstance(v, np.ndarray) else (_ for _ in ()).throw(TypeError(type(v))))+'\n',
                          encoding='utf-8')


def read_context(run_dir):
    root = Path(run_dir).expanduser().resolve()
    metadata = load_json(root / 'metadata.json')
    if metadata.get('plan', {}).get('integrated_experiment') != 'MATRIX32_MCU_NANO_SCOPE_V1':
        raise ValueError('Expected integrated Matrix32 scope recording')
    if metadata.get('status') != 'complete':
        raise ValueError('Only completed recordings can be analyzed')
    if len(metadata.get('records', [])) != 1:
        raise ValueError('Extended audits currently support one scope record')
    record = metadata['records'][0]
    approval, approved, onset_times, negative_only_count = verify_approved_record(root, record)
    epoch = load_json(root / 'epoch_clock_analysis.json')
    clock = next((x for x in epoch['records'] if
                  int(x['record_index']) == int(record['index'])), None)
    if clock is None:
        raise ValueError('Missing per-record clock mapping')
    axis = scope_axis(clock, record)
    trace_path, events = event_stream(root)
    mcu = mcu_stream(root)
    folder = analysis_root(root) / f"record_{int(record['index']):05d}" / 'pulse_review'
    return dict(root=root, metadata=metadata, record=record, approval=approval,
                approved=approved, onsets=onset_times, negative_only=negative_only_count,
                review_dir=folder, axis=axis, clock=clock, events=events,
                trace_path=trace_path, mcu=mcu,
                provenance={
                    'analysis_version': VERSION,
                    'record_index': int(record['index']),
                    'approved_onsets_sha256': approval['approved_onsets_sha256'],
                    'raw_ch1_sha256': approval['source_ch1_sha256'],
                    'matrix32_commands_sha256': digest(trace_path),
                    'mcu_rates_sha256': digest(mcu['path']),
                    'clock_epoch_sha256': digest(root / 'epoch_clock_analysis.json'),
                    'scope_host_timing': 'UNQUALIFIED; offset assumptions are sensitivity tests',
                    'physical_application_time_verified': False,
                })


def approved_intervals(ctx):
    times = ctx['onsets']
    return [dict(index=i, start_id=ctx['approved'][i]['id'],
                 end_id=ctx['approved'][i+1]['id'],
                 start_s=float(a), end_s=float(b), period_s=float(b-a),
                 measured_hz=float(1/(b-a)), is_regular=bool(b-a <= REGULAR_PERIOD_MAX_S))
            for i, (a,b) in enumerate(zip(times, times[1:]))]


def timed_acks(ctx, event_kind, *, shift_ms=0):
    return sorted((dict(scope_s=host_to_scope(e['pc_monotonic_ns'], ctx['axis']) +
                             shift_ms/1000, host_ns=int(e['pc_monotonic_ns']),
                        event=e) for e in ctx['events']
                   if e.get('event') == event_kind and e.get('status') == 'OK'),
                  key=lambda row: row['host_ns'])


def last_before(timed, time_s):
    positions = [x['scope_s'] for x in timed]
    i = bisect.bisect_right(positions, time_s)-1
    return timed[i] if i >= 0 else None


def quantiles(values):
    arr = np.asarray([v for v in values if v is not None], dtype=float)
    if not len(arr):
        return {'n': 0, 'median': None, 'p90': None, 'p95': None, 'max': None}
    return {'n': int(len(arr)), 'median': float(np.median(arr)),
            'p90': float(np.percentile(arr, 90)),
            'p95': float(np.percentile(arr, 95)), 'max': float(np.max(arr))}


def safe_figure_setup():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    return plt


def make_output(ctx, name):
    out = analysis_root(ctx['root']) / name
    out.mkdir(parents=True, exist_ok=True)
    (out / 'figures').mkdir(exist_ok=True)
    return out


def relative_to_analysis(ctx, path):
    return Path(path).relative_to(analysis_root(ctx['root'])).as_posix()


def csv_rows(path):
    with Path(path).open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def raw_windows_by_index(path, windows):
    """Read only selected original CSV samples, streaming a 10M-sample file once.

    windows: mapping of figure key to (inclusive start index, exclusive end).
    Each output is (scope_time_s array, original_voltage_v array). No resampling.
    """
    if not windows:
        return {}
    starts = sorted((max(0, int(a)), int(b), key) for key, (a,b) in windows.items())
    if any(b <= a for a,b,_ in starts):
        raise ValueError('Empty or reversed raw waveform window')
    result = {key: ([], []) for _, _, key in starts}
    with Path(path).open(encoding='utf-8') as f:
        header = f.readline().strip().split(',')
        if header[:2] != ['time_s','voltage_v']:
            raise ValueError('Unexpected CH1 waveform columns')
        active = []
        ptr = 0
        for i, line in enumerate(f):
            while ptr < len(starts) and starts[ptr][0] <= i:
                active.append(starts[ptr]); ptr += 1
            active = [v for v in active if i < v[1]]
            if not active and ptr >= len(starts):
                break
            if not active:
                continue
            components = line.split(',', 2)
            t, v = float(components[0]), float(components[1])
            for _, _, key in active:
                result[key][0].append(t)
                result[key][1].append(v)
    return {key:(np.asarray(a), np.asarray(v)) for key,(a,v) in result.items()}


def scope_sample_index(ctx, time_s):
    waveform = ctx['record']['channels']['CH1']['waveform']
    return int(round((time_s-float(waveform['first_time_s'])) /
                     float(waveform['sample_interval_s'])))


def ensure_source_same(ctx):
    """Detect any source edits during a long analysis before reporting success."""
    if digest(ctx['root']/ctx['record']['channels']['CH1']['path']) != ctx['approval']['source_ch1_sha256']:
        raise ValueError('CH1 source mutated during analysis')
    if digest(ctx['review_dir']/'approved_onsets.csv') != ctx['approval']['approved_onsets_sha256']:
        raise ValueError('Approved onsets mutated during analysis')
