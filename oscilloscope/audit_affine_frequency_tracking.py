"""Read-only audit of approved CH1 intervals under declared clock hypotheses.

Writes only to an explicit output directory outside the source run.
No pulse-aligned clock fitting, threshold changes, source-file changes,
or new approvals are performed. Run from the repository root, or supply
--repo-dir when this script is stored outside the repository.
"""
import sys as _bootstrap_sys
from pathlib import Path as _bootstrap_path
_bootstrap_sys.path.insert(0, str(_bootstrap_path(__file__).resolve().parents[1]))
from experiment_analysis.paths import analysis_root
from pathlib import Path
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter

import numpy as np

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def error_summary(values):
    a = np.array(values, dtype=float)
    if not len(a):
        return {'n': 0}
    return {
        'n': len(a), 'mean_signed': float(np.mean(a)),
        'median_abs': float(np.median(abs(a))),
        'p90_abs': float(np.percentile(abs(a), 90)),
        'p95_abs': float(np.percentile(abs(a), 95)),
        'max_abs': float(np.max(abs(a))),
    }


def csv_write(path, rows):
    if not rows:
        # Reusing an audit directory must not retain another record's rows.
        path.write_text('', encoding='utf-8')
        return
    columns = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def checked_output_directory(run_dir, output_dir):
    """Refuse outputs inside the original run, including symlink aliases."""
    source = Path(run_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if output == source or source in output.parents:
        raise ValueError('Output directory must be outside the original run directory')
    return output


def audit(run_dir, output_dir, *, record_index=1, repo_dir=None):
    RUN = Path(run_dir).expanduser().resolve()
    OUT = checked_output_directory(RUN, output_dir)
    candidate = Path(__file__).resolve().parents[1]
    REPO = (Path(repo_dir).expanduser().resolve() if repo_dir is not None
            else candidate if (candidate/'oscilloscope').is_dir() else Path.cwd())
    if not (REPO/'oscilloscope').is_dir():
        raise ValueError('Cannot locate repository imports; use --repo-dir')
    sys.path.insert(0, str(REPO))
    import matplotlib
    matplotlib.use('Agg')
    from analyze_mcu_recording import choose_recording, load_recordings
    from oscilloscope.analyze_integrated_matrix32 import (
        acknowledged_frequency_states, read_events, tentative_host_map,
    )
    from oscilloscope.apply_pulse_review import load_approved_onsets
    from oscilloscope.align_gray_records import read_nano_frames
    from oscilloscope.clock_qualification_diagnostics import _frame_pairs
    from oscilloscope.offset_robust_command_matching import classify_interval, offset_scenarios

    meta = json.loads((RUN / 'metadata.json').read_text())
    records = [r for r in meta['records'] if int(r['index']) == record_index]
    if len(records) != 1:
        raise ValueError('Expected exactly one selected record')
    record = records[0]
    record_dir = analysis_root(RUN) / f'record_{record_index:05d}'
    review = record_dir / 'pulse_review'
    ch1 = RUN / record['channels']['CH1']['path']
    approval, onsets = load_approved_onsets(review, ch1)
    times = np.array([float(r['time_s']) for r in onsets])
    if len(times) < 2 or not np.all(np.diff(times) > 0):
        raise ValueError('Need at least two strictly ordered approved onsets')
    tracking = list(csv.DictReader((record_dir / 'approved_frequency_tracking.csv').open()))
    if len(tracking) != len(times)-1:
        raise ValueError('Archived frequency tracking interval count differs from approved source')
    for i, row in enumerate(tracking):
        if (row['start_onset_id'], row['end_onset_id']) != (onsets[i]['id'], onsets[i+1]['id']):
            raise ValueError('Archived tracking onset IDs differ from approved source')
        if abs(float(row['period_ms']) - 1000*(times[i+1]-times[i])) >= 1e-8:
            raise ValueError('Archived tracking period differs from approved source')

    mcu_dir = RUN / 'mcu_matrix32'
    if len(list(mcu_dir.glob('*tactile_rates.csv'))) != 1:
        raise ValueError('Expected one continuous MCU recording')
    mcu = load_recordings(mcu_dir, choose_recording(mcu_dir))
    configuration = mcu['experiment_settings']['configuration']['stimulation_source']
    taxel, encoding = int(configuration['taxel']), configuration['encoding']
    if taxel not in range(12) or encoding not in ('sa_ii','ra_i'):
        raise ValueError('Unsupported selected taxel or encoding')
    mcu_pc = np.asarray(mcu['taxel_pc_ns'], dtype=np.int64)
    mcu_hz = mcu['sa_rate_hz' if encoding == 'sa_ii' else 'ra_rate_hz'][:, taxel]
    order = np.argsort(mcu_pc, kind='stable')
    mcu_pc, mcu_hz = mcu_pc[order], mcu_hz[order]
    traces = list(mcu_dir.glob('*matrix32_commands.jsonl'))
    if len(traces) != 1:
        raise ValueError('Expected one Matrix32 command trace')
    trace = traces[0]
    events = read_events(trace)
    states = sorted(acknowledged_frequency_states(events), key=lambda r: int(r['pc_monotonic_ns']))
    ack_pc = np.array([int(r['pc_monotonic_ns']) for r in states], dtype=np.int64)
    epochs = [e for e in json.loads((RUN / 'epoch_clock_analysis.json').read_text())['records']
              if int(e['record_index']) == record_index]
    if len(epochs) != 1:
        raise ValueError('Expected one original clock epoch for selected record')
    epoch = epochs[0]
    mapping = tentative_host_map(RUN, record_index)
    if mapping is None:
        raise ValueError('Original affine scope-to-host map missing')
    archived_summary = json.loads((record_dir/'approved_frequency_summary.json').read_text())
    scope_start = float(archived_summary['record_time_start_s'])
    scope_end = float(archived_summary['record_time_end_s'])
    if not (np.isfinite(scope_start) and np.isfinite(scope_end) and scope_end > scope_start
            and times[0] >= scope_start and times[-1] <= scope_end):
        raise ValueError('Invalid archived scope time bounds')
    h0, h1 = mapping['first_sample_pc_monotonic_ns_est'], mapping['last_sample_pc_monotonic_ns_est']
    host = h0 + (times-scope_start)/(scope_end-scope_start)*(h1-h0)
    pairs, issue = _frame_pairs(RUN, record, read_nano_frames(RUN / 'nano_serial.jsonl'))
    if issue:
        raise ValueError(issue)
    gray_start, gray_end = min(r[0] for r in pairs), max(r[0] for r in pairs)
    support = (times >= gray_start) & (times <= gray_end)

    scenarios = []
    scenarios.append({'name': 'original_midpoint_all_intervals', 'deltas_ns': [0.], 'gray_support_required': False,
                      'extra_gray_margin_ms': 0., 'guards_ms': [5.]})
    scenarios.append({'name': 'midpoint_within_gray_support', 'deltas_ns': [0.], 'gray_support_required': True,
                      'extra_gray_margin_ms': 0., 'guards_ms': [5., 20., 50.]})
    for extra_ms in (0., .5, 1.):
        offsets, issue = offset_scenarios(epoch, extra_ms*1000, scale=1.)
        if issue:
            raise ValueError(issue)
        scenarios.append({'name': f'causal_offset_plus_{extra_ms:g}ms_gray_margin',
                          'deltas_ns': offsets['deltas_ns'], 'gray_support_required': True,
                          'extra_gray_margin_ms': extra_ms, 'guards_ms': [5., 20., 50.]})

    OUT.mkdir(parents=True, exist_ok=True)
    full_rows, summaries, archived_classification_mismatches = [], [], []
    baseline_status = {'STABLE_ACROSS_TESTED_OFFSETS': 'COMPATIBLE_DIAGNOSTIC_ONLY',
                       'TRANSITION_WITHIN_OFFSET_ENVELOPE': 'NOT_OBSERVABLE_SPANS_ACK_CHANGE',
                       'ACK_PRIOR_CYCLE_OR_UNKNOWN_GUARD': 'NOT_OBSERVABLE_PRIOR_CYCLE_GUARD',
                       'ACK_MUTED_OR_UNKNOWN': 'NOT_OBSERVABLE_MUTED_OR_UNKNOWN'}
    for scenario in scenarios:
        for guard in scenario['guards_ms']:
            rows = []
            for i, period in enumerate(np.diff(times)):
                measured = 1/float(period)
                row = {'hypothesis': scenario['name'], 'minimum_guard_ms': guard,
                       'shift_low_ms': min(scenario['deltas_ns'])/1e6,
                       'shift_high_ms': max(scenario['deltas_ns'])/1e6,
                       'additional_gray_margin_ms': scenario['extra_gray_margin_ms'],
                       'start_onset_id': onsets[i]['id'], 'end_onset_id': onsets[i+1]['id'],
                       'start_scope_s': float(times[i]), 'end_scope_s': float(times[i+1]),
                       'period_ms': float(period*1000), 'physical_inverse_period_hz': measured,
                       'range_flag': tracking[i]['period_range_flag'],
                       'original_diagnostic_class': tracking[i]['ack_state_assessment'],
                       'within_gray_frame_start_support': bool(support[i] and support[i+1]),
                       'clock_status': epoch['status'], 'calibrated_alignment': False,
                       'physical_application_latency_verified': False}
                if scenario['gray_support_required'] and not (support[i] and support[i+1]):
                    result = {'ack_assessment': 'OUTSIDE_GRAY_FRAME_SUPPORT',
                              'mcu_assessment': 'OUTSIDE_GRAY_FRAME_SUPPORT',
                              'ack_matching_diagnostic': 'NOT_COMPARABLE',
                              'all_scenarios_agree': False}
                else:
                    shifts = np.array(scenario['deltas_ns'])
                    result = classify_interval(host[i]+shifts, host[i+1]+shifts,
                                               mcu_pc=mcu_pc, mcu_rates=mcu_hz,
                                               ack_pc=ack_pc, ack_states=states,
                                               hold_s=guard/1000, measured_hz=measured)
                row.update(result)
                ack_hz = result.get('ack_expected_hz_if_stable')
                row['period_error_us_if_ack_stable'] = (float(period)*1e6-1e6/ack_hz if ack_hz else None)
                row['relative_frequency_error_pct_if_ack_stable'] = (100*(measured/ack_hz-1) if ack_hz else None)
                if scenario['name'] == 'original_midpoint_all_intervals':
                    observed_class = baseline_status.get(result['ack_assessment'],result['ack_assessment'])
                    if result['ack_matching_diagnostic'] == 'OUTSIDE_EXPLORATORY_TOLERANCE':
                        observed_class = 'POTENTIALLY_INCONSISTENT_REVIEW'
                    if observed_class != tracking[i]['ack_state_assessment']:
                        archived_classification_mismatches.append({'start_onset_id': onsets[i]['id'],
                                                                  'archived': tracking[i]['ack_state_assessment'],
                                                                  'recomputed': observed_class})
                rows.append(row)
            stable = [r for r in rows if r['ack_assessment'] == 'STABLE_ACROSS_TESTED_OFFSETS']
            stable_mcu = [r for r in rows if r['mcu_assessment'] == 'STABLE_ACROSS_TESTED_OFFSETS']
            both = [r for r in stable if r['mcu_assessment'] == 'STABLE_ACROSS_TESTED_OFFSETS']
            summaries.append({
                'hypothesis': scenario['name'], 'minimum_guard_ms': guard,
                'shift_low_ms': min(scenario['deltas_ns'])/1e6,
                'shift_high_ms': max(scenario['deltas_ns'])/1e6,
                'additional_gray_margin_ms': scenario['extra_gray_margin_ms'],
                'intervals_total': len(rows), 'ack_comparable': len(stable),
                'ack_comparable_fraction': len(stable)/len(rows),
                'ack_within_exploratory_tolerance': sum(r['ack_matching_diagnostic'] == 'WITHIN_EXPLORATORY_TOLERANCE' for r in stable),
                'ack_outside_exploratory_tolerance': sum(r['ack_matching_diagnostic'] == 'OUTSIDE_EXPLORATORY_TOLERANCE' for r in stable),
                'ack_abs_error_under_1hz': sum(abs(r['ack_error_hz_if_stable']) < 1 for r in stable),
                'ack_frequency_error_hz': error_summary([r['ack_error_hz_if_stable'] for r in stable]),
                'ack_period_error_us': error_summary([r['period_error_us_if_ack_stable'] for r in stable]),
                'ack_relative_frequency_error_pct': error_summary([r['relative_frequency_error_pct_if_ack_stable'] for r in stable]),
                'mcu_stable': len(stable_mcu), 'both_mcu_and_ack_stable': len(both),
                'mcu_frequency_error_hz': error_summary([r['mcu_error_hz_if_stable'] for r in stable_mcu]),
                'stable_mcu_ack_requested_difference_hz': error_summary([r['mcu_requested_hz_if_stable']-r['ack_expected_hz_if_stable'] for r in both]),
                'ack_class_counts': dict(Counter(r['ack_assessment'] for r in rows)),
                'mcu_class_counts': dict(Counter(r['mcu_assessment'] for r in rows)),
            })
            full_rows.extend(rows)
    csv_write(OUT/'frequency_offset_hypotheses_intervals.csv', full_rows)
    summary_csv = [{k:v for k,v in r.items() if not isinstance(v, dict)} | {
        'median_abs_ack_error_hz': r['ack_frequency_error_hz'].get('median_abs'),
        'p95_abs_ack_error_hz': r['ack_frequency_error_hz'].get('p95_abs'),
        'max_abs_ack_error_hz': r['ack_frequency_error_hz'].get('max_abs'),
        'median_abs_ack_period_error_us': r['ack_period_error_us'].get('median_abs'),
        'p95_abs_ack_period_error_us': r['ack_period_error_us'].get('p95_abs')
    } for r in summaries]
    csv_write(OUT/'frequency_offset_hypotheses_summary.csv', summary_csv)

    selected_events = [r for r in events if h0 <= int(r.get('pc_monotonic_ns', 0)) <= h1]
    scope_event = lambda e: scope_start+(int(e['pc_monotonic_ns'])-h0)/(h1-h0)*(scope_end-scope_start)
    gap = max(tracking, key=lambda r: float(r['period_ms']))
    context_start, context_end = float(gap['start_scope_s'])-.5, float(gap['end_scope_s'])+.5
    relevant = [e for e in selected_events if context_start <= scope_event(e) <= context_end and e.get('event') in ('sf_ack','sc_ack','sf_requested','sc_requested')]
    gap_context = []
    for e in relevant:
        row = {k:e[k] for k in ('event','pc_monotonic_ns','frequency_hz','current_command','status') if k in e}
        row['scope_time_s_provisional'] = scope_event(e)
        for key in ('ack_monotonic_s','write_begin_monotonic_s','completed_monotonic_s'):
            v = (e.get('evidence') or {}).get(key)
            if v is not None:
                row[key] = v
                row[key.replace('_monotonic_s','_scope_s_provisional')] = scope_start+(v*1e9-h0)/(h1-h0)*(scope_end-scope_start)
        gap_context.append(row)
    csv_write(OUT/'gap_command_context_provisional.csv', gap_context)
    # Pair request/ACK log events and check the originating recorded MCU value.
    # These durations are host-side protocol timings, not physical output latency.
    mcu_source_rows = list(csv.DictReader(next(mcu_dir.glob('*tactile_rates.csv')).open()))
    source_by_ns = {}
    for row in mcu_source_rows:
        source_by_ns.setdefault(int(row['pc_arrival_monotonic_ns']),[]).append(row)
    pending, pipeline_rows, pipeline_issues = None, [], []
    for event in events:
        name = event.get('event')
        if name == 'sf_requested':
            if pending is not None:
                pipeline_issues.append('SF request replaced before matching ACK')
            pending = event
        elif name in ('sf_failed','sf_ack'):
            request, pending = pending, None
            if name != 'sf_ack' or not h0 <= int(event['pc_monotonic_ns']) <= h1:
                continue
            if request is None or request['frequency_hz'] != event['frequency_hz']:
                pipeline_issues.append('SF ACK lacks an unambiguous preceding matching request')
                continue
            source = source_by_ns.get(int(request['source_sample_ns']),[])
            if len(source) != 1:
                pipeline_issues.append('SF request source timestamp does not select one MCU snapshot')
                continue
            rate = float(source[0][f"{'sa' if encoding == 'sa_ii' else 'ra'}_rate_hz_{taxel}"])
            expected = 0 if rate < 2 else min(400,int(rate+.5))
            fast = bool(request.get('zero_current_workaround'))
            evidence = event.get('evidence') or {}
            line = {'sf_ack_scope_s_provisional':scope_event(event),
                    'frequency_hz':event['frequency_hz'], 'source_rate_hz':rate,
                    'rounded_expected_hz':expected,'fast_zero_current_scheduler':fast,
                    'rounded_command_matches_source':expected==event['frequency_hz'] if not fast else None,
                    'source_to_request_ms':(request['pc_monotonic_ns']-request['source_sample_ns'])/1e6,
                    'request_to_logged_ack_ms':(event['pc_monotonic_ns']-request['pc_monotonic_ns'])/1e6}
            if evidence.get('ack_monotonic_s') is not None and evidence.get('write_begin_monotonic_s') is not None:
                line['write_to_ack_indication_ms'] = (evidence['ack_monotonic_s']-evidence['write_begin_monotonic_s'])*1000
                line['ack_indication_to_log_ms'] = (event['pc_monotonic_ns']/1e9-evidence['ack_monotonic_s'])*1000
            pipeline_rows.append(line)
    csv_write(OUT/'sf_request_source_audit.csv',pipeline_rows)
    pipeline_summary = {
        'paired_sf_acks_in_provisional_scope_window':len(pipeline_rows),
        'ordinary_commands_match_rounded_mcu_source':sum(r['rounded_command_matches_source'] is True for r in pipeline_rows),
        'ordinary_commands_differ_from_rounded_mcu_source':sum(r['rounded_command_matches_source'] is False for r in pipeline_rows),
        'fast_zero_current_scheduler_commands':sum(r['fast_zero_current_scheduler'] for r in pipeline_rows),
        'issues':pipeline_issues,
        'timing_definitions':'Host software source/request/write/ACK indications; not electrical response latency.',
    }
    for key in ('source_to_request_ms','request_to_logged_ack_ms','write_to_ack_indication_ms','ack_indication_to_log_ms'):
        values = np.array([r[key] for r in pipeline_rows if key in r])
        pipeline_summary[key] = ({'n':len(values),'min':float(min(values)),
                                  'median':float(np.median(values)),'p95':float(np.percentile(values,95)),
                                  'max':float(max(values))} if len(values) else {'n':0})
    source_files = [RUN/'metadata.json', RUN/'epoch_clock_analysis.json', RUN/'nano_serial.jsonl',
                    trace, next(mcu_dir.glob('*tactile_rates.csv')), next(mcu_dir.glob('*settings.json')),
                    review/'review_approval.json', review/'approved_onsets.csv', review/'approved_intervals.csv',
                    record_dir/'approved_frequency_tracking.csv',record_dir/'approved_frequency_summary.json']
    payload = {
        'schema': 'matrix32_baseline_frequency_offset_audit_v1', 'run_id': RUN.name,'record':record_index,
        'taxel':taxel,'encoding':encoding,
        'approved_source_verified_against_original_ch1': True,
        'source_ch1_sha256': approval['source_ch1_sha256'],
        'source_files_sha256': {str(p.relative_to(RUN)):digest(p) for p in source_files},
        'approved_onsets': len(times), 'approved_intervals': len(times)-1,
        'all_approved_intervals_retained_in_each_scenario': True,
        'source_locked_approval_unmodified': True,
        'original_clock_status': epoch['status'], 'original_affine_mapping_only': True,
        'clock_fitted_to_pulses': False, 'thresholds_relaxed': False,
        'archived_interval_classifications_reproduced':not archived_classification_mismatches,
        'archived_classification_mismatches':archived_classification_mismatches,
        'constant_shift_sign': 'Positive shifts map an unchanged physical onset to a later host time.',
        'scope_start_s': scope_start, 'scope_end_s': scope_end,
        'gray_frame_start_support_s': [gray_start,gray_end],
        'outside_gray_frame_support_onsets': int(np.sum(~support)),
        'outside_gray_frame_support_intervals': int(np.sum(~(support[:-1]&support[1:]))),
        'offset_constraint': epoch['offset_constraint'],
        'minimum_guard_definition': 'max(scenario minimum, preceding active ACK period); unknown prior state is ineligible',
        'full_continuous_uncertainty_envelope_checked': True,
        'screening_tolerance': 'max(8 Hz,15% of stable ACK frequency); descriptive screen, not acceptance criterion',
        'additional_gray_margins_ms': [.5,1.],
        'offset_scenarios_not_calibrated_error_bounds': True,
        'raw_command_event_counts_full_session': dict(Counter(e.get('event') for e in events)),
        'raw_command_event_counts_provisional_scope_window': dict(Counter(e.get('event') for e in selected_events)),
        'raw_ack_status_counts_provisional_scope_window': dict(Counter(e.get('status') for e in selected_events if e.get('event') in ('sf_ack','sc_ack'))),
        'longest_interval': gap,
        'sc_ack_provisional_scope_window': [{k:e[k] for k in ('current_command','pc_monotonic_ns','status')} | {'scope_time_s_provisional':scope_event(e)} for e in selected_events if e.get('event')=='sc_ack'],
        'scenarios': summaries,
        'sf_request_source_audit':pipeline_summary,
        'analysis_script_sha256':digest(Path(__file__)),
    }
    (OUT/'frequency_offset_hypotheses_summary.json').write_text(json.dumps(payload, indent=2)+'\n')

    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(13,5), layout='constrained')
    labels = ['Midpoint', 'Causal interval', '+0.5 ms margin', '+1.0 ms margin']
    names = ['midpoint_within_gray_support','causal_offset_plus_0ms_gray_margin','causal_offset_plus_0.5ms_gray_margin','causal_offset_plus_1ms_gray_margin']
    x = np.arange(len(names))
    for j,guard in enumerate([5.,20.,50.]):
        subset=[next(s for s in summaries if s['hypothesis']==n and s['minimum_guard_ms']==guard) for n in names]
        axes[0].bar(x+(j-1)*.25,[s['ack_comparable'] for s in subset], width=.25,label=f'Minimum guard {guard:g} ms')
    axes[0].set_xticks(x,labels,rotation=15)
    axes[0].set(ylabel=f'Comparable intervals out of {len(times)-1:,}',title='Eligibility under declared offset and guard hypotheses')
    axes[0].legend(fontsize=8)
    baseline=[r for r in full_rows if r['hypothesis']=='original_midpoint_all_intervals' and r['ack_assessment']=='STABLE_ACROSS_TESTED_OFFSETS']
    axes[1].scatter([(r['start_scope_s']+r['end_scope_s'])/2 for r in baseline],[r['period_error_us_if_ack_stable'] for r in baseline],s=10,alpha=.7)
    axes[1].axhline(0,color='black',lw=.7)
    axes[1].set(xlabel='Scope-local time (s)',ylabel='Measured − ACK-indicated period (µs)',title=f'Original provisional comparison: {len(baseline)} eligible intervals')
    for ax in axes: ax.grid(axis='y',alpha=.2)
    fig.suptitle(f"Approved negative-onset intervals; original {epoch['status']} clock map retained\nOffset margins are sensitivity assumptions, not calibrated bounds",fontsize=11)
    fig.savefig(OUT/'frequency_offset_hypotheses.png',dpi=160)
    plt.close(fig)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--record', type=int, default=1)
    parser.add_argument('--repo-dir', type=Path, default=None,
                        help='Repository root when running this script outside its repository')
    args = parser.parse_args(argv)
    result = audit(args.run_dir,args.output_dir,record_index=args.record,repo_dir=args.repo_dir)
    print(f"[AFFINE AUDIT] record {args.record}: {result['approved_onsets']} approved onsets; "
          f"{result['approved_intervals']} intervals retained in every hypothesis")
    for row in result['scenarios']:
        print(f"[AFFINE AUDIT] {row['hypothesis']}; minimum guard {row['minimum_guard_ms']:g} ms: "
              f"{row['ack_comparable']} ACK-comparable, {row['ack_outside_exploratory_tolerance']} "
              f"outside exploratory tolerance")
    print(f'[AFFINE AUDIT] Outputs: {args.output_dir.resolve()}')
    return result


if __name__ == '__main__':
    main()
