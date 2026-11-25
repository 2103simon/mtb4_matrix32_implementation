"""Host-only throughput and ACK-indicated current-state timing views.

The current state means the most recently acknowledged SC command, NOT
measured electrical output current. This module never uses scope timestamps.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter

import numpy as np

from experiment_analysis.extended_analysis_common import safe_figure_setup, save_csv

SECOND_NS = 1_000_000_000
STATES = ('ACTIVE', 'MUTED')
DISTRIBUTIONS = (
    ('mcu_arrival_dt_ms', 'MCU arrival spacing'),
    ('sf_request_dt_ms', 'SF request spacing'),
    ('sf_ack_dt_ms', 'SF ACK spacing'),
    ('sf_request_to_ack_ms', 'SF request to ACK'),
    ('sf_write_to_ack_ms', 'SF BLE write to ACK'),
)


def sc_history(events):
    rows = sorted((int(e['pc_monotonic_ns']),
                   'MUTED' if int(e['current_command']) == 0 else 'ACTIVE')
                  for e in events if e.get('event') == 'sc_ack'
                  and e.get('status') == 'OK')
    if len(set(t for t, _ in rows)) != len(rows):
        raise ValueError('Ambiguous simultaneous SC ACKs')
    return rows


def state_at(ns, history):
    index = bisect_right([t for t, _ in history], int(ns))-1
    return history[index][1] if index >= 0 else 'UNKNOWN'


def state_for_span(a, b, history):
    a, b = int(a), int(b)
    if b < a:
        raise ValueError('Nonmonotonic host timestamps')
    state = state_at(a, history)
    if state == 'UNKNOWN':
        return 'UNKNOWN'
    if any(a < t <= b for t, _ in history):
        return 'CROSSES_SC_ACK'
    return state


def _transport_ns(event, key):
    nested = (event.get('evidence') or {}).get('ack') or {}
    seconds = nested.get(key)
    if not isinstance(seconds, (float, int)) or not np.isfinite(seconds):
        return None
    return int(round(seconds * SECOND_NS))


def host_series(ctx):
    events = ctx['events']
    return {
        'mcu_messages': [int(v) for v in ctx['mcu']['pc_ns']],
        'sf_requests': [int(e['pc_monotonic_ns']) for e in events
                        if e.get('event') == 'sf_requested'],
        'sf_ble_writes': [t for e in events if e.get('event') == 'sf_ack'
                          for t in [_transport_ns(e, 'write_begin_monotonic_s')]
                          if t is not None],
        'sf_acks': [int(e['pc_monotonic_ns']) for e in events
                    if e.get('event') == 'sf_ack'],
        'sc_acks': [int(e['pc_monotonic_ns']) for e in events
                    if e.get('event') == 'sc_ack'],
    }


def _bin_fractions(start, end, history):
    cuts = [start] + [t for t, _ in history if start < t < end] + [end]
    duration = Counter()
    for a, b in zip(cuts, cuts[1:]):
        duration[state_at(a, history)] += b-a
    return {key.lower()+'_fraction': duration[key]/(end-start)
            for key in ('ACTIVE', 'MUTED', 'UNKNOWN')}


def throughput_bins(ctx, *, bin_s=1):
    if bin_s <= 0:
        raise ValueError('Throughput bin length must be positive')
    width = int(round(bin_s * SECOND_NS))
    series = host_series(ctx)
    timestamps = [t for seq in series.values() for t in seq]
    if not timestamps:
        return []
    start = min(timestamps)
    n = 1 + (max(timestamps)-start)//width
    counts = {key: np.zeros(n, dtype=int) for key in series}
    for key, seq in series.items():
        for t in seq:
            counts[key][(t-start)//width] += 1
    history = sc_history(ctx['events'])
    rows = []
    for i in range(n):
        a, b = start+i*width, start+(i+1)*width
        rows.append(dict(bin_index=i, elapsed_start_s=round(i*bin_s, 6),
             start_host_ns=a, end_host_ns=b,
             **{key:int(vals[i]) for key,vals in counts.items()},
             **_bin_fractions(a,b,history)))
    for key, seq in series.items():
        if sum(row[key] for row in rows) != len(seq):
            raise AssertionError(f'Throughput event counts disagree for {key}')
    return rows


def state_timing_samples(ctx, sf_rows):
    history = sc_history(ctx['events'])
    rows = []

    def append(name, a, b):
        a, b = int(a), int(b)
        if b < a:
            raise ValueError(f'Negative host duration for {name}')
        rows.append(dict(distribution=name, state=state_for_span(a,b,history),
             duration_ms=(b-a)/1e6,start_host_ns=a,end_host_ns=b))

    mcu = [int(t) for t in ctx['mcu']['pc_ns']]
    for a,b in zip(mcu,mcu[1:]):
        append('mcu_arrival_dt_ms',a,b)
    for field,name in (('request_host_ns','sf_request_dt_ms'),
                       ('ack_host_ns','sf_ack_dt_ms')):
        times = [int(row[field]) for row in sf_rows]
        for a,b in zip(times,times[1:]):
            append(name,a,b)
    acks = [e for e in ctx['events'] if e.get('event')=='sf_ack']
    if len(acks)!=len(sf_rows):
        raise ValueError('SF host evidence count mismatch')
    for record,event in zip(sf_rows,acks):
        a,b=record['request_host_ns'],record['ack_host_ns']
        if int(event['pc_monotonic_ns'])!=int(b):
            raise ValueError('SF ACK pairing order mismatch')
        append('sf_request_to_ack_ms',a,b)
        start = _transport_ns(event,'write_begin_monotonic_s')
        finish = _transport_ns(event,'ack_monotonic_s')
        if start is not None and finish is not None:
            append('sf_write_to_ack_ms',start,finish)
    return rows


def state_timing_stats(samples):
    from experiment_analysis.analyze_command_timing import timing_distribution_stats
    result=[]
    for key,_ in DISTRIBUTIONS:
        for state in STATES:
            values=[row['duration_ms'] for row in samples
                    if row['distribution']==key and row['state']==state]
            stats=timing_distribution_stats({'values':values})['values']
            result.append(dict(distribution=key,state=state,**stats))
    return result


def plot_throughput(out, rows):
    plt=safe_figure_setup()
    fig,axes=plt.subplots(3,1,figsize=(15,9),sharex=True,layout='constrained',
                         gridspec_kw={'height_ratios':[1.1,1.35,.65]})
    x=np.asarray([r['elapsed_start_s']+.5 for r in rows])
    axes[0].step(x,[r['mcu_messages'] for r in rows],where='mid',label='MCU messages')
    for key,label in (('sf_requests','SF requests'),('sf_ble_writes','SF BLE writes'),
                      ('sf_acks','SF ACKs')):
        axes[1].step(x,[r[key] for r in rows],where='mid',label=label)
    states=('active','muted','unknown')
    colors=('#5c91bc','#d6a04b','#aaa')
    for i,(state,color) in enumerate(zip(states,colors)):
        values=[r[state+'_fraction'] for r in rows]
        bottoms=[sum(r[s+'_fraction'] for s in states[:i]) for r in rows]
        axes[2].bar(x,values,bottom=bottoms,width=.95,color=color,label=state.title())
    axes[0].set_ylabel('MCU messages / s')
    axes[1].set_ylabel('SF events / s')
    axes[2].set_ylabel('SC state\nfraction')
    axes[2].set_ylim(0,1)
    axes[2].set_xlabel('Host elapsed time since first recorded event (s)')
    for ax in axes:
        ax.grid(axis='y',alpha=.2)
        ax.legend(loc='upper right',fontsize=8,ncol=3)
    fig.suptitle('Host throughput and last acknowledged SC state')
    fig.savefig(out/'figures/throughput_per_second.png',dpi=170)
    plt.close(fig)


def plot_state_boxplots(out, samples):
    plt=safe_figure_setup()
    fig,axes=plt.subplots(2,1,figsize=(15,10),layout='constrained')
    for ax, selections, title in (
        (axes[0],DISTRIBUTIONS[:3],'MCU and SF event spacing'),
        (axes[1],DISTRIBUTIONS[3:],'Host completion latency; not physical execution'),
    ):
        for i,(key,label) in enumerate(selections):
            for j,state in enumerate(STATES):
                vals=[r['duration_ms'] for r in samples
                      if r['distribution']==key and r['state']==state]
                if not vals:
                    continue
                x=i*3+j+1
                box=ax.boxplot(vals,positions=[x],widths=.7,whis=(5,95),
                   patch_artist=True,showmeans=True,showfliers=False,
                   meanprops=dict(marker='D',markerfacecolor='black',
                                  markeredgecolor='white',markersize=5))
                box['boxes'][0].set_facecolor('#5c91bc' if state=='ACTIVE' else '#d6a04b')
                box['boxes'][0].set_alpha(.75)
                ax.scatter(x,np.percentile(vals,99),marker='^',s=35,color='black',zorder=5)
                # Group sample counts are reported in state_timing_statistics.csv.
        ax.set_xticks([3*i+1.5 for i in range(len(selections))])
        ax.set_xticklabels([label for _,label in selections])
        ax.set_yscale('log')
        ax.set_ylabel('Elapsed time (ms, log scale)')
        ax.set_title(title)
        ax.grid(axis='y',alpha=.2)
    from matplotlib.patches import Patch
    axes[0].legend(handles=[Patch(facecolor='#5c91bc',label='ACK-indicated active'),
                            Patch(facecolor='#d6a04b',label='ACK-indicated muted')],
                   fontsize=9,loc='upper right')
    fig.suptitle('Timing by last acknowledged SC state; crossing spans excluded')
    fig.savefig(out/'figures/active_muted_timing_boxplots.png',dpi=170)
    plt.close(fig)


def generate_state_timing_outputs(ctx, out, sf_rows):
    bins=throughput_bins(ctx)
    samples=state_timing_samples(ctx,sf_rows)
    stats=state_timing_stats(samples)
    save_csv(out/'throughput_per_second.csv',bins,
             list(bins[0]) if bins else ['bin_index'])
    save_csv(out/'state_timing_samples.csv',samples,
             ['distribution','state','duration_ms','start_host_ns','end_host_ns'])
    save_csv(out/'state_timing_statistics.csv',stats,
             ['distribution','state','n','mean_ms','std_ms','median_ms',
              'p05_ms','p25_ms','p75_ms','p95_ms','p99_ms','max_ms'])
    plot_throughput(out,bins)
    plot_state_boxplots(out,samples)
    return dict(
        bins=len(bins),
        throughput_event_counts={
            key:sum(row[key] for row in bins) for key in host_series(ctx)},
        state_sample_counts={state:sum(row['state']==state for row in samples)
                 for state in ('ACTIVE','MUTED','UNKNOWN','CROSSES_SC_ACK')},
        state_timing_statistics=stats,
        classification='last acknowledged SC; NOT physical on/off',
        duration_crossing_sc_excluded=True,
        figures=['figures/throughput_per_second.png',
                 'figures/active_muted_timing_boxplots.png'],
    )
