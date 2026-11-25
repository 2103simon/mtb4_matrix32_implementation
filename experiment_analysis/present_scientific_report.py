"""Present verified Matrix32 audit summaries as one scientific results report.

This is a presentation-only layer. The existing final-report builder first
validates source hashes, signs no reviews, and produces the technical report.
This module does not detect pulses, alter source evidence, or qualify clocks.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

TECHNICAL_REPORT_FILENAME = 'extended_analysis_technical_report.md'
OLD_ARCHIVE_FILENAME = 'final_experiment_report_legacy.md'


def migrate_archived_technical_report(analysis_dir):
    """Safely rename a prior technical report without changing its bytes.

    If both names exist, remove the obsolete duplicate only when identical.
    Conflicting archives are left untouched and reported as an error.
    """
    analysis = Path(analysis_dir).expanduser().resolve()
    previous = analysis / OLD_ARCHIVE_FILENAME
    technical = analysis / TECHNICAL_REPORT_FILENAME
    if previous.is_file():
        if technical.exists():
            if not technical.is_file() or previous.read_bytes() != technical.read_bytes():
                raise FileExistsError(
                    f'Conflicting archived reports: {previous} and {technical}; '
                    'resolve manually to avoid losing historical evidence'
                )
            previous.unlink()
        else:
            previous.rename(technical)
    return technical



def _read(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Required report evidence missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _image(path, caption, number):
    return f"![Figure {number}]({path})\n\n*Figure {number} - {caption}*\n"


def _frequency_pairs(analysis, expected):
    path = analysis / 'frequency_robustness_all_intervals.csv'
    if not path.exists():
        raise FileNotFoundError('Frequency robustness interval CSV required for frequency accuracy figure: ' + str(path))
    with path.open(newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    eligible = [r for r in rows if r.get('baseline_ack_diagnostic') == 'WITHIN_EXPLORATORY_TOLERANCE']
    if len(eligible) != expected:
        raise ValueError('Eligible frequency CSV rows do not match verified frequency robustness summary')
    commanded = [float(r['ack_indicated_hz_stable']) for r in eligible]
    measured = [float(r['measured_frequency_hz']) for r in eligible]
    errors = [float(r['frequency_error_hz']) for r in eligible]
    for want, got, err in zip(commanded, measured, errors):
        if abs((got - want) - err) > 1e-5:
            raise ValueError('Signed frequency error does not equal measured minus commanded')
    return commanded, measured, errors


def _accuracy_plot(analysis, eligible_count):
    from experiment_analysis.extended_analysis_common import safe_figure_setup
    import numpy as np
    plt = safe_figure_setup()
    c, m, errors = _frequency_pairs(analysis, eligible_count)
    if not errors:
        raise ValueError('No eligible frequency intervals for accuracy figure')
    dest = analysis / 'presentation_figures' / 'settled_frequency_accuracy.png'
    dest.parent.mkdir(parents=True, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.8, 4.6), layout='constrained')
    lo = min(c + m)
    hi = max(c + m)
    ax1.plot([lo, hi], [lo, hi], '--', color='#777777', linewidth=1.1, label='Exact agreement')
    ax1.scatter(c, m, s=22, alpha=.75, color='#2163a4')
    ax1.set(xlabel='ACK-indicated commanded frequency (Hz)',
            ylabel='CH1 measured frequency (Hz)',
            title=f'Settled eligible intervals (n={len(errors)})')
    ax1.legend(loc='upper left', fontsize=8)
    ax1.grid(alpha=.2)
    ax2.scatter(c, errors, s=22, alpha=.7, color='#b15032')
    ax2.axhline(0, color='#444444', linewidth=.9)
    ax2.set(xlabel='ACK-indicated commanded frequency (Hz)',
            ylabel='Measured minus commanded (Hz)',
            title='Signed frequency difference')
    ax2.grid(alpha=.2)
    fig.suptitle('Frequency agreement in a selected settled-state subset')
    fig.savefig(dest, dpi=160)
    plt.close(fig)
    return dest


def _window_plot(analysis, scope_s, host_s):
    from experiment_analysis.extended_analysis_common import safe_figure_setup
    plt = safe_figure_setup()
    dest = analysis / 'presentation_figures' / 'observation_windows.png'
    dest.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11.8, 3.1), layout='constrained')
    ax.barh(['Host MCU / BLE session', 'CH1 + CH2 scope capture'],
            [host_s, scope_s], color=['#3978a7', '#58a08f'], height=.5)
    for i, seconds in enumerate((host_s, scope_s)):
        ax.text(seconds + max(host_s, scope_s)*.012, i, f'{seconds:.1f} s', va='center')
    ax.set(xlabel='Duration (s) from each instrument\'s own clock',
           title='Observation durations, not a common synchronized time axis',
           xlim=(0, max(host_s, scope_s) * 1.14))
    ax.grid(axis='x', alpha=.15)
    ax.set_axisbelow(True)
    fig.savefig(dest, dpi=160)
    plt.close(fig)
    return dest


def _timing_figure(analysis):
    from PIL import Image, ImageOps, ImageDraw, ImageFont
    sources = [
        analysis / 'command_timing' / 'figures' / 'throughput_per_second.png',
        analysis / 'command_timing' / 'figures' / 'message_cadence_boxplots.png',
    ]
    if not all(p.is_file() for p in sources):
        raise FileNotFoundError('Command timing figures required for overview: ' + str(sources))
    width = 1500
    tiles = []
    for path in sources:
        with Image.open(path) as input_image:
            rgb = input_image.convert('RGB')
        new_height = round(rgb.height * width / rgb.width)
        tiles.append(rgb.resize((width, new_height), Image.Resampling.LANCZOS))
    header = 46
    gap = 30
    result = Image.new('RGB', (width, sum(tile.height for tile in tiles) + 2*header + gap), 'white')
    draw = ImageDraw.Draw(result)
    labels = ['A | Host message throughput per second', 'B | Full-session cadence distributions']
    y = 0
    for label, tile in zip(labels, tiles):
        draw.text((16, y+12), label, fill='#17364c')
        y += header
        result.paste(tile, (0, y))
        y += tile.height + gap
    dest = analysis / 'presentation_figures' / 'host_timing_overview.png'
    dest.parent.mkdir(parents=True, exist_ok=True)
    result.save(dest)
    return dest


def _check_figures(analysis, markdown):
    import re
    paths = re.findall(r'!\[[^]]*\]\(([^)]+)\)', markdown)
    if len(paths) != 6 or len(set(paths)) != 6:
        raise ValueError('Consolidated report must contain exactly six distinct evidence figures')
    for name in paths:
        if not (analysis / name).is_file():
            raise FileNotFoundError(f'Consolidated report figure missing: {name}')
    return paths


def present(analysis_dir):
    """Preserve the original technical consolidation, then write the main report."""
    analysis = Path(analysis_dir).expanduser().resolve()
    migrate_archived_technical_report(analysis)
    old_path = analysis / 'final_experiment_report.md'
    if not old_path.is_file():
        raise FileNotFoundError('First generate the validated technical report using build_final_experiment_report')
    current_technical = old_path.read_bytes()
    if (not current_technical.startswith(b'# Matrix32') or
            not any(section in current_technical for section in
                    (b'## Requested ', b'## Main measurements'))):
        raise ValueError('Refusing to replace the technical report with the presentation report')

    root = analysis.parent
    metadata = _read(root/'metadata.json')
    # Read-only controller setup detail, when uniquely present in the run.
    # It is supplemental metadata, not a new electrical classification.
    settings_files = sorted((root/'mcu_matrix32').glob('*_settings.json'))
    source_description = ''
    settings_sha256 = None
    if len(settings_files) == 1:
        import hashlib
        settings = _read(settings_files[0])
        source = settings.get('configuration', {}).get('stimulation_source', {})
        if source.get('taxel') is not None:
            source_description = (f" The controller selected tactile taxel "
                                  f"**{source['taxel']}** with **{source.get('encoding', 'unspecified')}** encoding.")
        settings_sha256 = hashlib.sha256(settings_files[0].read_bytes()).hexdigest()
    timing = _read(analysis/'command_timing'/'summary.json')
    transitions = _read(analysis/'frequency_transitions'/'summary.json')
    shutdown = _read(analysis/'stimulation_shutdown'/'summary.json')
    score = _read(analysis/'command_delivery_scorecard.json')
    robustness = _read(analysis/'frequency_robustness_summary.json')
    ack = _read(analysis/'ack_physical_observability'/'summary.json')
    manifest = _read(analysis/'extended_analysis_manifest.json')

    provenance = manifest['provenance']
    for name, summary in [('frequency_transitions', transitions),
                          ('stimulation_shutdown', shutdown), ('command_timing', timing)]:
        for key, value in provenance.items():
            if summary.get('provenance', {}).get(key) != value:
                raise ValueError(f'{name} provenance differs from verified consolidated report')
    for key, value in provenance.items():
        if score.get('provenance', {}).get(key) != value:
            raise ValueError('Protocol scorecard provenance differs from verified consolidated report')
    if robustness.get('total_approved_intervals') != manifest['approved_intervals']:
        raise ValueError('Robustness interval count differs from signed approval')

    sf = score['host_commands']['sf']
    sc = score['host_commands']['sc']
    physical = score['physical_ack_observability']
    settled = score['settled_electrical_intervals']
    if physical is None or settled is None:
        raise ValueError('Full scientific report requires per-ACK and robustness analyses')
    if sf['requested'] != sf['acks_ok'] + sf['acks_non_ok'] + sf['requests_without_ack']:
        raise ValueError('SF request/ACK arithmetic inconsistent')
    if sc['requested'] != sc['acks_ok'] + sc['acks_non_ok'] + sc['requests_without_ack']:
        raise ValueError('SC request/ACK arithmetic inconsistent')
    if ack['sf_ack_success_in_provisional_scope_window'] != physical['in_scope_provisional']:
        raise ValueError('Per-ACK and scorecard scope totals inconsistent')
    if transitions['approved_intervals'] != manifest['approved_intervals']:
        raise ValueError('Transition totals inconsistent')
    if shutdown['operator_excluded_negative_only'] < 0:
        raise ValueError('Invalid number of negative-only events')

    record = robustness['record_summaries'][0]
    eligible = int(record['baseline_ack_stable'])
    strict = int(record['strict_ack_stable'])
    if eligible != settled['eligible_intervals']:
        raise ValueError('Settled electrical interval totals differ')
    accuracy = record['baseline_frequency_error_hz']
    if accuracy['n'] != eligible:
        raise ValueError('Frequency robustness error sample count differs')
    if eligible - record['baseline_ack_outside_tolerance'] != settled['diagnostically_compatible_intervals']:
        raise ValueError('Frequency robustness outliers differ')
    offset_rows = transitions['offset_sensitivity']
    if not offset_rows or not any(float(row['shift_ms_assumed']) == 0 for row in offset_rows):
        raise ValueError('Transition offset scenarios missing nominal result')
    nominal = next(row for row in offset_rows if float(row['shift_ms_assumed']) == 0)
    if nominal['counts'].get('IN_INTERVAL_ACK_COMPATIBLE_ONLY') != transitions['active_transition_in_interval_ack_match_count']:
        raise ValueError('Nominal transition offset count is inconsistent')

    host = timing.get('host_state_timing', {})
    throughput = host.get('throughput_event_counts', {})
    if not throughput.get('mcu_messages') or not host.get('bins'):
        raise ValueError('Requires full-session host throughput analysis')
    ps = timing['pipeline_timing_stats']
    scope_duration = float(metadata['plan']['nominal_observed_scope_duration_s'])
    scope_points = int(metadata['plan']['points'])
    host_duration = float(host['bins'])
    _window_plot(analysis, scope_duration, host_duration)
    _accuracy_plot(analysis, eligible)
    _timing_figure(analysis)

    transition_figure = next((candidate for candidate in (
        'ack_physical_observability/restart_02_detail.png',
        'ack_physical_observability/restart_01_detail.png',
        'frequency_transitions/figures/transition_overview.png',
    ) if (analysis / candidate).is_file()), None)
    if transition_figure is None:
        raise FileNotFoundError('No verified restart or transition figure available')
    has_restart_detail = 'restart_' in transition_figure
    transition_caption = (
        'Top: physical CH1 inverse-period frequencies between approved biphasic pulses near a restart. '
        'Bottom: SF ACK frequencies provisionally mapped to the scope clock. The dashed line marks the first pulse '
        'following a long pause; no continuous stimulation is inferred inside that gap.'
        if has_restart_detail else
        'Approved physical inverse-period frequencies and provisionally mapped SF ACK events across the capture; '
        'their cross-clock ordering is not independently qualified.'
    )
    for relative in (
        'command_delivery_scorecard.png',
        'stimulation_shutdown/figures/incomplete_pulse_morphology.png',
    ):
        if not (analysis / relative).is_file():
            raise FileNotFoundError('Main report supporting figure missing: '+relative)

    fmt = lambda x: f'{float(x):.3f}'
    shift_table = ['| Hypothetical host-to-scope shift (ms) | Active intervals compatible only with an in-interval SF ACK |',
                   '| ---: | ---: |']
    for row in sorted(offset_rows, key=lambda r: float(r['shift_ms_assumed'])):
        shift_table.append(f"| {float(row['shift_ms_assumed']):+.1f} | {row['counts'].get('IN_INTERVAL_ACK_COMPATIBLE_ONLY', 0)} |")

    lines = [
        '# Matrix32 tactile-to-electrical stimulation: consolidated experiment report',
        '',
        '**Recording:** '+metadata.get('run_id','not specified'),
        '',
        '> **Principal conclusion.** All recorded SF frequency requests received successful protocol acknowledgments. '
        'Within a selected stable-state subset, electrical pulse periods agree closely with the acknowledged '
        'frequencies. Rapid updates, muted states and unqualified cross-clock alignment prevent a defensible '
        'per-command electrical execution or failure rate.',
        '',
        '## At a glance: research questions and evidence', '',
        '| Question | Result | Evidence classification |', '| --- | --- | --- |',
        f"| Were frequency requests acknowledged? | **{sf['acks_ok']}/{sf['requested']} SF OK ACKs**; {sf['requests_without_ack']} missing | Direct host protocol record |",
        f"| Were current-control requests acknowledged? | **{sc['acks_ok']}/{sc['requested']} SC OK ACKs** | Direct host protocol record |",
        f"| Were stable frequencies reproduced? | **{settled['diagnostically_compatible_intervals']}/{eligible} eligible** intervals within diagnostic tolerance; median absolute difference {fmt(accuracy['median_abs'])} Hz | Direct CH1 periods + conditional command pairing |",
        f"| Can every acknowledged change be proven electrically executed? | **No**: {physical['matching_settled_period_evidence']} in-scope ACKs have matching settled-period evidence; exact executed/failed counts unidentifiable | Conditional compatibility, not command execution proof |",
        f"| What happens with rapid commands? | {transitions['active_transition_in_interval_ack_match_count']} active transitions compatible with an in-interval SF ACK at nominal alignment | Timing-sensitive association |",
        f"| What are the incomplete events? | {shutdown['operator_excluded_negative_only']} signed negative-only exclusions, not complete pulses | Direct voltage morphology; command relationship provisional |",
        f"| How regular is tactile ingress? | {fmt(ps['mcu_arrival_dt_ms']['mean_ms'])} +/- {fmt(ps['mcu_arrival_dt_ms']['std_ms'])} ms (mean +/- SD) | Direct PC arrival intervals |",
        '',
        '## Experiment and measurement windows', '',
        'A single Tektronix record captured **CH1 electrical output across an operator-verified isolated resistive dummy load** '
        'and **CH2 Nano Every D7 timecode** at approximately '
        f'**{scope_points:,} samples per channel** for an approximately '
        f'**{scope_duration:g}-second** scope window. The MCU/controller logs span approximately '
        f'**{host_duration:g} seconds** and contain **{throughput["mcu_messages"]:,} MCU message arrivals**. '
        'The 20-second waveform and much longer host session are different observation windows; the host-to-scope '
        'offset is **not independently qualified**. No person was connected.' + source_description,
        '',
        _image('presentation_figures/observation_windows.png',
               'Bars show total available host-log and scope-record durations using their respective clocks. '
               'The lengths do not indicate alignment or overlap of individual events.', 1),
        '',
        '**Evidence handling.** Physical periods use consecutive onset timestamps of **'+str(manifest['approved_onsets'])+
        '** operator-approved, positive-phase-confirmed biphasic CH1 pulses (**'+str(manifest['approved_intervals'])+
        '** intervals). The **'+str(shutdown['operator_excluded_negative_only'])+
        '** operator-excluded negative-only events remain separate diagnostics. '
        'No approved pulse, review decision, original waveform or Nano clock fit is changed by report generation.',
        '',
        '## 1. Frequency requests and protocol delivery', '',
        f"The controller issued **{sf['requested']} SF value-change requests**, all acknowledged OK; "
        f"**{sf['muted_scheduler_sf400']}** were SF400 muted-scheduler commands and cannot be interpreted as 400 Hz electrical stimulation. "
        f"All **{sc['requested']}** SC commands were acknowledged OK. Of the SF ACKs, "
        f"**{physical['in_scope_provisional']}** lie within the *estimated* waveform window; "
        f"**{physical['matching_settled_period_evidence']}** have at least one matching isolated settled physical interval "
        f"under the audit assumptions, and **{physical['in_scope_no_individually_attributable_evidence']}** do not. "
        f"The remaining **{physical['outside_scope_provisional']}** ACKs are outside the estimated waveform window. "
        'These categories **must not** be converted to an executed/not-executed fraction.',
        '',
        _image('command_delivery_scorecard.png',
               'Left: SF and SC host request/ACK totals. Right: mutually exclusive SF ACK observability categories '
               'based on the provisional scope mapping; a matching period is not unique proof of electrical application.', 2),
        '',
        '## 2. Settled-state electrical frequency agreement', '',
        f"Across **{eligible}** eligible settled CH1 intervals, **{eligible-record['baseline_ack_outside_tolerance']}** "
        f"fall within diagnostic tolerance; the median absolute error is **{fmt(accuracy['median_abs'])} Hz**, "
        f"the 95th percentile is **{fmt(accuracy['p95_abs'])} Hz** and the maximum is **{fmt(accuracy['max_abs'])} Hz**. "
        f"A stricter timing qualification leaves **{strict}** comparable intervals "
        f"(**{strict-record['strict_ack_outside_tolerance']}** within tolerance). "
        'This is evidence of frequency accuracy for selected observable states, **not** for every request or every physical cycle.',
        '',
        _image('presentation_figures/settled_frequency_accuracy.png',
               'Left: measured inverse-period frequency versus ACK-indicated command for eligible settled intervals '
               '(dashed line = equality). Right: signed measured-minus-commanded frequency difference; zero denotes agreement. '
               'Excluded and transition-sensitive intervals are not plotted.', 3),
        '',
        '[Detailed robustness report](frequency_robustness_report.md) | '
        '[Eligible-interval evidence](frequency_robustness_all_intervals.csv)',
        '',
        '## 3. Rapid frequency updates and transition observability', '',
        f"In the nominal provisional map, **{transitions['prior_ack_mismatches_regular']}** regular intervals "
        'do not match the preceding SF ACK; **'
        f"{transitions['active_transition_in_interval_ack_match_count']}** active intervals are compatible with an SF ACK inside "
        'the same physical cycle. The selected transition example displays the relationship between ACKs '
        'and observed physical periods, without establishing firmware scheduling causation. '
        'Cross-clock event order depends on the assumed mapping.',
        '',
        _image(transition_figure, transition_caption, 4),
        '',
        '**Timing sensitivity (not a confidence interval).** Moving the assumed host-to-scope offset changes '
        'which SF ACKs appear inside each electrical interval:', '',
        *shift_table, '',
        'These are hypothetical alignment scenarios **not calibrated uncertainty bounds**. A precise electrical '
        'application instant for each ACK has not been measured.',
        '',
        '[Per-cycle transition evidence](frequency_transitions/transitions.csv) | '
        '[All transition figures and offset scenarios](frequency_transitions/report.md)',
        '',
        '## 4. Host scheduling and BLE communication', '',
        f"The MCU-to-host stream contains **{throughput['mcu_messages']:,}** messages at "
        f"**{fmt(ps['mcu_arrival_dt_ms']['median_ms'])} ms median** inter-arrival spacing, "
        f"with SD **{fmt(ps['mcu_arrival_dt_ms']['std_ms'])} ms**. "
        f"SF requests have a **{fmt(ps['sf_request_dt_ms']['median_ms'])} ms** median spacing, "
        f"and SF request-to-ACK completion has **{fmt(ps['sf_request_to_ack_ms']['median_ms'])} ms** median latency. "
        f"The 95th percentile of SF request-to-BLE-write delay is **{fmt(ps['sf_request_to_ble_write_ms']['p95_ms'])} ms**, "
        f"compared with **{fmt(ps['sf_ble_write_to_ack_ms']['p95_ms'])} ms** for BLE-write-to-ACK. "
        'Inter-request gaps reflect controller decisions and muted intervals, **not radio jitter**. '
        'All request/ACK timing is measured on the same PC clock and does not quantify electrical application latency.',
        '',
        _image('presentation_figures/host_timing_overview.png',
               'Panel A: per-second MCU arrivals, SF host requests, BLE writes and ACKs with ACK-indicated current state. '
               'Panel B: box-and-whisker distributions of the corresponding full-session message cadences. '
               'Long command-free gaps are included; source state and host timing are not electrical output measurements.', 5),
        '',
        '[Detailed timing distributions](command_timing/report.md) | '
        '[State-separated timing](command_timing/state_timing_statistics.csv) | '
        '[Per-second throughput](command_timing/throughput_per_second.csv)',
        '',
        '## 5. Current shutdown, restart and incomplete waveforms', '',
        f"The signed inventory excludes **{shutdown['operator_excluded_negative_only']}** negative-only voltage events. "
        f"Under the nominal mapping, **{shutdown['negative_only_after_latest_sc0_ack_provisional']}** "
        'occur after the latest SC0 ACK. Their morphology is directly observed, but SC0 timing is only provisionally '
        'associated and does not establish an electrical cutoff time or voltage-transient mechanism.',
        '',
        _image('stimulation_shutdown/figures/incomplete_pulse_morphology.png',
               'Operator-excluded negative-only CH1 voltage events. Each lacks the required positive-phase confirmation '
               'and contributes neither an approved biphasic pulse nor a measured stimulation period.', 6),
        '',
        '[Current-transition analysis](stimulation_shutdown/report.md) | '
        '[Source event details](stimulation_shutdown/incomplete_events.csv)',
        '',
        '## Conclusions and remaining questions', '',
        '**Established:** All recorded SF and SC requests have successful protocol replies; selected settled '
        'biphasic electrical cycles have small frequency deviations; the MCU stream arrives at a regular host cadence. '
        '**Suggestive, not established:** Rapid SF updates may be superseded or deferred; incomplete CH1 excursions '
        'coincide with SC0-indicated states. **Not determined:** Which individual acknowledged SF values were '
        'electrically applied, precise command-to-output latency, electrical current or charge balance, and the '
        'internal frequency-update scheduling rule. This experiment is **not a safety validation**.',
        '',
        '**Best follow-up experiment:** Use intentionally spaced single-frequency plateaus and short bursts across '
        'a verified dummy load, with an independently calibrated timing marker relating command transmission to CH1. '
        'That can test deferred/superseded update hypotheses without relying on nominal host-to-scope alignment.',
        '',
        '## Detailed evidence, limitations and reproducibility', '',
        '| Supporting analysis | Evidence |', '| --- | --- |',
        '| [Per-ACK audit](ack_physical_observability/final_report.md) | Individual ACK classifications and clock-association caveats |',
        '| [Robustness audit](frequency_robustness_report.md) | Full tolerance definitions, period/frequency errors, held-out clock tests |',
        '| [Frequency transitions](frequency_transitions/report.md) | Full interval classifications and offset sensitivity |',
        '| [Shutdown and restart](stimulation_shutdown/report.md) | All signed negative-only source events |',
        '| [Command timing](command_timing/report.md) | Cadence distributions, active/muted comparisons and BLE timing |',
        '| [Legacy consolidated report](extended_analysis_technical_report.md) | Previous 12-figure technical index, retained in full |',
        '',
        'The experiment used one 20-second scope record and a longer continuous host/controller log; '
        'a Nano Gray-timecode mapping was analyzed but **not qualified**. '
        'All exported evidence remains linked to the signed review and SHA-256 source fingerprints. '
        'Original waveforms, current-control commands, approval signatures, and clock evidence were not rewritten.',
        '',
        '[Analysis provenance](extended_analysis_manifest.json) | '
        '[Protocol and physical observability scorecard](command_delivery_scorecard.json)',
        '',
    ]
    # Scheduled prompts are not separately verified tactile actions.
    cues = metadata.get('plan', {}).get('operator_prompts', {}).get('schedule', [])
    if cues:
        at = ', '.join(f"{float(cue['at_s']):g} s ({cue['text'].lower().rstrip('.')})"
                       for cue in cues if 'at_s' in cue and 'text' in cue)
        note = ('**Operator cue schedule (host timer only):** ' + at + '. '
                'Cue delivery is not proof of an operator action or of an event at that time on CH1.')
        idx = lines.index('## 1. Frequency requests and protocol delivery')
        lines[idx:idx] = [note, '']
    text = '\n'.join(lines)
    figures = _check_figures(analysis, text)
    technical_archive = analysis / TECHNICAL_REPORT_FILENAME
    temp_technical = analysis / (TECHNICAL_REPORT_FILENAME + '.tmp')
    temp_report = analysis / 'final_experiment_report.md.tmp'
    temp_report.write_text(text, encoding='utf-8')
    # Keep the first archived technical report immutable on subsequent rebuilds.
    if not technical_archive.is_file():
        temp_technical.write_bytes(current_technical)
        temp_technical.replace(technical_archive)
    temp_report.replace(old_path)
    index = analysis / 'README.md'
    if not index.exists() or index.read_text(encoding='utf-8').startswith('# Matrix32 analysis report entry points'):
        index.write_text(
            '# Matrix32 analysis report entry points\n\n'
            '**Authoritative:** [Scientific experiment report](final_experiment_report.md)\n\n'
            '**Detailed technical analysis:** [Full original technical consolidation](extended_analysis_technical_report.md)\n\n'
            'The older `final_integrated_report.md`, if present, remains an historical index. '
            'All detailed analyses and source-locked approvals remain under their existing paths.\n',
            encoding='utf-8')
    return {'primary_report': 'final_experiment_report.md',
            'technical_report': TECHNICAL_REPORT_FILENAME,
            'primary_figures': figures, 'figure_count': len(figures),
            'presentation_schema': 'matrix32_scientific_report_v1',
            'controller_settings_sha256_if_available': settings_sha256}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    from experiment_analysis.paths import analysis_root
    return present(analysis_root(args.run_dir))


if __name__ == '__main__':
    main()
