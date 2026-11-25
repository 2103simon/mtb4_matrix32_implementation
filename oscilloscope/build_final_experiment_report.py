"""Assemble a source-consistent illustrated index of Matrix32 analyses.

No results are recomputed here. The three independent summaries must share
source hashes. Existing pulse approval and per-ACK evidence must match too.
"""
from __future__ import annotations
import argparse
from pathlib import Path

from oscilloscope.extended_analysis_common import read_context, load_json, write_json

REQUIRED = (
    ('frequency_transitions','Frequency transitions'),
    ('stimulation_shutdown','Shutdown and restart'),
    ('command_timing','Command timing'),
)


def build_command_delivery_scorecard(events, timing, prior, robust):
    """Separate observed host ACKs from conditional electrical evidence.

    This function intentionally offers NO per-SF 'executed/not executed'
    success rate: the original cross-clock mapping and Matrix32 internal
    command-application time have not been qualified.
    """
    host = {}
    for prefix in ('sf', 'sc'):
        requests = [e for e in events if e.get('event') == prefix + '_requested']
        acks = [e for e in events if e.get('event') == prefix + '_ack']
        paired = timing[f'{prefix}_request_ack_pairs']
        if len(requests) != paired or len(acks) != paired:
            raise ValueError(f'{prefix.upper()} command counts conflict with paired host-timing audit')
        ok = sum(e.get('status') == 'OK' for e in acks)
        # Reports use successful ACK messages, not BLE transmission attempts.
        host[prefix] = {
            'requested': len(requests),
            'acks_received': len(acks),
            'acks_ok': ok,
            'acks_non_ok': len(acks) - ok,
            'requests_without_ack': len(requests) - len(acks),
        }
    changes = [(e.get('evidence') or {}).get('same_value')
               for e in events if e.get('event') == 'sf_ack']
    host['sf']['confirmed_value_changes'] = (sum(value is False for value in changes)
        if all(value is not None for value in changes) else None)
    host['sf']['muted_scheduler_sf400'] = timing['sf400_muted_scheduler_count']
    host['sf']['source_rate_matched'] = timing['mcu_source_matched_count']

    physical = None
    if prior is not None:
        categories = prior['ack_classifications_in_scope']
        inside = int(prior['sf_ack_success_in_provisional_scope_window'])
        outside = int(prior['sf_ack_success_outside_scope_window'])
        if sum(categories.values()) != inside:
            raise ValueError('Per-ACK classifications do not sum to in-scope SF ACKs')
        if inside + outside != host['sf']['acks_ok']:
            raise ValueError('Per-ACK scope classification does not reconcile with successful SF ACKs')
        if int(prior['sf_ack_success_all_session']) != host['sf']['acks_ok']:
            raise ValueError('Per-ACK audit and command trace disagree on total SF ACKs')
        matched = int(categories.get('STABLE_MATCHING_PERIOD_OBSERVED', 0))
        physical = {
            'in_scope_provisional': inside,
            'outside_scope_provisional': outside,
            'matching_settled_period_evidence': matched,
            'in_scope_no_individually_attributable_evidence': inside - matched,
            'potential_supersession_before_pulse': int(categories.get('NEXT_SF_ACK_BEFORE_NEXT_PULSE', 0)),
            'muted_at_ack': int(categories.get('ACK_WHILE_CURRENT_MUTED', 0)),
            'muted_before_next_pulse': int(categories.get('MUTE_ACK_BEFORE_NEXT_PULSE', 0)),
            'prior_cycle_not_settled': int(categories.get('INSUFFICIENT_PRIOR_CYCLE_SETTLING', 0)),
            'other_insufficient_isolated_evidence': int(categories.get('INSUFFICIENT_ISOLATED_PERIOD_EVIDENCE', 0)),
            'additional_categories': {k:v for k,v in categories.items() if k not in (
                'STABLE_MATCHING_PERIOD_OBSERVED', 'NEXT_SF_ACK_BEFORE_NEXT_PULSE',
                'ACK_WHILE_CURRENT_MUTED','MUTE_ACK_BEFORE_NEXT_PULSE',
                'INSUFFICIENT_PRIOR_CYCLE_SETTLING',
                'INSUFFICIENT_ISOLATED_PERIOD_EVIDENCE')},
            'proved_executed_commands': None,
            'proved_unexecuted_commands': None,
            'execution_rate_measurable': False,
            'clock_qualified': False,
        }
    settled = None
    if robust is not None:
        # Physical intervals and SF ACKs have different denominators.
        records = robust.get('record_summaries', [])
        eligible = sum(int(x['baseline_ack_stable']) for x in records)
        disagree = sum(int(x['baseline_ack_outside_tolerance']) for x in records)
        settled = {
            'eligible_intervals': eligible,
            'diagnostically_compatible_intervals': eligible - disagree,
            'diagnostic_outliers': disagree,
            'per_command_execution_evidence': False,
        }
    return {'schema': 'matrix32_command_delivery_scorecard_v1',
            'host_commands': host, 'physical_ack_observability': physical,
            'settled_electrical_intervals': settled,
            'physical_application_time_qualified': False,
            'interpretation': 'An OK ACK establishes protocol acknowledgment. Matching settled '
               'electrical periods are compatible with the acknowledged SF, not unique proof '
               'of execution. Unobserved intermediate SFs are not necessarily failures.'}


def plot_command_delivery_scorecard(path, card):
    """One figure, with separate denominators for protocol and scope evidence."""
    from oscilloscope.extended_analysis_common import safe_figure_setup
    plt = safe_figure_setup()
    fig, axes = plt.subplots(1, 2, figsize=(15, 6.2), layout='constrained')
    sf = card['host_commands']['sf']
    sc = card['host_commands']['sc']
    labels = ['SF requests', 'SF OK ACKs', 'SC requests', 'SC OK ACKs']
    values = [sf['requested'], sf['acks_ok'], sc['requested'], sc['acks_ok']]
    axes[0].barh(labels[::-1], values[::-1])
    for i, value in enumerate(values[::-1]):
        axes[0].text(value + 2, i, str(value), va='center')
    axes[0].set(xlim=(0, max(values + [1]) * 1.18), xlabel='Host-side count',
                title='Commands and successful acknowledgments')
    axes[0].grid(axis='x', alpha=.2)
    axes[0].set_axisbelow(True)
    detail = card['physical_ack_observability']
    if detail:
        cats = [
            ('Matching settled period', detail['matching_settled_period_evidence']),
            ('Possible supersession', detail['potential_supersession_before_pulse']),
            ('Current muted at ACK', detail['muted_at_ack']),
            ('Muted before next pulse', detail['muted_before_next_pulse']),
            ('Previous cycle not settled', detail['prior_cycle_not_settled']),
            ('Other insufficient evidence', detail['other_insufficient_isolated_evidence']),
        ]
        cats += [(k, v) for k, v in detail['additional_categories'].items()]
        cats += [('Outside scope', detail['outside_scope_provisional'])]
        axes[1].barh([name for name, _ in cats[::-1]], [n for _, n in cats[::-1]])
        for i, (_, count) in enumerate(cats[::-1]):
            axes[1].text(count + 1, i, str(count), va='center')
        axes[1].set(xlim=(0, max([n for _, n in cats] + [1]) * 1.27),
                    xlabel='Successful SF ACKs',
                    title='Electrical observability, NOT execution verdict')
        axes[1].grid(axis='x', alpha=.2)
        axes[1].set_axisbelow(True)
    else:
        axes[1].text(.5,.5,'Per-ACK scope audit unavailable',ha='center',va='center',
                     transform=axes[1].transAxes)
        axes[1].set_axis_off()
    fig.suptitle('One recording: reliable protocol delivery; physical attribution is limited',
                 fontsize=13)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def build(run_dir):
    ctx=read_context(run_dir)
    analysis=ctx['root']/'analysis'
    # Snapshot the existing long-form report exactly once before this builder
    # replaces its old path with the new authoritative scientific report.
    # This preserves the user's original technical index byte-for-byte.
    old_report=analysis/'final_experiment_report.md'
    original_legacy=analysis/'final_experiment_report_legacy.md'
    if old_report.is_file() and not original_legacy.exists():
        prior_text=old_report.read_text(encoding='utf-8')
        if ('## Requested ' in prior_text or
                '## Main measurements' in prior_text):
            original_legacy.write_bytes(old_report.read_bytes())
    summaries={}
    for name,title in REQUIRED:
        folder=analysis/name
        for relative in ('summary.json','report.md'):
            if not (folder/relative).is_file():
                raise FileNotFoundError(f'Missing {folder/relative}; run its independent module first')
        summary=load_json(folder/'summary.json')
        for key,value in ctx['provenance'].items():
            if summary.get('provenance',{}).get(key)!=value:
                raise ValueError(f'Source/version mismatch in {name}: {key}')
        for img in summary.get('figures',[]):
            if not (folder/img).is_file():
                raise FileNotFoundError(f'Report figure missing: {folder/img}')
        summaries[name]=summary
    prior_path=analysis/'ack_physical_observability'/'summary.json'
    prior=load_json(prior_path) if prior_path.exists() else None
    if prior:
        for rel in ('final_report.md','command_physical_timeline.png'):
            if not (analysis/'ack_physical_observability'/rel).is_file():
                raise FileNotFoundError(f'Per-ACK report link missing: {rel}')
        old=prior.get('source_hashes',{})
        mapping={'approved_onsets_sha256':'approved_onsets_sha256',
                 'source_ch1_sha256':'raw_ch1_sha256',
                 'commands_sha256':'matrix32_commands_sha256',
                 'mcu_rates_sha256':'mcu_rates_sha256',
                 'epoch_clock_analysis_sha256':'clock_epoch_sha256'}
        for src,ref in mapping.items():
            if old.get(src)!=ctx['provenance'][ref]:
                raise ValueError(f'Prior per-ACK audit uses a different source: {src}')
    robust_path=analysis/'frequency_robustness_summary.json'
    robust=load_json(robust_path) if robust_path.exists() else None
    if robust and not (analysis/'frequency_robustness_report.md').is_file():
        raise FileNotFoundError('Frequency robustness summary exists without report')
    if robust and robust.get('total_approved_intervals')!=len(ctx['onsets'])-1:
        raise ValueError('Prior frequency audit does not match approved interval count')
    transitions=summaries['frequency_transitions']
    shutdown=summaries['stimulation_shutdown']
    timing=summaries['command_timing']
    if transitions['approved_intervals'] != len(ctx['onsets'])-1:
        raise ValueError('Transition interval count mismatch')
    if shutdown['operator_excluded_negative_only'] != ctx['negative_only']:
        raise ValueError('Shutdown signed-rejection count mismatch')
    if prior and prior['approved_intervals']!=len(ctx['onsets'])-1:
        raise ValueError('Old per-ACK audit interval count mismatch')
    existing_ack=prior.get('ack_classifications_in_scope',{}) if prior else {}
    stable_matches=existing_ack.get('STABLE_MATCHING_PERIOD_OBSERVED')
    matched=transitions['mismatches_with_compatible_in_interval_sf_ack']
    delivery=build_command_delivery_scorecard(ctx['events'], timing, prior, robust)
    write_json(analysis/'command_delivery_scorecard.json', {
        **delivery, 'provenance': ctx['provenance']})
    plot_command_delivery_scorecard(analysis/'command_delivery_scorecard.png', delivery)
    sf=delivery['host_commands']['sf']
    sc=delivery['host_commands']['sc']
    physical=delivery['physical_ack_observability']
    settled=delivery['settled_electrical_intervals']
    lines=['# Matrix32 — consolidated experimental analysis','',
      '**Source:** completed integrated recording with source-locked, operator-signed '
      'biphasic CH1 onset inventory. This document compiles independent offline '
      'reports; no new pulse detection or clock calibration was performed.','',
      '## Requested → ACKed → physically observed (executive scorecard)', '',
      '| Question | Result |','| --- | ---: |',
      f"| SF frequency commands requested | {sf['requested']} |",
      f"| Of those, actual SF value changes verified in ACK trace | {sf['confirmed_value_changes'] if sf['confirmed_value_changes'] is not None else 'not available'} |",
      f"| SF changes acknowledged OK | **{sf['acks_ok']} / {sf['requested']}** |",
      f"| SF requests without ACK / non-OK ACK | {sf['requests_without_ack']} / {sf['acks_non_ok']} |",
      f"| SC current changes acknowledged OK | **{sc['acks_ok']} / {sc['requested']}** |",
      f"| Ordinary source-matched tactile-frequency SF changes | {sf['source_rate_matched']} |",
      f"| Muted-scheduler SF400 changes (not stimulation evidence) | {sf['muted_scheduler_sf400']} |",
      *( [f"| SF ACKs inside estimated scope window | {physical['in_scope_provisional']} |",
          f"| SF ACKs with matching settled electrical interval | **{physical['matching_settled_period_evidence']}** |",
          f"| In-scope ACKs with no individually attributable settled interval | {physical['in_scope_no_individually_attributable_evidence']} |",
          f"| Of those: potentially superseded before next pulse | {physical['potential_supersession_before_pulse']} |",
          f"| Of those: current muted at ACK | {physical['muted_at_ack']} |",
          f"| Of those: muted before next pulse | {physical['muted_before_next_pulse']} |",
          f"| Of those: previous cycle not settled | {physical['prior_cycle_not_settled']} |",
          f"| Of those: other insufficient isolated evidence | {physical['other_insufficient_isolated_evidence']} |",
          f"| SF ACKs outside estimated scope window | {physical['outside_scope_provisional']} |"]
         if physical else ['| Individual SF ACKs with physical evidence | not available: run per-ACK audit |']),
      *( [f"| Eligible settled electrical intervals matching ACK frequency | **{settled['diagnostically_compatible_intervals']} / {settled['eligible_intervals']}** |"]
         if settled else ['| Steady-state electrical interval agreement | not available |']),
      '| Number of frequency changes proven physically executed | **Not determinable** |',
      '| Number of frequency changes proven not executed | **Not determinable** |',
      '',
      '**Reading this table:** 100% SF ACK success is demonstrated *at the protocol level*. '
      'The matching electrical-period count is evidence of compatibility for '
      'individual ACKs under declared timing assumptions, **not** an executed-command '
      'percentage. The remaining ACKs must **not** be labeled failed, skipped, or '
      'successfully executed without additional physical evidence. '
      'The scope–host clock offset is unqualified.', '',
      '![Host acknowledgments and physical observability](command_delivery_scorecard.png)', '',
      '[Machine-readable scorecard](command_delivery_scorecard.json) · '
      '[Per-SF ACK classification](ack_physical_observability/sf_ack_observability.csv) · '
      '[Per-command host timing](command_timing/sf_command_latency.csv).', '',
      '## Main measurements','',
      '| Measurement | Result |','| --- | ---: |',
      f"| Operator-approved biphasic CH1 onsets | {len(ctx['onsets'])} |",
      f"| Consecutive approved CH1 intervals | {len(ctx['onsets'])-1} |",
      f"| Negative-only events explicitly excluded by operator | {ctx['negative_only']} |",
      f"| Regular prior-ACK mismatches including muted/SC context | {transitions['prior_ack_mismatches_regular']} |",
      f"| Active transitions with compatible in-interval ACK | {transitions['active_transition_in_interval_ack_match_count']} |",
      f"| All mismatches with in-interval match including SC context | {matched} |",
      f"| Excluded negative-only events following latest SC0 (nominal map) | {shutdown['negative_only_after_latest_sc0_ack_provisional']} |",
      f"| Successful SF request/ACK pairs in full session | {timing['sf_request_ack_pairs']} |",
      f"| Successful SC request/ACK pairs in full session | {timing['sc_request_ack_pairs']} |",
      f"| SF ACKs with matching *settled* physical interval in prior audit | {stable_matches if stable_matches is not None else 'not available'} |",
      '',
      '## 1. Approved steady-state frequency and per-ACK observability','',
      'Steady-state frequency compatibility and per-ACK observability describe '
      '**different subsets** and must not be combined into an execution rate.']
    if robust:
        rec=robust['record_summaries'][0]
        lines += [f"The previous robustness audit evaluated **{rec['baseline_ack_stable']}** "
                  'baseline eligible ACK intervals (see its full tolerance criteria).', '']
    if prior:
        lines += [f"The previous per-ACK audit classified **{prior['sf_ack_success_in_provisional_scope_window']}** "
                  'successful SF ACKs inside the estimated scope window. '
                  'A matching physical interval is compatibility evidence, not unique causation.', '',
                  '[Detailed per-ACK report](ack_physical_observability/final_report.md) · '
                  '[Steady-state robustness](frequency_robustness_report.md).','',
                  '![Original per-ACK overview](ack_physical_observability/command_physical_timeline.png)','']
    else:
        lines += ['A prior per-ACK summary was not present; run the optional '
                  '`oscilloscope.audit_ack_physical_observability` audit for it.','']
    lines.extend([
      '## 2. Rapid frequency transitions','',
      f"**{transitions['prior_ack_mismatches_regular']}** regular intervals are "
      'not compatible with the last preceding ACK under the nominal map. '
      f"**{transitions['active_transition_in_interval_ack_match_count']}** are active "
      'intervals with a compatible in-interval SF ACK and no SC transition. '
      'The remaining prior-ACK mismatches involve muted/SC-transition context. '
      'Neither count demonstrates device failure or proves cycle-boundary application.','',
      '![Physical intervals and ACK positions](frequency_transitions/figures/transition_overview.png)','',
      '![Offset-sensitivity scenarios](frequency_transitions/figures/transition_timing_sensitivity.png)','',
      '[Full transition report](frequency_transitions/report.md) · '
      '[Per-interval transition evidence](frequency_transitions/transitions.csv).','',
      '## 3. Current shutdown and restart','',
      f"The signed review excluded **{ctx['negative_only']}** negative-only "
      'events. The time association with SC0 remains provisional; a negative '
      'voltage excursion does not demonstrate current delivery, and SC0 ACK '
      'does not identify the electrical shutdown instant.','',
      '![SC0/SC50 and observed CH1 events](stimulation_shutdown/figures/shutdown_overview.png)','',
      '![Original incomplete event waveforms](stimulation_shutdown/figures/incomplete_pulse_morphology.png)','',
      '[Full shutdown report](stimulation_shutdown/report.md) · '
      '[Incomplete event evidence](stimulation_shutdown/incomplete_events.csv).','',
      '## 4. Host command timing and cycle opportunities','',
      f"Host-observed SF request-to-ACK latency median: "
      f"**{timing['sf_request_ack_ms']['median']:.3f} ms**. This is protocol "
      'completion time, NOT physical application latency.','',
      *( [
         f"MCU message arrival median: "
         f"**{timing['pipeline_timing_stats']['mcu_arrival_dt_ms']['median_ms']:.3f} ms**, "
         f"SD **{timing['pipeline_timing_stats']['mcu_arrival_dt_ms']['std_ms']:.3f} ms**. "
         f"SF request median spacing: "
         f"**{timing['pipeline_timing_stats']['sf_request_dt_ms']['median_ms']:.3f} ms**. "
         "These describe the full logged session; long SF gaps can be "
         "deliberate idle/muted intervals, not radio jitter.", '',
         '![MCU ingress and SF request, BLE write and ACK cadence](command_timing/figures/message_cadence_boxplots.png)','',
         '![MCU-to-SF scheduling and BLE protocol latency](command_timing/figures/command_latency_boxplots.png)','',
         '[Full timing statistics and tails](command_timing/timing_distribution_statistics.csv) · '
         '[Original timing samples](command_timing/timing_distribution_samples.csv).', ''
        ] if timing.get('pipeline_timing_stats') else []),
      '![Host protocol latency](command_timing/figures/latency_distribution.png)','',
      '![ACK spacing vs requested cycle time](command_timing/figures/command_burst_analysis.png)','',
      *( ['![Per-second throughput and acknowledged current-state fractions](command_timing/figures/throughput_per_second.png)','',
          '![Timing distributions for ACK-indicated active and muted states](command_timing/figures/active_muted_timing_boxplots.png)','',
          '[Per-second counts](command_timing/throughput_per_second.csv) · '
          '[Per-state timing statistics](command_timing/state_timing_statistics.csv).','']
         if timing.get('host_state_timing') else []),
      '[Full host timing report](command_timing/report.md) · '
      '[Per-SF protocol timing](command_timing/sf_command_latency.csv).','',
      '## Confidence and remaining limitations','',
      '1. **Measured directly:** scope-local intervals between approved biphasic pulses; '
      'original negative-only CH1 waveforms; host-monotonic request/ACK durations.',
      '2. **Conditional on unqualified cross-clock mapping:** assigning SF/SC ACKs '
      'to individual electrical cycles; apparent latency from SC0 to negative-only events.',
      '3. **Hypotheses, not proven:** cycle-boundary frequency updates, internally '
      'superseded SFs, and the mechanism responsible for incomplete voltage dips.',
      '4. **Not established:** electrical current or charge balance, exact turn-off time, '
      'physical command-application latency, or per-command execution percentage.',
      '', 'This is a resistive dummy-load bench experiment, not a safety validation.', '',
      '## Reproducibility','',
      'All analyses read immutable signed evidence and write only under the '
      '`analysis/` tree. Each module records source SHA-256 fingerprints, '
      'software version and assumptions in its `summary.json`.','',
      '[Full machine-readable provenance](extended_analysis_manifest.json).'])
    report=analysis/'final_experiment_report.md'
    from oscilloscope.report_figure_captions import add_figure_captions
    report.write_text(add_figure_captions('\n'.join(lines)+'\n'),encoding='utf-8')
    manifest=dict(schema='matrix32_extended_report_manifest_v1',
         provenance=ctx['provenance'],approved_onsets=len(ctx['onsets']),
         approved_intervals=len(ctx['onsets'])-1,
         module_summaries={name:f'{name}/summary.json' for name,_ in REQUIRED},
         preexisting_ack_report_included=prior is not None,
         preexisting_frequency_robustness_included=robust is not None,
         delivery_scorecard='command_delivery_scorecard.json',
         delivery_scorecard_figure='command_delivery_scorecard.png',
         consolidated_report='final_experiment_report.md')
    write_json(analysis/'extended_analysis_manifest.json',manifest)
    # The validated former consolidation remains available verbatim as legacy.
    # The scientific main report adds only presentation figures and narrative;
    # all source-locked audit classifications and provenance stay unchanged.
    if prior is not None and robust is not None:
        from oscilloscope.present_scientific_report import present
        presentation = present(analysis)
        manifest.update(presentation)
    else:
        # Preserve the preexisting partial-audit workflow. A scientific
        # source-verified presentation requires both independent audits.
        from shutil import copyfile
        if not (analysis/'final_experiment_report_legacy.md').is_file():
            copyfile(report, analysis/'final_experiment_report_legacy.md')
        manifest['legacy_report'] = 'final_experiment_report_legacy.md'
        manifest['presentation_status'] = 'SKIPPED_MISSING_OPTIONAL_PRIOR_AUDITS'
        print('[EXTENDED REPORT] Per-ACK or robustness audit absent; '
              'retaining technical consolidation rather than fabricating the '
              'six-figure scientific report')
    write_json(analysis/'extended_analysis_manifest.json', manifest)
    print(f'[EXTENDED REPORT] {report}')
    return manifest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path)
    args=p.parse_args(argv)
    return build(args.run_dir)

if __name__=='__main__':
    main()
