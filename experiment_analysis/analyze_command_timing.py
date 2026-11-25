"""Host-clock SF/SC request→ACK latency, burst pressure and MCU source audit.

Request/ACK latency uses one host monotonic clock and is NOT physical
Matrix32 command-application latency. Period comparisons are scheduling
opportunities, not evidence that a command executed electrically.
"""
from __future__ import annotations
import argparse
import csv
from collections import Counter
from pathlib import Path
import math

import numpy as np

from experiment_analysis.extended_analysis_common import (
    make_output, quantiles, read_context, safe_figure_setup, save_csv, write_json,
)


def pair_commands(events, prefix):
    """Pair corresponding request and ACK by trace order, fail closed on ambiguity."""
    req=[e for e in events if e.get('event')==prefix+'_requested']
    ack=[e for e in events if e.get('event')==prefix+'_ack']
    if len(req)!=len(ack):
        raise ValueError(f'{prefix.upper()} request/ACK count differs; refusing positional pairing')
    result=[]
    field='frequency_hz' if prefix=='sf' else 'current_command'
    for i,(r,a) in enumerate(zip(req,ack),1):
        start=int(r['pc_monotonic_ns']); end=int(a['pc_monotonic_ns'])
        if r.get(field)!=a.get(field) or end<start:
            raise ValueError(f'{prefix.upper()} request/ACK mismatch at sequence {i}')
        if i>1 and int(req[i-2]['pc_monotonic_ns'])>start:
            raise ValueError('Out-of-order host command requests')
        result.append((i,r,a))
    return result


def analyze_host_events(ctx):
    events=ctx['events']
    sf=pair_commands(events,'sf')
    sc=pair_commands(events,'sc')
    prev_sf_ns=None
    previous_sf_hz=None
    sc_acks=[e for e in events if e.get('event')=='sc_ack' and e.get('status')=='OK']
    sc_acks.sort(key=lambda e:int(e['pc_monotonic_ns']))
    sc_i=0
    latest_sc=None
    mcu_index={int(ns):i for i,ns in enumerate(ctx['mcu']['pc_ns'])}
    sf_rows=[]
    for seq, req, ack in sf:
        start=int(req['pc_monotonic_ns']); end=int(ack['pc_monotonic_ns'])
        while sc_i<len(sc_acks) and int(sc_acks[sc_i]['pc_monotonic_ns']) <= end:
            latest_sc=sc_acks[sc_i];sc_i+=1
        active = (latest_sc is None or int(latest_sc['current_command'])!=0)
        hz=int(ack['frequency_hz'])
        delta_ms=((end-prev_sf_ns)/1e6 if prev_sf_ns is not None else None)
        prior_period_ms=(1000/previous_sf_hz if active and previous_sf_hz else None)
        ratio=(delta_ms/prior_period_ms if delta_ms is not None and prior_period_ms else None)
        evidence=ack.get('evidence') or {}
        nested=evidence.get('ack') or {}
        write_sec=nested.get('write_begin_monotonic_s')
        ack_sec=nested.get('ack_monotonic_s')
        write_to_ack_ms=(1000*(ack_sec-write_sec) if isinstance(write_sec,(int,float))
                          and isinstance(ack_sec,(int,float)) and ack_sec>=write_sec else None)
        source_ns=req.get('source_sample_ns')
        i=mcu_index.get(int(source_ns)) if source_ns is not None else None
        source_hz=float(ctx['mcu']['rates'][i]) if i is not None else None
        scheduler_muted = bool(not active and hz==400)
        rounded_matches=(int(math.floor(source_hz+0.5))==hz
                         if source_hz is not None and not scheduler_muted else None)
        sf_rows.append(dict(
            sf_seq=seq, request_host_ns=start, ack_host_ns=end, sf_hz=hz,
            ack_status=ack.get('status',''), request_to_ack_ms=(end-start)/1e6,
            ble_write_to_ack_ms=write_to_ack_ms,
            preceding_sf_ack_spacing_ms=delta_ms,
            prior_ack_frequency_hz=previous_sf_hz,
            prior_active_cycle_period_ms=prior_period_ms,
            ack_spacing_divided_by_prior_period=ratio,
            ack_faster_than_one_prior_cycle=(ratio<1 if ratio is not None else ''),
            latest_ack_current_command=(latest_sc['current_command'] if latest_sc else ''),
            state_at_sf_ack=('ACK_INDICATED_ACTIVE' if active else 'ACK_INDICATED_MUTED'),
            is_sf400_muted_scheduler=scheduler_muted,
            source_mcu_monotonic_ns=source_ns or '',
            source_mcu_rate_hz=source_hz if source_hz is not None else '',
            rounded_source_hz_equals_sf=rounded_matches if rounded_matches is not None else '',
            physical_application_latency_measured=False,
        ))
        prev_sf_ns=end
        previous_sf_hz=hz
    sc_rows=[]
    for seq,req,ack in sc:
        start=int(req['pc_monotonic_ns']);end=int(ack['pc_monotonic_ns'])
        evidence=ack.get('evidence') or {}
        nested=evidence.get('ack') or {}
        write_sec=nested.get('write_begin_monotonic_s')
        ack_sec=nested.get('ack_monotonic_s')
        sc_rows.append(dict(sc_seq=seq,current_command=int(ack['current_command']),
            request_host_ns=start,ack_host_ns=end,ack_status=ack.get('status',''),
            request_to_ack_ms=(end-start)/1e6,
            ble_write_to_ack_ms=(1000*(ack_sec-write_sec)
                                 if isinstance(write_sec,(int,float)) and
                                 isinstance(ack_sec,(int,float)) and ack_sec>=write_sec
                                 else None),physical_application_latency_measured=False))
    return sf_rows,sc_rows


def physical_command_density(ctx):
    """Host/SCOPE association is provisional, so density is descriptive only."""
    from experiment_analysis.extended_analysis_common import approved_intervals, host_to_scope
    # Every interval, including long gaps, is retained in exported table.
    intervals=approved_intervals(ctx)
    sf=[e for e in ctx['events'] if e.get('event')=='sf_ack' and e.get('status')=='OK']
    host_scope=np.asarray([host_to_scope(e['pc_monotonic_ns'],ctx['axis']) for e in sf])
    rows=[]
    for it in intervals:
        n=int(np.count_nonzero((host_scope>it['start_s'])&(host_scope<it['end_s'])))
        rows.append(dict(start_id=it['start_id'],end_id=it['end_id'],
           start_scope_s=it['start_s'],end_scope_s=it['end_s'],
           measured_period_ms=it['period_s']*1000,regular_period=it['is_regular'],
           sf_acks_during_interval_provisional=n,
           more_than_one_sf_ack_during_period_provisional=n>1,
           clock_alignment_qualified=False))
    return rows



# All PC / BLE timings are intervals on the host monotonic clock.
# MCU corrected timestamp deltas are separately reported source-clock intervals.
CADENCE_LABELS = (
    ('mcu_arrival_dt_ms', 'MCU arrivals'),
    ('mcu_corrected_dt_ms', 'MCU source timestamps'),
    ('sf_request_dt_ms', 'SF requests'),
    ('sf_ble_write_dt_ms', 'SF BLE writes'),
    ('sf_ack_dt_ms', 'SF ACKs'),
)
LATENCY_LABELS = (
    ('mcu_arrival_to_sf_request_ms', 'MCU arrival → SF request'),
    ('sf_request_to_ble_write_ms', 'SF request → BLE write'),
    ('sf_ble_write_to_ack_ms', 'SF BLE write → ACK'),
    ('sf_request_to_ack_ms', 'SF request → ACK'),
    ('sc_request_to_ack_ms', 'SC request → ACK'),
)


def _positive_differences_ns(values, name):
    values=np.asarray(values,dtype=np.int64)
    gaps=np.diff(values)
    if np.any(gaps <= 0):
        raise ValueError(f'Non-increasing {name} timestamps; cannot report cadence')
    return gaps.astype(float)/1e6


def compute_timing_distributions(ctx,sf,sc):
    """Host-arrival cadence, actual BLE-write cadence and exchange stages.

    MCU corrected timestamps are source-clock differences with 1-ms
    granularity, not aligned host/BLE timestamps. Missing BLE transport phase
    evidence is omitted rather than inferred from host ACK times.
    """
    data={
        'mcu_arrival_dt_ms':_positive_differences_ns(
            ctx['mcu']['pc_ns'],'MCU PC-arrival'),
        'sf_request_dt_ms':_positive_differences_ns(
            [r['request_host_ns'] for r in sf],'SF request'),
        'sf_ack_dt_ms':_positive_differences_ns(
            [r['ack_host_ns'] for r in sf],'SF ACK'),
        'sf_request_to_ack_ms':np.asarray(
            [r['request_to_ack_ms'] for r in sf],dtype=float),
        'sc_request_to_ack_ms':np.asarray(
            [r['request_to_ack_ms'] for r in sc],dtype=float),
        'sf_ble_write_to_ack_ms':np.asarray(
            [r['ble_write_to_ack_ms'] for r in sf
             if r['ble_write_to_ack_ms'] is not None],dtype=float),
    }
    data['mcu_arrival_to_sf_request_ms']=np.asarray([
        (r['request_host_ns']-int(r['source_mcu_monotonic_ns']))/1e6
        for r in sf if r['source_mcu_monotonic_ns'] != ''
    ],dtype=float)
    src_path=ctx['mcu'].get('path')
    if src_path is not None:
        with open(src_path,newline='',encoding='utf-8') as f:
            rows=list(csv.DictReader(f))
        if rows and 'corrected_timestamp_ms' in rows[0]:
            if len(rows)!=len(ctx['mcu']['pc_ns']):
                raise ValueError('MCU source timestamp / arrival length mismatch')
            native=np.asarray(
                [int(r['corrected_timestamp_ms']) for r in rows],dtype=np.int64)
            dt=np.diff(native)
            if np.any(dt <= 0):
                raise ValueError('MCU corrected timestamps are non-increasing')
            data['mcu_corrected_dt_ms']=dt.astype(float)
    sf_ack=[e for e in ctx['events'] if e.get('event')=='sf_ack']
    if len(sf_ack)!=len(sf):
        raise ValueError('SF ACK trace and verified request/ACK pairs disagree')
    write_ns=[]
    request_to_write=[]
    for row,e in zip(sf,sf_ack):
        if int(e['pc_monotonic_ns'])!=int(row['ack_host_ns']):
            raise ValueError('SF ACK trace order mismatch')
        nested=(e.get('evidence') or {}).get('ack') or {}
        write_s=nested.get('write_begin_monotonic_s')
        if not isinstance(write_s,(int,float)):
            continue
        write_ns.append(int(round(write_s*1e9)))
        dt_ms=(write_s*1e9-int(row['request_host_ns']))/1e6
        if dt_ms < 0:
            raise ValueError('BLE write begin precedes logged SF request')
        request_to_write.append(dt_ms)
    data['sf_ble_write_dt_ms']=_positive_differences_ns(
        write_ns,'SF BLE write')
    data['sf_request_to_ble_write_ms']=np.asarray(
        request_to_write,dtype=float)
    for key,arr in data.items():
        if not np.all(np.isfinite(arr)) or np.any(arr < 0):
            raise ValueError(f'Invalid {key} timing intervals')
    return data


def timing_distribution_stats(data):
    """Mean/SD/median and untrimmed tails for reproducible comparison."""
    stats={}
    for key,values in data.items():
        x=np.asarray(values,dtype=float)
        stats[key]=dict(
            n=int(x.size),
            mean_ms=float(np.mean(x)) if len(x) else None,
            std_ms=float(np.std(x,ddof=1)) if len(x)>1 else (0. if len(x) else None),
            median_ms=float(np.median(x)) if len(x) else None,
            p05_ms=float(np.percentile(x,5)) if len(x) else None,
            p25_ms=float(np.percentile(x,25)) if len(x) else None,
            p75_ms=float(np.percentile(x,75)) if len(x) else None,
            p95_ms=float(np.percentile(x,95)) if len(x) else None,
            p99_ms=float(np.percentile(x,99)) if len(x) else None,
            max_ms=float(np.max(x)) if len(x) else None)
    return stats


def plot_timing_boxplots(out,data):
    """Plot full-session inter-event cadence and protocol-stage latencies."""
    plt=safe_figure_setup()
    figures=[]
    for selections,filename,title in (
        (CADENCE_LABELS,'message_cadence_boxplots.png',
         'Message arrival / SF dispatch / BLE write / ACK cadence'),
        (LATENCY_LABELS,'command_latency_boxplots.png',
         'Host-observed pipeline stages: NOT electrical application latency'),
    ):
        keys=[key for key,_ in selections if len(data.get(key,()))]
        labels=[f'{label}\n(n={len(data[key])})'
                for key,label in selections if len(data.get(key,()))]
        fig,ax=plt.subplots(
            figsize=(max(11,len(keys)*2.0),6.5),layout='constrained')
        if keys:
            values=[data[key] for key in keys]
            bp=ax.boxplot(values,whis=(5,95),showmeans=True,
                showfliers=False,patch_artist=True,
                meanprops=dict(marker='D',markerfacecolor='black',
                               markeredgecolor='white',markersize=6),
                medianprops=dict(color='black',linewidth=1.8))
            ax.set_xticks(range(1,len(keys)+1))
            ax.set_xticklabels(labels)
            for patch in bp['boxes']:
                patch.set_facecolor('#7FB6D6')
                patch.set_alpha(.8)
            p99=[np.percentile(data[key],99) for key in keys]
            ax.scatter(range(1,len(keys)+1),p99,marker='^',s=42,
                color='#BC6239',zorder=5,label='99th percentile')
            ax.set_yscale('log')
            ax.legend(loc='upper right')
        ax.set(ylabel='Elapsed time (ms, log scale)',title=title)
        ax.grid(axis='y',alpha=.2)
        ax.set_axisbelow(True)
        fig.savefig(out/'figures'/filename,dpi=170)
        plt.close(fig)
        figures.append('figures/'+filename)
    return figures


def timing_stats_markdown(stats):
    lines=[
        '| Distribution | n | Mean ± SD (ms) | Median | P05 | P25 | P75 | P95 | P99 | Max |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    for key,label in (*CADENCE_LABELS,*LATENCY_LABELS):
        entry=stats.get(key)
        if not entry or not entry['n']:
            continue
        lines.append(
            f"| {label} | {entry['n']} | {entry['mean_ms']:.3f} ± {entry['std_ms']:.3f} | "
            f"{entry['median_ms']:.3f} | {entry['p05_ms']:.3f} | "
            f"{entry['p25_ms']:.3f} | {entry['p75_ms']:.3f} | "
            f"{entry['p95_ms']:.3f} | {entry['p99_ms']:.3f} | {entry['max_ms']:.3f} |")
    return lines


def make_figures(out,sf,sc,density):
    plt=safe_figure_setup()
    fig, ax=plt.subplots(figsize=(12,5),layout='constrained')
    sf_lat=[r['request_to_ack_ms'] for r in sf]
    sc_lat=[r['request_to_ack_ms'] for r in sc]
    if sf_lat:ax.hist(sf_lat,bins=32,alpha=.7,label=f'SF request to ACK (n={len(sf_lat)})')
    if sc_lat:ax.hist(sc_lat,bins=20,alpha=.55,label=f'SC request to ACK (n={len(sc_lat)})')
    ax.set(xlabel='Host-monotonic request→ACK (ms)',ylabel='Number of commands',
           title='Protocol completion latency; NOT electrical application latency')
    ax.legend();ax.grid(alpha=.2)
    fig.savefig(out/'figures/latency_distribution.png',dpi=150);plt.close(fig)

    active=[r for r in sf if r['ack_spacing_divided_by_prior_period'] is not None]
    fig, ax=plt.subplots(figsize=(12,5),layout='constrained')
    if active:
        ax.scatter([r['prior_active_cycle_period_ms'] for r in active],
                   [r['preceding_sf_ack_spacing_ms'] for r in active],
                   s=15,alpha=.65)
        maxv=max(max(r['prior_active_cycle_period_ms'],r['preceding_sf_ack_spacing_ms']) for r in active)
        ax.plot([0,maxv],[0,maxv],'--',color='black',lw=.75,
                label='Equal ACK spacing and prior period')
    ax.set(xlabel='Previous ACK-indicated cycle period (ms)',
           ylabel='Elapsed time between SF ACKs (ms)',
           title='Commands vs cycle opportunities (host clock; no electrical attribution)')
    ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.savefig(out/'figures/command_burst_analysis.png',dpi=150);plt.close(fig)

    start=sf[0]['ack_host_ns'] if sf else 0
    fig, axes=plt.subplots(2,1,figsize=(14,7),sharex=True,layout='constrained')
    times=[(r['ack_host_ns']-start)/1e9 for r in sf]
    axes[0].scatter(times,[r['sf_hz'] for r in sf],marker='|',s=55,
                    label='SF ACK Hz')
    if sf:
        axes[1].plot(times,[r['request_to_ack_ms'] for r in sf],'.-',lw=.65,
                     label='Host request→ACK')
    axes[0].set_ylabel('SF ACK (Hz)')
    axes[1].set_ylabel('Request→ACK (ms)')
    axes[1].set_xlabel('Host elapsed time since first SF ACK (s)')
    for ax in axes:ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.suptitle('Command throughput and latency on one monotonic host clock')
    fig.savefig(out/'figures/command_timeline.png',dpi=150);plt.close(fig)


def run(run_dir):
    ctx=read_context(run_dir)
    out=make_output(ctx,'command_timing')
    sf,sc=analyze_host_events(ctx)
    density=physical_command_density(ctx)
    save_csv(out/'sf_command_latency.csv',sf,list(sf[0]) if sf else ['sf_seq'])
    save_csv(out/'sc_command_latency.csv',sc,list(sc[0]) if sc else ['sc_seq'])
    save_csv(out/'command_rate_vs_cycle.csv',density,list(density[0]) if density else ['start_id'])
    make_figures(out,sf,sc,density)
    distributions=compute_timing_distributions(ctx,sf,sc)
    distribution_stats=timing_distribution_stats(distributions)
    boxplot_files=plot_timing_boxplots(out,distributions)
    save_csv(out/'timing_distribution_statistics.csv',
             [dict(distribution=key,**row) for key,row in distribution_stats.items()],
             ['distribution','n','mean_ms','std_ms','median_ms','p05_ms','p25_ms',
              'p75_ms','p95_ms','p99_ms','max_ms'])
    save_csv(out/'timing_distribution_samples.csv',
             [dict(distribution=key,delta_ms=float(v))
              for key,values in distributions.items() for v in values],
             ['distribution','delta_ms'])
    from experiment_analysis.host_timing_views import generate_state_timing_outputs
    state_outputs=generate_state_timing_outputs(ctx,out,sf)
    sf_times=quantiles([r['request_to_ack_ms'] for r in sf])
    sc_times=quantiles([r['request_to_ack_ms'] for r in sc])
    active=[r for r in sf if r['ack_spacing_divided_by_prior_period'] is not None]
    faster=sum(bool(r['ack_faster_than_one_prior_cycle']) for r in active)
    summary=dict(schema='matrix32_host_command_timing_v1',
       sf_request_ack_pairs=len(sf),sc_request_ack_pairs=len(sc),
       sf_request_ack_ms=sf_times,sc_request_ack_ms=sc_times,
       pipeline_timing_stats=distribution_stats,
       host_state_timing=state_outputs,
       sf_ack_with_previous_active_cycle_reference=len(active),
       sf_acks_faster_than_previous_cycle=faster,
       sf400_muted_scheduler_count=sum(r['is_sf400_muted_scheduler'] for r in sf),
       mcu_source_matched_count=sum(r['rounded_source_hz_equals_sf'] is True for r in sf),
       regular_physical_intervals=len([r for r in density if r['regular_period']]),
       physical_intervals_with_multiple_sf_acks_provisional=sum(
           r['regular_period'] and r['more_than_one_sf_ack_during_period_provisional']
           for r in density),
       figures=['figures/latency_distribution.png','figures/command_burst_analysis.png',
                'figures/command_timeline.png'] + boxplot_files + state_outputs['figures'],
       host_request_to_ack_is_physical_latency=False,
       provenance=ctx['provenance'])
    write_json(out/'summary.json',summary)
    lines=['# Matrix32 host command timing and burst pressure','',
      '**All latency durations use the PC monotonic clock for both endpoints.** '
      'They describe software request-to-ACK completion, not device electrical execution.','',
      '| Host timing evidence | Result |','| --- | ---: |',
      f"| SF requested/ACKed pairs | {len(sf)} |",
      f"| SC requested/ACKed pairs | {len(sc)} |",
      f"| SF median request→ACK | {sf_times['median']:.3f} ms |" if sf else '| SF median | n/a |',
      f"| SF p90 / p95 request→ACK | {sf_times['p90']:.3f} / {sf_times['p95']:.3f} ms |" if sf else '| SF p90/p95 | n/a |',
      f"| SC median request→ACK | {sc_times['median']:.3f} ms |" if sc else '| SC median | n/a |',
      f"| ACK spacing shorter than previous active commanded cycle | {faster} / {len(active)} |",
      f"| SF400 ACKs during ACK-indicated mute | {summary['sf400_muted_scheduler_count']} |",
      f"| Complete approved cycles with ≥2 SF ACKs (provisional) | "
      f"{summary['physical_intervals_with_multiple_sf_acks_provisional']} |",'',
      '## Message cadence and pipeline jitter', '',
      'All distributions cover the **complete recorded MCU/controller session**, '
      'not just the ~20 s scope capture. The long SF inter-command tails can '
      'include idle/muted periods; they are not automatically network jitter. '
      'The host MCU-arrival intervals include USB/OS scheduling. '
      'MCU source timestamps have 1-ms quantization. '
      'BLE write begins and ACK timestamps use the recorded host monotonic clock.', '',
      *timing_stats_markdown(distribution_stats), '',
      '![Incoming MCU vs SF request / BLE-write / ACK cadence](figures/message_cadence_boxplots.png)', '',
      '![Request, write and ACK latency stages](figures/command_latency_boxplots.png)', '',
      '**Plot convention:** central line = median, diamond = mean, box = '
      '25th–75th percentile, whiskers = 5th–95th percentile, triangle = '
      '99th percentile. Maxima and standard deviations remain in the table. '
      'Logarithmic time axis is used solely for readability.', '',
      '[Distribution statistics](timing_distribution_statistics.csv) · '
      '[Untrimmed underlying timing samples](timing_distribution_samples.csv).', '',
      '![Host protocol request-to-ACK latency](figures/latency_distribution.png)','',
      '![Inter-ACK spacing relative to preceding period](figures/command_burst_analysis.png)','',
      '![Full SF ACK timing](figures/command_timeline.png)','',
      '## Per-second throughput and ACK-indicated active/muted timing','',
      'Host-monotonic counts below cover the **complete controller session**. '
      'Bars show the fractional time in each one-second bin after an acknowledged '
      'SC0 (muted), a nonzero SC command (active), or before the first known '
      'SC ACK (unknown). The shaded state is the last *acknowledged command*, '
      '**not a measurement of electrical output current**.','',
      '![Full-session MCU, SF and SC host traffic](figures/throughput_per_second.png)','',
      'Each duration in the state comparison is included only when **both endpoints** '
      'lie within a single acknowledged SC state and there is **no SC ACK '
      'between them**. Durations that cross an SC ACK and intervals before '
      'the first known SC ACK are omitted from the active/muted plots but '
      'retained with their state labels in the exported sample CSV. '
      'Long genuine command gaps are not automatically trimmed.','',
      '![Active versus muted timing by ACK-indicated SC state](figures/active_muted_timing_boxplots.png)','',
      '[Per-second throughput data](throughput_per_second.csv) · '
      '[State-separated statistics](state_timing_statistics.csv) · '
      '[All state-labeled timing spans](state_timing_samples.csv).','',
      '## Interpretation','',
      '- Request/ACK pairing is verified by command type, order, monotonic time, and value.',
      '- BLE write-to-ACK timings are stored separately where the trace provides '
      'transport-phase timestamps.',
      '- SF400 during SC0 is a controller scheduling policy, not proof of 400 Hz output.',
      '- Comparing ACK spacing to a previously requested period is a host-side '
      'opportunity metric, not physical-cycle execution evidence.',
      '- Per-cycle command density uses an unqualified cross-clock mapping and '
      'must be interpreted as provisional.',
      '', '[SF event table](sf_command_latency.csv) · '
      '[SC event table](sc_command_latency.csv) · '
      '[per-approved-cycle command pressure](command_rate_vs_cycle.csv) · '
      '[provenance](summary.json).']
    from experiment_analysis.report_figure_captions import add_figure_captions
    (out/'report.md').write_text(add_figure_captions('\n'.join(lines)+'\n'),encoding='utf-8')
    print(f"[TIMING] {len(sf)} SF and {len(sc)} SC ACK pairs; SF median {sf_times['median']:.3f} ms; {faster}/{len(active)} faster than preceding cycle")
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path)
    args=p.parse_args(argv)
    return run(args.run_dir)

if __name__=='__main__':
    main()
