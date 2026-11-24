"""Offline source-locked SF ACK observability report for Matrix32 experiments.

Reads operator-approved biphasic CH1 negative onsets (never re-detects), host
MCU rates and command log, and the saved provisional scope/host clock map.
An ACK is protocol evidence, not proof of electrical application; this audit
identifies compatible observed complete cycles and potential supersession.

Run from repository root:
  python -m oscilloscope.audit_ack_physical_observability --run-dir data/recordings/RUN

Only generated files in analysis/ack_physical_observability/ and the new
analysis/final_integrated_report.md are written. Original files are read-only.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np

DEFAULT_MARGINS_MS = (0.5, 1.8, 3.5)
MIN_PREVIOUS_CYCLE_GUARD_S = 0.005
MATCH_ABSOLUTE_HZ = 1.0
MATCH_RELATIVE_FRACTION = 0.025
MAX_REGULAR_PERIOD_S = 0.5


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as inp:
        for block in iter(lambda: inp.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_csv(path):
    with Path(path).open(newline='', encoding='utf-8') as inp:
        return list(csv.DictReader(inp))


def save_csv(path, rows, fields):
    with Path(path).open('w', newline='', encoding='utf-8') as out:
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def verify_approved_record(root, record):
    """Verify signed decisions, source-table hashes and original raw CH1."""
    idx = int(record['index'])
    folder = root / 'analysis' / f'record_{idx:05d}' / 'pulse_review'
    approval = load_json(folder / 'review_approval.json')
    manifest = load_json(folder / 'review_manifest.json')
    if approval.get('human_signed_off') is not True:
        raise ValueError(f'Record {idx}: no signed operator approval')
    if int(approval['record_index']) != idx or int(manifest['record_index']) != idx:
        raise ValueError('Approval record index mismatch')
    if 'BIPHASIC_POSITIVE_CONFIRM_V1' not in manifest.get('detector', ''):
        raise ValueError('Signed review is not a biphasic-confirmed pulse inventory')
    if manifest['source_ch1_sha256'] != approval['source_ch1_sha256']:
        raise ValueError('Approval source waveform hash mismatch')
    if manifest['source_tables_sha256'] != approval['source_tables_sha256']:
        raise ValueError('Detector source-table inventory mismatch')
    for name, expected in approval['source_tables_sha256'].items():
        if digest(folder / name) != expected:
            raise ValueError(f'Changed detector evidence: {name}')
    for filename, key in (
        ('review_manifest.json', 'review_manifest_sha256'),
        ('review_decisions.csv', 'review_decisions_sha256'),
        ('manual_additions.csv', 'manual_additions_sha256'),
        ('approved_onsets.csv', 'approved_onsets_sha256'),
        ('approved_intervals.csv', 'approved_intervals_sha256'),
    ):
        if digest(folder / filename) != approval[key]:
            raise ValueError(f'Invalid operator approval file: {filename}')
    source_ch1 = root / record['channels']['CH1']['path']
    if digest(source_ch1) != approval['source_ch1_sha256']:
        raise ValueError('Original CH1 changed since operator approval')
    onsets = read_csv(folder / 'approved_onsets.csv')
    intervals = read_csv(folder / 'approved_intervals.csv')
    if len(onsets) != int(approval['included_onsets']):
        raise ValueError('Approved onset count mismatch')
    if len(intervals) != int(approval['included_intervals']):
        raise ValueError('Approved interval count mismatch')
    if len(intervals) != max(0, len(onsets)-1):
        raise ValueError('Approved interval inventory must be consecutive')
    if any(row['review_decision'] != 'INCLUDE' for row in onsets):
        raise ValueError('Nonincluded onset in signed inventory')
    times = np.asarray([float(x['time_s']) for x in onsets], dtype=float)
    if not np.all(np.isfinite(times)) or np.any(np.diff(times) <= 0):
        raise ValueError('Invalid approved onset timestamps')
    for row, a, b in zip(intervals, onsets, onsets[1:]):
        if row['start_id'] != a['id'] or row['end_id'] != b['id']:
            raise ValueError('Approved interval IDs differ from onsets')
        if abs(float(row['period_s']) - (float(b['time_s'])-float(a['time_s']))) > 1e-9:
            raise ValueError('Approved interval period mismatch')
    decisions = read_csv(folder / 'review_decisions.csv')
    rejected_negative_only = sum(row.get('detector_status') ==
                                 'REJECTED_MISSING_POSITIVE_PHASE' and
                                 row.get('decision', '').upper() == 'EXCLUDE'
                                 for row in decisions)
    return approval, onsets, times, rejected_negative_only


def event_stream(root):
    files = sorted((root / 'mcu_matrix32').glob('*_matrix32_commands.jsonl'))
    if len(files) != 1:
        raise ValueError(f'Expected exactly one command trace, found {len(files)}')
    with files[0].open(encoding='utf-8') as inp:
        events = [json.loads(line) for line in inp if line.strip()]
    return files[0], sorted(events, key=lambda x: int(x.get('pc_monotonic_ns', -1)))


def mcu_stream(root):
    files = sorted((root / 'mcu_matrix32').glob('*_tactile_rates.csv'))
    if len(files) != 1:
        raise ValueError(f'Expected exactly one MCU rates CSV, found {len(files)}')
    settings = files[0].with_name(files[0].name.replace('_tactile_rates.csv', '_settings.json'))
    source = (load_json(settings).get('configuration', {}).get('stimulation_source', {})
              if settings.exists() else {})
    taxel = int(source.get('taxel', 11))
    encoding = source.get('encoding', 'sa_ii')
    if taxel not in range(12) or encoding not in ('sa_ii', 'ra_i'):
        raise ValueError('Unknown recorded taxel or tactile encoding')
    key = ('sa_rate_hz_' if encoding == 'sa_ii' else 'ra_rate_hz_') + str(taxel)
    with files[0].open(newline='', encoding='utf-8') as inp:
        rows = list(csv.DictReader(inp))
    ns = np.asarray([int(row['pc_arrival_monotonic_ns']) for row in rows], dtype=np.int64)
    rates = np.asarray([float(row[key]) for row in rows], dtype=float)
    pressure = np.asarray([float(row[f'taxel_{taxel}']) for row in rows], dtype=float)
    return {'path': files[0], 'taxel': taxel, 'encoding': encoding,
            'pc_ns': ns, 'rates': rates, 'pressure': pressure}


def scope_axis(clock, record):
    metadata = record['channels']['CH1']['waveform']
    s0, s1 = float(metadata['first_time_s']), float(metadata['last_time_s'])
    h0, h1 = (int(clock['first_sample_host_monotonic_ns_est']),
              int(clock['last_sample_host_monotonic_ns_est']))
    if s1 <= s0 or h1 <= h0:
        raise ValueError('Non-positive scope or estimated host capture duration')
    return (s0, s1, h0, h1)


def host_to_scope(ns, axis):
    s0, s1, h0, h1 = axis
    return s0 + (int(ns)-h0)/(h1-h0)*(s1-s0)


def classify_ack_rows(events, onsets, axis, margin_ms, *, onset_ids=None, mcu=None):
    """Classify successful SF ACKs without inferring physical apply instants.

    A matching witness is TWO consecutive approved biphasic onsets after the
    ACK + prior-cycle guard and before the next control ACK. Comparison uses
    only scope-native physical periods. The host map is explicitly provisional.
    """
    margin = margin_ms / 1000
    onset_ids = (list(onset_ids) if onset_ids is not None
                 else [f"P{i+1:05d}" for i in range(len(onsets))])
    if len(onset_ids) != len(onsets):
        raise ValueError("Onset ID inventory does not match approved times")
    mcu_positions = ({int(ns): i for i, ns in enumerate(mcu["pc_ns"])}
                     if mcu is not None else {})
    s0, s1, h0, h1 = axis
    sf_requests = [e for e in events if e.get('event') == 'sf_requested']
    sf_acks = [e for e in events if e.get('event') == 'sf_ack' and e.get('status') == 'OK']
    # Record request provenance ONLY when sequential pairing is unambiguous.
    paired = {}
    for i, ack in enumerate(sf_acks):
        req = sf_requests[i] if i < len(sf_requests) else None
        if (req and int(req.get('pc_monotonic_ns', 0)) <= int(ack['pc_monotonic_ns'])
                and req.get('frequency_hz') == ack.get('frequency_hz')):
            paired[id(ack)] = req
    control = [e for e in events if e.get('event') in (
        'sf_ack', 'sc_ack', 'start_write_completed', 'shutdown_stop_write_completed',
        'stop_write_completed', 'ble_lost') and
        (e.get('event') not in ('sf_ack', 'sc_ack') or e.get('status') == 'OK')]
    control.sort(key=lambda e: int(e['pc_monotonic_ns']))
    enriched = []
    mode = 'UNKNOWN'
    last_frequency = None
    for e in control:
        before = mode
        old_frequency = last_frequency
        kind = e['event']
        if kind == 'start_write_completed':
            mode = 'ACK_INDICATED_ACTIVE'
            last_frequency = e.get('frequency_hz')
        elif kind == 'sf_ack':
            last_frequency = e.get('frequency_hz')
        elif kind == 'sc_ack':
            mode = ('ACK_INDICATED_MUTED' if e.get('current_command') == 0
                    else 'ACK_INDICATED_ACTIVE')
        elif kind in ('shutdown_stop_write_completed', 'stop_write_completed', 'ble_lost'):
            mode = 'ACK_INDICATED_STOPPED'
        enriched.append((e, host_to_scope(e['pc_monotonic_ns'], axis),
                         before, old_frequency))

    periods = np.diff(onsets)
    physical = 1/periods if periods.size else np.array([])
    output = []
    for number, (e, t, mode_before, previous) in enumerate(
        (c for c in enriched if c[0]['event'] == 'sf_ack'), start=1
    ):
        hz = float(e['frequency_hz'])
        position = next(i for i, x in enumerate(enriched) if x[0] is e)
        following = enriched[position+1:]
        next_control = following[0] if following else None
        nxt_t = next_control[1] if next_control else s1
        next_onset_index = int(np.searchsorted(onsets, t, side='right'))
        next_onset = (float(onsets[next_onset_index])
                      if next_onset_index < len(onsets) else None)
        guard = max(MIN_PREVIOUS_CYCLE_GUARD_S, 1/float(previous)) if (
            previous is not None and float(previous) > 0) else None
        if guard is None:
            eligible = np.array([], dtype=int)
        else:
            eligible = np.flatnonzero(
                (onsets[:-1] >= t + guard + margin)
                & (onsets[1:] <= nxt_t - margin)
                & (periods <= MAX_REGULAR_PERIOD_S)
                & (onsets[:-1] >= s0) & (onsets[1:] <= s1)
            )
        good = [int(i) for i in eligible if abs(physical[i]-hz) <=
                max(MATCH_ABSOLUTE_HZ, MATCH_RELATIVE_FRACTION*hz)]
        bad = [int(i) for i in eligible if int(i) not in good]
        in_window = h0 <= int(e['pc_monotonic_ns']) <= h1
        if not in_window:
            status = 'OUTSIDE_SCOPE_WINDOW'
        elif mode_before == 'ACK_INDICATED_MUTED':
            status = 'ACK_WHILE_CURRENT_MUTED'
        elif mode_before != 'ACK_INDICATED_ACTIVE':
            status = 'UNKNOWN_CURRENT_STATE'
        elif bad:
            status = 'STABLE_CONTRADICTION_REVIEW'
        elif good:
            status = 'STABLE_MATCHING_PERIOD_OBSERVED'
        elif next_onset is None or next_onset > s1:
            status = 'NO_FOLLOWING_APPROVED_PULSE'
        elif next_control and nxt_t < next_onset - margin and next_control[0]['event'] == 'sf_ack':
            status = 'NEXT_SF_ACK_BEFORE_NEXT_PULSE'
        elif next_control and nxt_t < next_onset - margin and (
            next_control[0]['event'] == 'sc_ack' and
                next_control[0].get('current_command') == 0):
            status = 'MUTE_ACK_BEFORE_NEXT_PULSE'
        elif guard is None or nxt_t-t < guard+margin:
            status = 'INSUFFICIENT_PRIOR_CYCLE_SETTLING'
        else:
            status = 'INSUFFICIENT_ISOLATED_PERIOD_EVIDENCE'
        req = paired.get(id(e))
        source_pos = (mcu_positions.get(int(req['source_sample_ns']))
                      if req and req.get('source_sample_ns') is not None else None)
        source_rate = (float(mcu['rates'][source_pos])
                       if source_pos is not None else None)
        output.append({
            'sf_ack_seq_all': number,
            'host_monotonic_ns': int(e['pc_monotonic_ns']),
            'scope_time_s_provisional': round(t, 9),
            'ack_hz': int(hz),
            'ack_indicated_current_state': mode_before,
            'previous_ack_hz': previous,
            'previous_cycle_guard_ms': round(guard*1000, 3) if guard else '',
            'request_host_monotonic_ns': req.get('pc_monotonic_ns', '') if req else '',
            'request_source_mcu_ns': req.get('source_sample_ns', '') if req else '',
            'request_frequency_hz': req.get('frequency_hz', '') if req else '',
            'request_pairing_verified': req is not None,
            'source_mcu_rate_hz': round(source_rate, 6) if source_rate is not None else '',
            'source_mcu_rate_rounded_matches_ack': (
                int(math.floor(source_rate+0.5)) == int(hz)
                if source_rate is not None else ''),
            'next_control_event': next_control[0]['event'] if next_control else '',
            'next_control_hz': next_control[0].get('frequency_hz', '') if next_control else '',
            'next_control_current': next_control[0].get('current_command', '') if next_control else '',
            'next_control_scope_s_provisional': round(nxt_t, 9),
            'next_approved_onset_scope_s': next_onset if next_onset is not None else '',
            'time_to_next_approved_onset_ms_provisional': (round((next_onset-t)*1000,3)
                if next_onset is not None else ''),
            'eligible_stable_intervals': len(eligible),
            'matching_stable_intervals': len(good),
            'contradicting_stable_intervals': len(bad),
            'witness_interval_ids': ';'.join(
                f'{onset_ids[i]}->{onset_ids[i+1]}' for i in good),
            'witness_period_ms': ';'.join(f'{1000*periods[i]:.3f}' for i in good),
            'classification': status,
            'physical_execution_proven': False,
            'scope_host_clock_qualified': False,
        })
    return output


def cycle_context(events, onsets, axis, *, onset_ids=None):
    """Per-cycle latest preceding SF ACK, SC state, and intervening controls."""
    rows = []
    onset_ids = (list(onset_ids) if onset_ids is not None
                 else [f'P{i+1:05d}' for i in range(len(onsets))])
    relevant = [e for e in events if e.get('event') in (
        'sf_ack', 'sc_ack', 'start_write_completed') and e.get('status', 'OK') == 'OK']
    relevant.sort(key=lambda x: int(x['pc_monotonic_ns']))
    mapped = [(host_to_scope(x['pc_monotonic_ns'], axis), x) for x in relevant]
    times = [x[0] for x in mapped]
    import bisect
    for i, (a, b) in enumerate(zip(onsets[:-1], onsets[1:])):
        dt = float(b-a)
        n = bisect.bisect_right(times, float(a))
        prior = mapped[:n]
        within = [x for t, x in mapped[n:] if t < b]
        sf = next((x for t, x in reversed(prior) if x['event'] in (
            'sf_ack', 'start_write_completed')), None)
        sc = next((x for t, x in reversed(prior) if x['event'] == 'sc_ack'), None)
        measured = 1/dt
        ack_hz = sf.get('frequency_hz') if sf else None
        is_muted = sc is not None and sc.get('current_command') == 0
        sc_transition = any(x['event'] == 'sc_ack' for x in within)
        if dt > MAX_REGULAR_PERIOD_S:
            label = 'LONG_PAUSE_NOT_CYCLE'
        elif is_muted:
            label = 'ACK_INDICATED_MUTED'
        elif sc_transition:
            label = 'SC_TRANSITION_DURING_CYCLE'
        elif ack_hz is None:
            label = 'NO_PRIOR_SF_ACK'
        elif abs(measured-float(ack_hz)) <= max(MATCH_ABSOLUTE_HZ, MATCH_RELATIVE_FRACTION*float(ack_hz)):
            label = 'MATCHES_LATEST_PRE_CYCLE_ACK_DIAGNOSTIC'
        else:
            label = 'DOES_NOT_MATCH_LATEST_PRE_CYCLE_ACK_REVIEW'
        rows.append({
            'start_id': onset_ids[i], 'end_id': onset_ids[i+1],
            'start_s': round(float(a), 9), 'end_s': round(float(b), 9),
            'period_ms': round(dt*1000, 4),
            'physical_frequency_hz': round(measured, 6),
            'latest_prior_sf_hz': ack_hz if ack_hz is not None else '',
            'latest_prior_sc_current': sc.get('current_command', '') if sc else '',
            'control_events_within_interval': ';'.join(
                f"{x['event']}:{x.get('frequency_hz', x.get('current_command',''))}"
                for x in within),
            'assessment': label,
        })
    return rows


def make_figures(folder, results, onsets, events, axis, mcu, margins, scenario_rows):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    s0, s1, h0, h1 = axis
    xx = (onsets[:-1]+onsets[1:])/2
    yy = 1/np.diff(onsets)
    regular = np.diff(onsets) <= MAX_REGULAR_PERIOD_S
    ack = [r for r in results if r['classification'] != 'OUTSIDE_SCOPE_WINDOW']
    sc = [e for e in events if e.get('event') == 'sc_ack' and e.get('status') == 'OK']
    sf = [e for e in events if e.get('event') == 'sf_ack' and e.get('status') == 'OK']
    # Provisional host mapping is used ONLY to place MCU/ACK context.
    mx = np.asarray([host_to_scope(v, axis) for v in mcu['pc_ns']])
    fig, axes = plt.subplots(3, 1, figsize=(16, 11), sharex=True,
                             layout='constrained')
    mask = (mx >= s0) & (mx <= s1)
    axes[0].plot(mx[mask], mcu['pressure'][mask], lw=1, label=f"MCU taxel {mcu['taxel']} pressure")
    axes[0].set_ylabel('Taxel (0–255)')
    axes[0].legend(loc='upper right')
    axes[1].plot(mx[mask], mcu['rates'][mask], '--', lw=1,
                 label=f"MCU {mcu['encoding']} request (provisional)")
    axes[1].scatter([r['scope_time_s_provisional'] for r in ack],
                    [r['ack_hz'] for r in ack], marker='|', s=60,
                    label='SF ACK (provisional)')
    axes[1].set_ylabel('Requested / ACK Hz')
    axes[1].legend(fontsize=9)
    axes[2].scatter(xx[regular], yy[regular], s=13, alpha=.75,
                    label='Approved CH1 inverse period')
    for e in sc:
        t = host_to_scope(e['pc_monotonic_ns'], axis)
        if s0 <= t <= s1:
            axes[2].axvline(t, ls='--', alpha=.4,
                            color='red' if e.get('current_command') == 0 else 'green')
    axes[2].set_ylabel('Physical 1/period (Hz)')
    axes[2].set_xlabel('Scope-local seconds (host context PROVISIONAL)')
    axes[2].legend(fontsize=9)
    for ax in axes:
        ax.grid(alpha=.2)
        ax.set_xlim(s0, s1)
    fig.suptitle('Physical frequency vs MCU requests and Matrix32 ACKs — unqualified host clock')
    fig.savefig(folder/'command_physical_timeline.png', dpi=150)
    plt.close(fig)

    counts = Counter(x['classification'] for x in results if x['classification'] != 'OUTSIDE_SCOPE_WINDOW')
    ordered = sorted(counts, key=lambda x: (-counts[x], x))
    fig, ax = plt.subplots(figsize=(12, 6.8), layout='constrained')
    ax.barh(ordered[::-1], [counts[x] for x in ordered[::-1]])
    ax.set_xlabel('Successful SF ACK count inside estimated scope window')
    ax.set_title('Per-ACK evidence category — not a device success/failure rate')
    for i, x in enumerate(ordered[::-1]):
        ax.text(counts[x]+.5, i, str(counts[x]), va='center')
    ax.set_xlim(0, max(counts.values(), default=0)*1.16+1)
    fig.savefig(folder/'ack_observability_categories.png', dpi=155)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 5), layout='constrained')
    c = Counter(x['classification'] for x in results)
    if any(r['matching_stable_intervals'] for r in results):
        witness = [r for r in results if r['matching_stable_intervals']]
        xs, ys = [], []
        for r in witness:
            for val in str(r['witness_period_ms']).split(';'):
                if val:
                    xs.append(float(r['ack_hz']))
                    ys.append(1000/float(val))
        ax.scatter(xs, ys, alpha=.55, s=17)
        maximum = max(xs+ys)
        ax.plot([0, maximum], [0, maximum], '--', color='black', lw=.8,
                label='Equal period (visual reference)')
        ax.legend()
    ax.set(xlabel='ACK-indicated SF (Hz)', ylabel='Approved physical inverse period (Hz)',
           title='Eligible matching intervals after preceding-cycle guard')
    ax.grid(alpha=.2)
    fig.savefig(folder/'ack_matched_cycles.png', dpi=155)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 5), layout='constrained')
    labels = [f'{margin:g} ms' for margin in margins]
    matched = [sum(x['classification'] == 'STABLE_MATCHING_PERIOD_OBSERVED'
                   for x in rows) for rows in scenario_rows]
    ambiguous = [sum(x['classification'] == 'STABLE_CONTRADICTION_REVIEW'
                     for x in rows) for rows in scenario_rows]
    ax.plot(labels, matched, 'o-', label='ACKs with matching stable interval')
    ax.plot(labels, ambiguous, 'o-', label='ACKs with contradictory stable interval')
    ax.set(ylabel='Number of ACKs', xlabel='Assumed host/scope offset margin',
           title='Evidence sensitivity to uncalibrated clock-offset assumptions')
    ax.legend()
    ax.grid(alpha=.2)
    fig.savefig(folder/'offset_sensitivity.png', dpi=155)
    plt.close(fig)

    # Inspect the longest TWO physical gaps in this acquisition, not all SC0
    # chatter. Do not interpret the long onset-to-onset gap as a low pulse rate.
    gaps = np.flatnonzero(np.diff(onsets) > MAX_REGULAR_PERIOD_S)
    selected = sorted(gaps, key=lambda i: onsets[i+1]-onsets[i], reverse=True)[:2]
    for serial, idx in enumerate(sorted(selected), 1):
        resume = float(onsets[idx+1])
        left, right = max(s0, resume-0.70), min(s1, resume+0.45)
        fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(14, 7), sharex=True,
                                      layout='constrained')
        visible = (xx>=left)&(xx<=right)&regular
        ax0.scatter(xx[visible], yy[visible], s=35,
                    label='Physical complete-pulse interval')
        ax0.axvline(resume, color='black', ls='--', label='First pulse after long gap')
        ax0.set_ylabel('CH1 inverse period (Hz)')
        ax0.legend(loc='upper left', fontsize=9)
        for e in sf:
            t = host_to_scope(e['pc_monotonic_ns'], axis)
            if left <= t <= right:
                ax1.scatter(t, e['frequency_hz'], marker='|', s=200, color='C0')
                ax1.annotate(f"SF{e['frequency_hz']}", (t, e['frequency_hz']),
                             xytext=(1, 4), textcoords='offset points',
                             rotation=55, fontsize=7)
        for e in sc:
            t = host_to_scope(e['pc_monotonic_ns'], axis)
            if left <= t <= right:
                ax1.axvline(t, ls=':', color='red' if e.get('current_command')==0
                            else 'green', alpha=.65)
                ax1.annotate(f"SC{e['current_command']}", (t, 0),
                             rotation=90, fontsize=8, xytext=(2, 0),
                             textcoords='offset points')
        ax1.set(xlabel='Scope-local time (s), host events provisionally mapped',
                ylabel='SF ACK (Hz)')
        ax1.grid(alpha=.2)
        ax0.grid(alpha=.2)
        fig.suptitle(f"Restart {serial}: long gap {(onsets[idx+1]-onsets[idx]):.3f} s; "
                     'no physical cycles inferred inside gap')
        filename = f'restart_{serial:02d}_detail.png'
        fig.savefig(folder/filename, dpi=155)
        plt.close(fig)
    return len(selected)


def run(run_dir, *, margin_ms=1.8, margins_ms=DEFAULT_MARGINS_MS):
    root = Path(run_dir).expanduser().resolve()
    metadata = load_json(root/'metadata.json')
    if metadata.get('plan', {}).get('integrated_experiment') != 'MATRIX32_MCU_NANO_SCOPE_V1':
        raise ValueError('Not an integrated Matrix32 scope recording')
    if metadata.get('status') != 'complete':
        raise ValueError('Only complete recordings may receive final reports')
    records = metadata.get('records', [])
    if len(records) != 1:
        raise ValueError('This report currently requires one independent scope record')
    record = records[0]
    approval, approved, onsets, negative_only = verify_approved_record(root, record)
    clock = next(x for x in load_json(root/'epoch_clock_analysis.json')['records']
                 if int(x['record_index']) == int(record['index']))
    axis = scope_axis(clock, record)
    trace_path, events = event_stream(root)
    mcu = mcu_stream(root)
    margins = sorted(set((*margins_ms, margin_ms)))
    onset_ids = [r['id'] for r in approved]
    scenario_rows = [classify_ack_rows(events, onsets, axis, margin,
                        onset_ids=onset_ids, mcu=mcu) for margin in margins]
    rows = classify_ack_rows(events, onsets, axis, margin_ms,
                             onset_ids=onset_ids, mcu=mcu)
    cycles = cycle_context(events, onsets, axis, onset_ids=onset_ids)
    inside = [x for x in rows if x['classification'] != 'OUTSIDE_SCOPE_WINDOW']
    counts = Counter(x['classification'] for x in inside)
    all_counts = Counter(x.get('event') for x in events)
    sf = [e for e in events if e.get('event') == 'sf_ack' and e.get('status') == 'OK']
    if len(rows) != len(sf):
        raise ValueError('Not all successful SF ACKs were accounted for')
    if len(onsets) != int(approval['included_onsets']):
        raise ValueError('Approval changed during report creation')
    out = root/'analysis'/'ack_physical_observability'
    out.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ['sf_ack_seq_all','classification']
    save_csv(out/'sf_ack_observability.csv', rows, fields)
    save_csv(out/'approved_cycle_ack_context.csv', cycles,
             list(cycles[0]) if cycles else ['start_id','end_id','assessment'])
    sensitivity = [{'margin_ms': margin,
                    'in_scope_ack_count': sum(x['classification'] != 'OUTSIDE_SCOPE_WINDOW'
                                              for x in scenario),
                    'categories': dict(Counter(x['classification'] for x in scenario
                                               if x['classification'] != 'OUTSIDE_SCOPE_WINDOW'))}
                   for margin, scenario in zip(margins, scenario_rows)]
    (out/'offset_sensitivity.json').write_text(json.dumps(sensitivity, indent=2)+'\n')
    restart_count = make_figures(out, rows, onsets, events, axis, mcu, margins,
                                 scenario_rows)
    robustness_path = root/'analysis'/'frequency_robustness_summary.json'
    robustness = load_json(robustness_path) if robustness_path.exists() else None
    robust_record = next((r for r in (robustness or {}).get('record_summaries', [])
                          if int(r['record']) == int(record['index'])), None)
    periods = np.diff(onsets)
    regular = periods <= MAX_REGULAR_PERIOD_S
    hashes = {'approved_onsets_sha256': approval['approved_onsets_sha256'],
              'source_ch1_sha256': approval['source_ch1_sha256'],
              'commands_sha256': digest(trace_path),
              'mcu_rates_sha256': digest(mcu['path']),
              'epoch_clock_analysis_sha256': digest(root/'epoch_clock_analysis.json')}
    summary = {'schema': 'matrix32_sf_ack_observability_v1',
               'run_id': metadata['run_id'], 'record_index': int(record['index']),
               'generated_utc': datetime.now(timezone.utc).isoformat(),
               'verified_human_approval': True,
               'approved_biphasic_negative_onsets': len(onsets),
               'approved_intervals': len(periods),
               'negative_only_rejected_by_reviewer': negative_only,
               'valid_nominal_active_range_intervals': int(np.count_nonzero(
                   (periods <= .5) & (periods >= .0025))),
               'sf_ack_success_all_session': len(sf),
               'sf_ack_success_in_provisional_scope_window': len(inside),
               'sf_ack_success_outside_scope_window': len(rows)-len(inside),
               'sf400_muted_scheduler_policy_ack_count': sum(
                   r['ack_hz'] == 400 and
                   r['source_mcu_rate_rounded_matches_ack'] is False for r in rows),
               'sf_source_rate_rounded_matches_ack': sum(
                   r['source_mcu_rate_rounded_matches_ack'] is True for r in rows),
               'sf_source_rate_verified_count': sum(
                   isinstance(r['source_mcu_rate_rounded_matches_ack'], bool)
                   for r in rows),
               'sf_ack_status_counts': dict(Counter(e.get('status') for e in events
                   if e.get('event') == 'sf_ack')),
               'event_counts_full_session': dict(all_counts),
               'ack_classifications_in_scope': dict(counts),
               'cycle_assessments': dict(Counter(r['assessment'] for r in cycles)),
               'clock_status': clock['status'],
               'scope_edge_fit_rms_us': clock.get('scope_edge_fit_rms_us'),
               'host_offset_margin_ms': margin_ms,
               'host_offset_margin_is_assumption_not_calibrated_bound': True,
               'comparison_hz_tolerance': 'max(1Hz,2.5% ACK Hz)',
               'previous_cycle_guard': 'max(5ms,1/previous acknowledged SF Hz)',
               'protocol_ack_is_not_physical_apply_proof': True,
               'no_physical_pulses_interpolated_across_long_gaps': True,
               'source_hashes': hashes,
               'existing_frequency_robustness': robust_record}
    (out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    lines = [
        f"# Final Matrix32 electrical frequency and SF ACK observability — {metadata['run_id']}",
        '', '**Source-locked operator-approved complete biphasic pulses only.** '
        'Electrical periods are computed from approved **negative-onset** timestamps; '
        'positive phases are confirmation, not alternative timing references.',
        '', '## 1. Evidence and scope', '',
        f"- {len(onsets)} approved complete biphasic pulses and {len(periods)} consecutive intervals. "
        f"{negative_only} reviewer-excluded negative-only events are retained in the review evidence.",
        f"- {len(sf)} successful SF acknowledgments in the full controller session; "
        f"{len(inside)} lie inside the **estimated** 20-second scope acquisition window.",
        f"- Scope/Nano clock status **{clock['status']}**, edge-fit RMS "
        f"{float(clock.get('scope_edge_fit_rms_us', float('nan'))):.1f} µs. "
        'The host offset is uncalibrated.',
        '- Every waveform, approval decision, and source-table hash is verified '
        'against the signed review before this report is created.',
        '', '![MCU requested rates, SF ACK, and approved physical periods]'
        '(command_physical_timeline.png)',
        '', 'The source-sample link is host-native: **'
        f"{summary['sf_source_rate_rounded_matches_ack']}/"
        f"{summary['sf_source_rate_verified_count']}** linked SF ACKs have"
        ' a command equal to the rounded rate of the exact named MCU sample. '
        f"The other **{summary['sf400_muted_scheduler_policy_ack_count']}** are"
        ' SF400 muted-scheduler commands, not ordinary tactile-rate requests '
        'or evidence of physical 400 Hz output. This does not establish '
        'physical execution.', 
        '', '## 2. Stable frequency tracking (existing approved robustness audit)', ''
    ]
    if robust_record:
        err = robust_record['baseline_frequency_error_hz']
        lines.extend([
            f"- **{robust_record['baseline_ack_stable']}** eligible ACK-stable intervals in "
            f"the robustness baseline; **{robust_record['baseline_ack_outside_tolerance']}** "
            'outside its diagnostic screening tolerance.',
            f"- Median absolute frequency difference **{err['median_abs']:.3f} Hz**; "
            f"95th percentile **{err['p95_abs']:.3f} Hz**; "
            f"maximum **{err['max_abs']:.3f} Hz** (eligible intervals only).",
            f"- **{robust_record['strict_ack_stable']}** intervals under the strict "
            'offset/previous-cycle guard scenario. The strict subset is much smaller.',
            '- This reuses the existing independent frequency audit; see '
            '[full robustness report](../frequency_robustness_report.md) and '
            '[original comparisons](../frequency_robustness_all_intervals.csv).',
        ])
    else:
        lines.append('No prior robustness audit was present. Run '
                     '`python -m oscilloscope.evaluate_clock_models --run-dir RUN --block-s 2` '
                     'then `python -m oscilloscope.audit_frequency_tracking --run-dir RUN` '
                     'to add the independent steady-state comparison.')
    lines.extend(['', '## 3. Did each SF ACK produce an observable complete cycle?', '',
                  '| Per-ACK classification | Count |', '| --- | ---: |'])
    for label, count in sorted(counts.items(), key=lambda x:(-x[1],x[0])):
        lines.append(f'| `{label}` | {count} |')
    lines.extend([
        f'| **Total ACKs inside estimated scope window** | **{len(inside)}** |',
        f'', f'Additionally, **{len(rows)-len(inside)}** successful SF ACKs occur '
        'outside the scope window. The table counts every eligible host-side ACK '
        '**once**. A matching physical period is compatibility evidence, not a '
        'unique command-execution proof. A superseded/unresolved label is not '
        'a Matrix32 command failure.', '',
        '![Per-ACK categories](ack_observability_categories.png)', '',
        '![Electrical cycles matching stable ACK frequencies](ack_matched_cycles.png)', '',
        '### Interpretation rules', '',
        '- `STABLE_MATCHING_PERIOD_OBSERVED`: at least one complete approved '
        'physical interval was observed after the ACK, the previous cycle guard '
        'and the assumed margin, and before the next control ACK.',
        '- `NEXT_SF_ACK_BEFORE_NEXT_PULSE`: another frequency ACK precedes '
        'the next observed pulse by more than the assumed margin. This is '
        '**consistent with supersession**, not proof of internal omission.',
        '- `ACK_WHILE_CURRENT_MUTED`: commanded current state was off; '
        'a frequency ACK alone does not imply stimulation.',
        '- `STABLE_CONTRADICTION_REVIEW`: eligible observed intervals do not '
        'agree and require a raw waveform/clock transition investigation.',
        '- Other categories mean insufficient observation, no definitive '
        'execution/failure verdict.',
        '', '## 4. Sensitivity to provisional host offset', '',
        f'The main classification uses a **±{margin_ms:g} ms offset margin**, '
        'alongside a prior-cycle settling guard of '
        '`max(5 ms, 1 / preceding SF frequency)`. **These margins are '
        'sensitivity assumptions, not measured guarantees.** The '
        'scope–Nano–host mapping remains unqualified.',
        '', '![Per-ACK classification margin sensitivity](offset_sensitivity.png)',
        '', '## 5. Physical restarts and deferred commands', '',
        'The following windows are selected from the two longest actual '
        'approved onset-to-onset gaps. The gaps themselves are **not** '
        'interpreted as low-frequency stimulation cycles.',
    ])
    for i in range(1, restart_count+1):
        lines.extend(['', f'### Restart {i}', '',
                      f'![Restart {i} cycle and command context](restart_{i:02d}_detail.png)'])
    lines.extend([
        '', 'During command bursts, several successful frequency ACKs can '
        'arrive within the duration of one electrically observed period. '
        'Intermediate frequencies then may have no independently observable '
        'complete cycle. This is consistent with cycle-boundary scheduling, '
        '**not a proof of device internals**. BLE writes may precede the ACK, '
        'so exact causal order near the clock margin is unresolved.',
        '', '## 6. Reproducibility and limitations', '',
        f'- Signed raw CH1 SHA-256: `{hashes["source_ch1_sha256"]}`.',
        f'- Source command trace SHA-256: `{hashes["commands_sha256"]}`.',
        '- [Every SF ACK, including outside-scope cases](sf_ack_observability.csv).',
        '- [Every approved interval with preceding-command context]'
        '(approved_cycle_ack_context.csv).',
        '- [Offset-margin scenarios](offset_sensitivity.json).',
        '- [Machine-readable summary](summary.json).',
        '- [Approved pulse review and excluded incomplete events]'
        '(../record_00001/pulse_review/report.md).',
        '- The CH1 interval is scope-native and requires no cross-device clock '
        'fit; command attribution is **provisional**.',
        '- A protocol ACK is not an independently calibrated electrical '
        'application marker. No physical output is inferred in unobserved '
        'scope gaps or between known pulses.',
        '- Neither a normal pulse morphology nor the software STOP proves '
        'electrical charge balance or physical safety.',
        '', '**Conclusion.** In eligible settled states, measured Matrix32 '
        'frequency agrees closely with the acknowledged value. Fast ACK bursts '
        'have a mixture of directly compatible cycles, plausible supersession, '
        'and inherently unresolved events. These categories must not be '
        'converted to a per-command physical execution percentage.',
    ])
    (out/'final_report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    overview = ('# Final integrated Matrix32 report\n\n'
                'The source-locked and operator-approved electrical evidence, '
                'frequency robustness audit, and per-ACK observability report '\
                'are combined here:\n\n'
                '- [Final per-ACK and physical frequency report]'
                '(ack_physical_observability/final_report.md)\n'
                '- [Frequency robustness analysis](frequency_robustness_report.md)\n'
                '- [Original integrated session report](integrated_report.md)\n\n'
                '![Physical frequency and command context]'
                '(ack_physical_observability/command_physical_timeline.png)\n\n'
                '![Per-ACK classifications]'
                '(ack_physical_observability/ack_observability_categories.png)\n\n'
                '**Cross-clock ACK attribution remains provisional.**\n')
    (root/'analysis'/'final_integrated_report.md').write_text(overview, encoding='utf-8')
    print('[ACK AUDIT] Verified operator approval and raw CH1 hash')
    print(f'[ACK AUDIT] {len(onsets)} approved biphasic onsets, {len(periods)} intervals')
    print(f'[ACK AUDIT] SF ACKs: {len(sf)} whole session; {len(inside)} in estimated scope window')
    print(f'[ACK AUDIT] Classifications: {dict(counts)}')
    print(f'[ACK AUDIT] Final report: {out/"final_report.md"}')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--margin-ms', type=float, default=1.8,
                        help='Assumed host/scope offset margin (not a calibration)')
    parser.add_argument('--sensitivity-margins-ms', type=float, nargs='+',
                        default=DEFAULT_MARGINS_MS)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.margin_ms) or args.margin_ms < 0 or
            any(not math.isfinite(x) or x < 0 for x in args.sensitivity_margins_ms)):
        parser.error('Margins must be finite, nonnegative milliseconds')
    return run(args.run_dir, margin_ms=args.margin_ms,
               margins_ms=args.sensitivity_margins_ms)


if __name__ == '__main__':
    main()
