"""Descriptive italic captions for generated Matrix32 Markdown figures.

Only newly generated analysis reports use these captions. Signed reviews
and historical snapshots are not edited.
"""
from __future__ import annotations
from pathlib import Path
import re

IMAGE_LINE = re.compile(r'(?m)^!\[([^\]\n]*)\]\(([^)\n]+)\)[ \t]*$')
FIGURE_NOTES = {
    'command_delivery_scorecard.png':
        'Host SF/SC requests and successful acknowledgments on the left; SF acknowledgments grouped by available settled electrical-period evidence on the right. The panels have different denominators.',
    'command_physical_timeline.png':
        'Approved physical pulse periods and provisionally aligned SF control events. Positions across the host and scope clocks are not independently calibrated.',
    'transition_overview.png':
        'Approved scope-local pulse frequencies alongside provisionally placed SF acknowledgments. Cross-clock event ordering is conditional on the adopted mapping.',
    'transition_timing_sensitivity.png':
        'Transition classifications under alternative assumed host-to-scope offsets. Scenarios are not calibrated confidence limits.',
    'shutdown_overview.png':
        'Complete approved pulses, excluded negative-only events, and provisionally positioned SC0/SC50 acknowledgments.',
    'incomplete_pulse_morphology.png':
        'Voltage characteristics of operator-excluded negative-only events. These are not counted as complete biphasic pulses.',
    'sc0_offset_sensitivity.png':
        'Negative-only event associations with SC acknowledgments across assumed host-to-scope timing offsets.',
    'latency_distribution.png':
        'SF and SC host-monotonic request-to-ACK durations, not measured electrical application latency.',
    'command_burst_analysis.png':
        'Spacing between SF acknowledgments compared with the previous acknowledged cycle period; the diagonal indicates equal durations.',
    'command_timeline.png':
        'Acknowledged SF frequency and protocol completion latency over the controller session.',
    'message_cadence_boxplots.png':
        'MCU message and SF command spacing: boxes show P25–P75, lines medians, diamonds means, whiskers P05–P95, triangles P99. Times use a logarithmic axis.',
    'command_latency_boxplots.png':
        'Host-observed MCU-to-request, request-to-write, and write-to-ACK durations. Percentile and mean markers follow the other timing box plot.',
    'throughput_per_second.png':
        'One-second counts of MCU arrivals, SF requests, BLE writes and SF ACKs. Bottom bars give each second’s fraction in the last acknowledged SC state, including unknown time.',
    'active_muted_timing_boxplots.png':
        'Timing grouped by last acknowledged SC state; durations spanning an SC ACK or preceding the first SC ACK are excluded. Boxes/markers use the timing box-plot convention.',
}

def caption_for(path):
    name = Path(path).name
    if name in FIGURE_NOTES:
        return FIGURE_NOTES[name]
    if re.fullmatch(r'transition_\d+\.png', name):
        return ('Detailed approved physical periods and candidate SF events '
                'around one transition; cross-clock ordering is provisional.')
    if re.fullmatch(r'R\d+_raw\.png', name):
        return ('Original CH1 samples around an operator-excluded negative-only '
                'event, shown for morphology review rather than period counting.')
    raise ValueError(f'No caption specified for figure: {path}')

def add_figure_captions(markdown):
    """Insert italic text directly after each standalone Markdown figure.

    Raises if an unknown figure would be left without a caption.
    Repeated application does not duplicate captions.
    """
    lines = markdown.splitlines(keepends=True)
    result = []
    for i, line in enumerate(lines):
        result.append(line)
        match = IMAGE_LINE.fullmatch(line.rstrip('\r\n'))
        if not match:
            continue
        caption = f'*Figure — {caption_for(match.group(2))}*'
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and lines[j].strip() == caption:
            continue
        # Leave a full blank line after the caption, even before the next figure.
        result.append('\n' + caption + '\n\n')
    return ''.join(result)
