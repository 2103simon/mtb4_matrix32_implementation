"""Audit rapid SF changes against operator-approved biphasic CH1 periods.

No physical application time is inferred from a BLE acknowledgment. Interval
compatibility is a timing-sensitive *observation*, not proof of causation.
"""
from __future__ import annotations
import argparse
from collections import Counter
from pathlib import Path

import numpy as np

from oscilloscope.extended_analysis_common import (
    DEFAULT_MARGIN_MS, approved_intervals, compatible, digest, make_output,
    read_context, safe_figure_setup, save_csv, timed_acks, write_json,
)

OFFSETS_MS = (-3.5, -1.8, 0.0, 1.8, 3.5)


def evaluate(ctx, shift_ms=0):
    sf = timed_acks(ctx, 'sf_ack', shift_ms=shift_ms)
    sc = timed_acks(ctx, 'sc_ack', shift_ms=shift_ms)
    start = [e for e in ctx['events'] if e.get('event') == 'start_write_completed']
    start_at = (ctx['axis'][0] + (int(start[0]['pc_monotonic_ns'])-ctx['axis'][2]) /
                (ctx['axis'][3]-ctx['axis'][2])*(ctx['axis'][1]-ctx['axis'][0]) +
                shift_ms/1000) if start else None
    result = []
    sf_i = sc_i = 0
    for it in approved_intervals(ctx):
        a, b, hz = it['start_s'], it['end_s'], it['measured_hz']
        while sf_i < len(sf) and sf[sf_i]['scope_s'] <= a:
            sf_i += 1
        while sc_i < len(sc) and sc[sc_i]['scope_s'] <= a:
            sc_i += 1
        before_sf = sf[sf_i-1] if sf_i else None
        before_sc = sc[sc_i-1] if sc_i else None
        during_sf = []
        j = sf_i
        while j < len(sf) and sf[j]['scope_s'] < b:
            during_sf.append(sf[j]); j += 1
        during_sc = []
        j = sc_i
        while j < len(sc) and sc[j]['scope_s'] < b:
            during_sc.append(sc[j]); j += 1
        previous = (float(before_sf['event']['frequency_hz']) if before_sf else None)
        matches_prior = compatible(hz, previous)
        match_inside = [r for r in during_sf if compatible(hz, r['event']['frequency_hz'])]
        state = ('UNKNOWN' if before_sc is None and
                 (start_at is None or start_at > a) else
                 'MUTED' if before_sc is not None and before_sc['event'].get('current_command') == 0
                 else 'ACTIVE')
        if not it['is_regular']:
            status = 'LONG_GAP_NOT_A_STIMULATION_PERIOD'
        elif state == 'MUTED':
            status = 'SC0_CONTEXT_NOT_RATE_EVIDENCE'
        elif during_sc:
            status = 'SC_TRANSITION_WITHIN_INTERVAL'
        elif state == 'UNKNOWN':
            status = 'UNKNOWN_CURRENT_STATE'
        elif matches_prior:
            status = 'PRIOR_ACK_COMPATIBLE'
        elif match_inside:
            status = 'IN_INTERVAL_ACK_COMPATIBLE_ONLY'
        elif during_sf:
            status = 'IN_INTERVAL_ACKS_NONE_COMPATIBLE'
        elif previous is None:
            status = 'NO_PRIOR_SF_ACK'
        else:
            status = 'NO_ACK_COMPATIBLE_REVIEW'
        result.append({
            **{k:it[k] for k in ('index','start_id','end_id','start_s','end_s','period_s','measured_hz')},
            'regular_period': bool(it['is_regular']),
            'classification': status,
            'current_state_at_start_provisional': state,
            'prior_sf_ack_hz': previous if previous is not None else '',
            'prior_sf_ack_scope_s_provisional': round(before_sf['scope_s'],9) if before_sf else '',
            'matches_prior_ack': bool(matches_prior),
            'in_interval_sf_ack_count': len(during_sf),
            'in_interval_sf_ack_hz': ';'.join(str(x['event']['frequency_hz']) for x in during_sf),
            'in_interval_sf_ack_scope_s_provisional': ';'.join(f"{x['scope_s']:.9f}" for x in during_sf),
            'in_interval_matching_sf_ack_count': len(match_inside),
            'in_interval_matching_hz': ';'.join(str(x['event']['frequency_hz']) for x in match_inside),
            'in_interval_sc_ack_count': len(during_sc),
            'clock_offset_shift_ms_assumed': float(shift_ms),
            'physical_apply_time_proven': False,
        })
    return result


def make_figures(ctx, out, rows, scenarios, *, detail_limit=12):
    plt = safe_figure_setup()
    fig, axes = plt.subplots(2, 1, figsize=(16,9), sharex=True, layout='constrained')
    physical = [r for r in rows if r['regular_period']]
    axes[0].scatter([(r['start_s']+r['end_s'])/2 for r in physical],
                    [r['measured_hz'] for r in physical], s=13, alpha=.72,
                    label='Approved CH1 frequency')
    focus = [r for r in rows if r['classification'] in
             ('IN_INTERVAL_ACK_COMPATIBLE_ONLY','IN_INTERVAL_ACKS_NONE_COMPATIBLE','NO_ACK_COMPATIBLE_REVIEW')]
    axes[0].scatter([(r['start_s']+r['end_s'])/2 for r in focus],
                    [r['measured_hz'] for r in focus], s=35, marker='x',
                    label='Transition-sensitive (not execution failures)')
    sf = timed_acks(ctx, 'sf_ack')
    s0, s1 = ctx['axis'][:2]
    sf = [x for x in sf if s0 <= x['scope_s'] <= s1]
    # SF400 while SC0 is the controller's mute-scheduler sentinel, not
    # physical 400-Hz stimulation. Keep it in CSV evidence but omit its
    # very high y-value so the ordinary ~2–110 Hz changes remain readable.
    sc_for_mute = timed_acks(ctx, 'sc_ack')
    visible, hidden_muted_400 = [], 0
    for ack in sf:
        preceding = next((x for x in reversed(sc_for_mute)
                          if x['host_ns'] <= ack['host_ns']), None)
        if (int(ack['event']['frequency_hz']) == 400 and preceding is not None
                and int(preceding['event']['current_command']) == 0):
            hidden_muted_400 += 1
        else:
            visible.append(ack)
    sf = visible
    axes[1].text(.01,.96,f'{hidden_muted_400} muted-scheduler SF400 ACKs omitted from y-axis',
                 transform=axes[1].transAxes,va='top',fontsize=9)
    axes[1].scatter([x['scope_s'] for x in sf],
                    [float(x['event']['frequency_hz']) for x in sf],
                    marker='|', s=120, label='SF ACK (provisional position)')
    axes[0].set_ylabel('Approved physical Hz')
    axes[1].set_ylabel('ACK-indicated SF (Hz)')
    axes[1].set_xlabel('Scope-local seconds; cross-device alignment unqualified')
    axes[0].legend(fontsize=9)
    axes[1].legend(fontsize=9)
    for ax in axes:
        ax.grid(alpha=.2); ax.set_xlim(s0, s1)
    fig.suptitle('Cycle-to-cycle frequency transitions: ACK position does not establish application')
    fig.savefig(out/'figures/transition_overview.png', dpi=150); plt.close(fig)

    classes = sorted({r['classification'] for scenario in scenarios for r in scenario})
    base = Counter(r['classification'] for r in rows)
    fig, ax = plt.subplots(figsize=(13,6), layout='constrained')
    for label in classes:
        if label in ('PRIOR_ACK_COMPATIBLE','IN_INTERVAL_ACK_COMPATIBLE_ONLY',
                     'IN_INTERVAL_ACKS_NONE_COMPATIBLE','NO_ACK_COMPATIBLE_REVIEW'):
            ax.plot(OFFSETS_MS, [sum(r['classification']==label for r in case)
                                 for case in scenarios], 'o-', label=label)
    ax.set(xlabel='Assumed additional host-to-scope offset shift (ms)',
           ylabel='Approved interval count',
           title='Clock-offset sensitivity: scenario assumptions, NOT calibrated error bounds')
    ax.grid(alpha=.2); ax.legend(fontsize=8)
    fig.savefig(out/'figures/transition_timing_sensitivity.png', dpi=150); plt.close(fig)

    details = sorted(focus, key=lambda r: (
        0 if r['classification']=='IN_INTERVAL_ACK_COMPATIBLE_ONLY' else 1,
        -r['in_interval_sf_ack_count'], r['index']))[:detail_limit]
    for count, focus_row in enumerate(details,1):
        a, b = float(focus_row['start_s']), float(focus_row['end_s'])
        pad = max(.08, min(.18, 2*(b-a)))
        left, right = max(s0,a-pad), min(s1,b+pad)
        local = [r for r in rows if r['regular_period'] and
                 (r['start_s']+r['end_s'])/2 >= left and
                 (r['start_s']+r['end_s'])/2 <= right]
        events = [e for e in sf if left <= e['scope_s'] <= right]
        fig, axes = plt.subplots(2,1,figsize=(13,6.5),sharex=True,layout='constrained')
        axes[0].plot([(r['start_s']+r['end_s'])/2 for r in local],
                     [r['measured_hz'] for r in local], 'o-', lw=.8,
                     label='Approved physical inverse period')
        for x in (a,b):
            axes[0].axvline(x, color='black', alpha=.4, ls=':')
            axes[1].axvline(x, color='black', alpha=.4, ls=':')
        axes[1].scatter([x['scope_s'] for x in events],
                        [x['event']['frequency_hz'] for x in events],s=100,marker='|')
        for x in events:
            axes[1].annotate(f"SF{x['event']['frequency_hz']}",
                (x['scope_s'],float(x['event']['frequency_hz'])),
                xytext=(2,5),textcoords='offset points',fontsize=8)
        axes[0].set_ylabel('Physical frequency (Hz)')
        axes[1].set_ylabel('SF ACK (Hz)')
        axes[1].set_xlabel('Scope-local seconds; ACK positions provisional')
        axes[0].grid(alpha=.2); axes[1].grid(alpha=.2)
        axes[0].legend(fontsize=8)
        fig.suptitle(f"{focus_row['start_id']}→{focus_row['end_id']}: "
                     f"{focus_row['measured_hz']:.2f} Hz; {focus_row['classification']}")
        fig.savefig(out/f'figures/transition_{count:02d}.png',dpi=145);plt.close(fig)
    return [f'figures/transition_{i:02d}.png' for i in range(1,len(details)+1)]


def run(run_dir):
    ctx = read_context(run_dir)
    out = make_output(ctx, 'frequency_transitions')
    rows = evaluate(ctx)
    scenarios = [evaluate(ctx, shift_ms=x) for x in OFFSETS_MS]
    if any(len(s)!=len(rows) for s in scenarios):
        raise ValueError('Transition offset sweep changed approved interval inventory')
    save_csv(out/'transitions.csv',rows,list(rows[0]) if rows else ['classification'])
    changes = [r for r in rows if r['regular_period'] and not r['matches_prior_ack']]
    save_csv(out/'transition_classifications.csv', changes,
             list(rows[0]) if rows else ['classification'])
    sensitivity = [{'shift_ms_assumed':off, 'counts':dict(Counter(x['classification'] for x in scenario))}
                   for off,scenario in zip(OFFSETS_MS,scenarios)]
    write_json(out/'offset_sensitivity.json', sensitivity)
    detail_images = make_figures(ctx,out,rows,scenarios)
    counts = dict(Counter(x['classification'] for x in rows))
    summary = dict(schema='matrix32_frequency_transition_audit_v1',
        approved_intervals=len(rows), regular_intervals=sum(x['regular_period'] for x in rows),
        prior_ack_mismatches_regular=len(changes),
        mismatches_with_in_interval_sf_ack=sum(x['in_interval_sf_ack_count']>0 for x in changes),
        mismatches_with_compatible_in_interval_sf_ack=sum(x['in_interval_matching_sf_ack_count']>0 for x in changes),
        active_transition_in_interval_ack_match_count=counts.get('IN_INTERVAL_ACK_COMPATIBLE_ONLY',0),
        classification_counts=counts,offset_sensitivity=sensitivity,
        figures=['figures/transition_overview.png','figures/transition_timing_sensitivity.png',*detail_images],
        provenance=ctx['provenance'])
    write_json(out/'summary.json',summary)
    lines=['# Matrix32 approved frequency transitions', '',
      f"**{len(rows)}** signed onset-to-onset intervals; **{len(changes)}** regular intervals "
      'not compatible with the last preceding SF ACK under the nominal provisional map.',
      '', f"Of the **{len(changes)}** mismatches, "
      f"**{summary['active_transition_in_interval_ack_match_count']}** are active "
      'intervals without SC transitions that match an in-interval SF ACK. '
      'The other mismatches have muted or SC-transition context. '
      f"Overall **{summary['mismatches_with_compatible_in_interval_sf_ack']}** "
      'mismatched intervals have a compatible in-interval ACK, including SC context. '
      'Compatibility does not prove the late ACK caused an already-running cycle.', '',
      '## Evidence classification','',
      '| Category | Intervals |','| --- | ---: |',
      *(f'| {key} | {n} |' for key,n in sorted(counts.items())), '',
      '![Approved physical frequency and SF ACK timing](figures/transition_overview.png)','',
      '![Clock offset sensitivity](figures/transition_timing_sensitivity.png)','',
      '## Detailed transitions','',
      *[f'![Transition detail {i}]({pic})' for i,pic in enumerate(detail_images,1)],'',
      '## Interpretation and limits','',
      '- Physical periods come **only** from successive approved biphasic CH1 onsets.',
      '- Periods longer than 0.5 s are gaps, not active stimulation cycles.',
      '- The prior-ACK versus in-interval-ACK comparison is descriptive, not a '
      'calibrated command-application timing estimate.',
      '- The ±3.5 / ±1.8 ms offset changes are sensitivity *scenarios*, not measured bounds.',
      '- Changing an ACK timestamp by an assumed offset changes category membership '
      'but never the raw electrical interval.',
      '- Do not infer from a match that the corresponding ACK was physically executed.',
      '', '[Every approved interval](transitions.csv) · '
      '[transition subset](transition_classifications.csv) · '
      '[numeric scenario sweep](offset_sensitivity.json) · '
      '[source-locked provenance](summary.json).']
    (out/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(f"[TRANSITIONS] {len(rows)} approved intervals; {len(changes)} prior-ACK mismatches; {summary['mismatches_with_compatible_in_interval_sf_ack']} compatible in-interval ACKs")
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path)
    args=p.parse_args(argv)
    return run(args.run_dir)

if __name__=='__main__':
    main()
