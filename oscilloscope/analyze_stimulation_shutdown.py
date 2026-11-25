"""Audit signed negative-only CH1 events and SC0/SC50 shutdown/restart context.

An SC0 ACK is not a physical turn-off timestamp. Biphasic rejections remain
excluded from pulse/frequency inventories. Raw windows are diagnostic only.
"""
from __future__ import annotations
import argparse
from collections import Counter
from pathlib import Path

import numpy as np

from oscilloscope.extended_analysis_common import (
    csv_rows, digest, make_output, raw_windows_by_index, read_context,
    safe_figure_setup, save_csv, scope_sample_index, timed_acks, write_json,
)

WINDOW_BEFORE_US=350
WINDOW_AFTER_US=750


def compute_events(ctx):
    rejection = csv_rows(ctx['review_dir']/'rejected_onsets.csv')
    decisions = {r['id']:r for r in csv_rows(ctx['review_dir']/'review_decisions.csv')}
    review = [x for x in rejection if x['status']=='REJECTED_MISSING_POSITIVE_PHASE']
    if len(review)!=ctx['negative_only']:
        raise ValueError('Rejected source table disagrees with signed negative-only decisions')
    sc = timed_acks(ctx, 'sc_ack')
    sf = timed_acks(ctx, 'sf_ack')
    onsets = ctx['onsets']
    out = []
    for i,r in enumerate(review):
        # Match the immutable source event to the reviewer row by sample index.
        candidates=[d for d in decisions.values() if
                    d['sample_index']==r['sample_index'] and
                    d['detector_status']=='REJECTED_MISSING_POSITIVE_PHASE']
        if len(candidates)!=1 or candidates[0]['decision'].strip().upper()!='EXCLUDE':
            raise ValueError(f"Incomplete event at sample {r['sample_index']} is not explicitly excluded")
        rid = candidates[0]['id']
        t=float(r['time_s'])
        before = [x for x in sc if x['scope_s'] <= t]
        last = before[-1] if before else None
        upcoming = next((x for x in sc if x['scope_s']>t), None)
        n_before = int(np.searchsorted(onsets,t,side='left'))
        last_pulse = float(onsets[n_before-1]) if n_before>0 else None
        next_pulse = float(onsets[n_before]) if n_before<len(onsets) else None
        out.append(dict(
            id=rid, review_decision='EXCLUDE', sample_index=int(r['sample_index']),
            scope_s=t, negative_crossing_v=float(r['voltage_v']),
            expected_positive_threshold_v=float(r.get('positive_threshold_v') or 0),
            detector_positive_max_v=float(r.get('positive_max_v') or 0),
            signed_rejection_status=r['status'],
            last_sc_ack_value=last['event']['current_command'] if last else '',
            last_sc_ack_scope_s_provisional=round(last['scope_s'],9) if last else '',
            since_last_sc_ack_ms_provisional=round((t-last['scope_s'])*1000,3) if last else '',
            next_sc_ack_value=upcoming['event']['current_command'] if upcoming else '',
            next_sc_ack_scope_s_provisional=round(upcoming['scope_s'],9) if upcoming else '',
            last_approved_onset_scope_s=last_pulse if last_pulse is not None else '',
            next_approved_onset_scope_s=next_pulse if next_pulse is not None else '',
            local_min_v=None, local_max_v=None, local_positive_max_v=None,
            negative_duration_us=None, positive_phase_reconfirmed=False,
            physical_shutdown_time_proven=False,
        ))
    return out


def compute_sc_timeline(ctx, negative_rows):
    sc = timed_acks(ctx,'sc_ack')
    times=ctx['onsets']
    rejected_times=[r['scope_s'] for r in negative_rows]
    rows=[]
    for i,e in enumerate(sc):
        t=e['scope_s']; requested=int(e['event']['current_command'])
        next_sc=sc[i+1] if i+1<len(sc) else None
        end=next_sc['scope_s'] if next_sc else ctx['axis'][1]
        start_idx=int(np.searchsorted(times,t,side='right'))
        next_t=float(times[start_idx]) if start_idx<len(times) else None
        previous_t=float(times[start_idx-1]) if start_idx>0 else None
        until_next=[float(t0) for t0 in times if t<t0<end]
        negative_in_epoch=[float(t0) for t0 in rejected_times if t<t0<end]
        rows.append(dict(sc_seq=i+1, current_command=requested,
            scope_s_provisional=round(t,9),
            inside_scope=bool(ctx['axis'][0]<=t<=ctx['axis'][1]),
            next_sc_ack_scope_s_provisional=round(next_sc['scope_s'],9) if next_sc else '',
            host_ack_to_next_sc_ack_ms=round((end-t)*1000,3) if next_sc else '',
            preceding_approved_pulse_scope_s=previous_t if previous_t is not None else '',
            following_approved_pulse_scope_s=next_t if next_t is not None else '',
            ack_to_next_approved_pulse_ms_provisional=round((next_t-t)*1000,3)
                 if next_t is not None else '',
            approved_pulses_before_next_sc_ack=len(until_next),
            incomplete_negative_events_before_next_sc_ack=len(negative_in_epoch),
            physical_sc0_or_sc50_application_time_measured=False))
    return rows


def sc0_offset_sensitivity(ctx, negative_rows):
    """Shift the provisional SC ACK placement; do not change CH1 events."""
    offsets=(-3.5,-1.8,0.0,1.8,3.5)
    scenarios=[]
    for shift in offsets:
        sc=timed_acks(ctx,'sc_ack',shift_ms=shift)
        n=0
        for row in negative_rows:
            last=next((e for e in reversed(sc) if e['scope_s'] <= row['scope_s']),None)
            if last and int(last['event'].get('current_command',-1))==0:
                n+=1
        scenarios.append(dict(assumed_offset_shift_ms=shift,
                              incomplete_events_after_sc0_ack_provisional=n,
                              total_incomplete_events=len(negative_rows),
                              qualified_absolute_clock_error_bound=False))
    return scenarios


def fill_raw_morphology(ctx, rows):
    waveform=ctx['record']['channels']['CH1']['waveform']
    dt=float(waveform['sample_interval_s'])
    windows={r['id']:(max(0,r['sample_index']-int(WINDOW_BEFORE_US*1e-6/dt)),
                      r['sample_index']+int(WINDOW_AFTER_US*1e-6/dt)) for r in rows}
    raw=raw_windows_by_index(ctx['root']/ctx['record']['channels']['CH1']['path'],windows)
    for r in rows:
        t,v=raw[r['id']]
        if len(t)<3:
            raise ValueError(f"No original waveform samples found for {r['id']}")
        after=v[t >= r['scope_s']]
        if not len(after):
            raise ValueError('Onset outside raw diagnostic window')
        r['local_min_v']=float(v.min())
        r['local_max_v']=float(v.max())
        r['local_positive_max_v']=float(after.max())
        r['negative_duration_us']=float(np.count_nonzero(
            (t>=r['scope_s']) & (t<=r['scope_s']+550e-6) &
            (v < float(r['negative_crossing_v'])))*dt*1e6)
        # Not a new detector: immutable source says missing sustained phase.
        r['positive_phase_reconfirmed']=False
    return raw


def make_figures(ctx,out,negative,sc_rows,raw):
    plt=safe_figure_setup()
    fig,axes=plt.subplots(2,1,figsize=(16,8),sharex=True,layout='constrained')
    onsets=ctx['onsets']
    if len(onsets):
        axes[0].vlines(onsets,0,1,lw=.6,alpha=.5,label='Approved complete biphasic onset')
    if negative:
        axes[0].scatter([r['scope_s'] for r in negative],
                        [.5]*len(negative),s=45,marker='x',color='C3',
                        label='Signed EXCLUDE: negative-only event')
    for r in sc_rows:
        t=r['scope_s_provisional']
        if ctx['axis'][0] <= t <= ctx['axis'][1]:
            col='C3' if r['current_command']==0 else 'C2'
            axes[1].axvline(t,color=col,alpha=.7,ls='--')
    axes[0].set(ylabel='Observed CH1 events', ylim=(-.1,1.15))
    axes[1].set(ylabel='SC0 / SC50 ACK markers',yticks=[])
    axes[1].set_xlabel('Scope-local seconds; SC ACK timestamps provisionally mapped')
    axes[0].legend(loc='upper right',fontsize=8)
    for ax in axes:ax.grid(alpha=.15);ax.set_xlim(*ctx['axis'][:2])
    fig.suptitle('Shutdown/restart: biphasic pulses vs negative-only events and SC commands')
    fig.savefig(out/'figures/shutdown_overview.png',dpi=150);plt.close(fig)
    if negative:
        cols=3; nrow=int(np.ceil(len(negative)/cols))
        fig,axes=plt.subplots(nrow,cols,figsize=(15,3*nrow),layout='constrained')
        axes=np.atleast_1d(axes).ravel()
        for ax,r in zip(axes,negative):
            t,v=raw[r['id']]
            ax.plot((t-r['scope_s'])*1e6,v,lw=1)
            ax.axvline(0,ls=':',color='black')
            ax.axhline(r['expected_positive_threshold_v'],ls='--',alpha=.5)
            ax.set_title(f"{r['id']} at {r['scope_s']:.3f}s; SC{r['last_sc_ack_value']}",fontsize=9)
            ax.set_xlabel('Time after negative onset (µs)');ax.set_ylabel('CH1 (V)')
            ax.grid(alpha=.2)
        for ax in axes[len(negative):]:ax.axis('off')
        fig.suptitle('Original raw CH1 negative-only morphology (excluded by operator)')
        fig.savefig(out/'figures/incomplete_pulse_morphology.png',dpi=150);plt.close(fig)
    else:
        fig,ax=plt.subplots(figsize=(8,4));ax.text(.5,.5,'No signed negative-only events',
                 ha='center',va='center',transform=ax.transAxes);ax.axis('off')
        fig.savefig(out/'figures/incomplete_pulse_morphology.png');plt.close(fig)
    sensitivity=sc0_offset_sensitivity(ctx,negative)
    fig,ax=plt.subplots(figsize=(10,4.5),layout='constrained')
    ax.plot([x['assumed_offset_shift_ms'] for x in sensitivity],
            [x['incomplete_events_after_sc0_ack_provisional'] for x in sensitivity],
            'o-',label='Latest ACK is SC0 under assumed shift')
    ax.set(xlabel='Assumed SC ACK offset shift (ms)',
           ylabel='Operator-excluded negative-only event count',
           title='SC0 association sensitivity; shifts are NOT calibrated clock bounds')
    ax.set_ylim(-.5,max(len(negative)+1,1));ax.grid(alpha=.2);ax.legend(fontsize=9)
    fig.savefig(out/'figures/sc0_offset_sensitivity.png',dpi=145);plt.close(fig)
    details=[]
    for r in negative:
        t,v=raw[r['id']]
        fig,ax=plt.subplots(figsize=(11,4),layout='constrained')
        ax.plot((t-r['scope_s'])*1e6,v,lw=1,label='Original CH1 samples')
        ax.axvline(0,ls=':',color='black',label='Negative crossing')
        ax.axhline(r['expected_positive_threshold_v'],ls='--',color='C2',
                   label='Positive-phase detector threshold')
        ax.set(xlabel='Time relative to negative crossing (µs)',ylabel='Voltage (V)',
               title=f"{r['id']}: source-locked excluded negative-only event; "
                     'SC timing PROVISIONAL')
        ax.legend(fontsize=9);ax.grid(alpha=.2)
        name=f"figures/{r['id']}_raw.png"
        fig.savefig(out/name,dpi=150);plt.close(fig);details.append(name)
    return details


def run(run_dir):
    ctx=read_context(run_dir)
    out=make_output(ctx,'stimulation_shutdown')
    incomplete=compute_events(ctx)
    sc=compute_sc_timeline(ctx,incomplete)
    raw=fill_raw_morphology(ctx,incomplete)
    save_csv(out/'incomplete_events.csv',incomplete,list(incomplete[0]) if incomplete else ['id'])
    save_csv(out/'shutdown_restart_events.csv',sc,list(sc[0]) if sc else ['sc_seq'])
    figures=make_figures(ctx,out,incomplete,sc,raw)
    scenarios=sc0_offset_sensitivity(ctx,incomplete)
    write_json(out/'sc0_offset_sensitivity.json',scenarios)
    after_sc0=sum(r['last_sc_ack_value']==0 for r in incomplete)
    positive_count=sum(r['current_command']!=0 for r in sc)
    summary=dict(schema='matrix32_stimulation_shutdown_audit_v1',
       operator_excluded_negative_only=len(incomplete),
       negative_only_after_latest_sc0_ack_provisional=after_sc0,
       sc_ack_total=len(sc),sc0_ack_count=len(sc)-positive_count,
       sc_restore_ack_count=positive_count,
       approved_biphasic_onsets=len(ctx['onsets']),
       inferred_current_off_time_measured=False,
       figures=['figures/shutdown_overview.png','figures/incomplete_pulse_morphology.png','figures/sc0_offset_sensitivity.png',*figures],
       sc0_offset_sensitivity=scenarios,
       provenance=ctx['provenance'])
    write_json(out/'summary.json',summary)
    lines=['# Matrix32 current shutdown and restart', '',
      f"**{len(incomplete)}** source-locked negative-only events are explicitly EXCLUDED "
      'from the complete-pulse inventory by operator decision.',
      f"**{after_sc0}** of these occur after the latest acknowledged SC0 "
      '(provisional cross-clock ordering). This is an association, not '
      'a measured shutoff latency or delivered-charge assessment.','',
      f"This recording contains **{summary['sc0_ack_count']} SC0** and "
      f"**{positive_count} SC-restore** successful acknowledgments across "
      'the entire command trace. Only scope-window comparisons are physically observable.','',
      '![Electrical onsets and SC control ACKs](figures/shutdown_overview.png)','',
      '![Raw negative-only event morphology](figures/incomplete_pulse_morphology.png)','',
      '![SC0 association timing sensitivity](figures/sc0_offset_sensitivity.png)','',
      '## Event-level evidence','',
      '| Excluded ID | Time (s) | Negative crossing (V) | Positive maximum (V) | Latest SC | SC age (ms)* |',
      '| --- | ---: | ---: | ---: | ---: | ---: |',
      *[f"| {r['id']} | {r['scope_s']:.6f} | {r['negative_crossing_v']:.2f} | "
        f"{r['local_positive_max_v']:.2f} | {r['last_sc_ack_value']} | "
        f"{r['since_last_sc_ack_ms_provisional']} |" for r in incomplete],'',
      '*SC age uses the unqualified estimated scope-to-host clock alignment. '
      'It is not the physical time of current shutoff.', '',
      '## Original-sample details','',
      *[f'![Excluded negative-only waveform {r["id"]}]({pic})'
        for r,pic in zip(incomplete,figures)],'',
      '## Interpretation','',
      '- Incomplete events are reviewed source events, not auto-detected new pulses.',
      '- SC0 and SC50 are acknowledged commands. They do not prove electrical current zero, '
      'charge balance, or exact current-on/off times.',
      '- The 550-µs positive-phase rejection gate is retained; raw plots are diagnostic.',
      '- Repeated current transitions and short restarts can make naive post-SC windows '
      'inappropriate for physical latency inference.',
      '', '[Excluded event inventory](incomplete_events.csv) · '
      '[Every SC ACK](shutdown_restart_events.csv) · '
      '[SC0 offset scenarios](sc0_offset_sensitivity.json) · [provenance](summary.json).']
    from oscilloscope.report_figure_captions import add_figure_captions
    (out/'report.md').write_text(add_figure_captions('\n'.join(lines)+'\n'),encoding='utf-8')
    print(f"[SHUTDOWN] {len(incomplete)} negative-only events excluded; {after_sc0} after latest SC0 (provisional); {len(sc)} SC ACKs")
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path)
    args=p.parse_args(argv)
    return run(args.run_dir)

if __name__=='__main__':
    main()
