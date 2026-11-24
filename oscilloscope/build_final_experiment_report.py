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


def build(run_dir):
    ctx=read_context(run_dir)
    analysis=ctx['root']/'analysis'
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
    lines=['# Matrix32 — consolidated experimental analysis','',
      '**Source:** completed integrated recording with source-locked, operator-signed '
      'biphasic CH1 onset inventory. This document compiles independent offline '
      'reports; no new pulse detection or clock calibration was performed.','',
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
      '![Host protocol latency](command_timing/figures/latency_distribution.png)','',
      '![ACK spacing vs requested cycle time](command_timing/figures/command_burst_analysis.png)','',
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
    report.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    manifest=dict(schema='matrix32_extended_report_manifest_v1',
         provenance=ctx['provenance'],approved_onsets=len(ctx['onsets']),
         approved_intervals=len(ctx['onsets'])-1,
         module_summaries={name:f'{name}/summary.json' for name,_ in REQUIRED},
         preexisting_ack_report_included=prior is not None,
         preexisting_frequency_robustness_included=robust is not None,
         consolidated_report='final_experiment_report.md')
    write_json(analysis/'extended_analysis_manifest.json',manifest)
    print(f'[EXTENDED REPORT] {report}')
    return manifest


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',required=True,type=Path)
    args=p.parse_args(argv)
    return build(args.run_dir)

if __name__=='__main__':
    main()
