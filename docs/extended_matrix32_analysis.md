# Matrix32 extended scientific analyses

> **Authoritative report (November 2026 presentation revision):**
> `analysis/final_experiment_report.md` is now the concise six-figure scientific report
> with research-question conclusions, measured frequency errors, timing sensitivity,
> protocol timing, and limitations. The previous longer report is automatically
> preserved as `analysis/extended_analysis_technical_report.md`, and the older
> `final_integrated_report.md` is a historical index only. The detailed
> frequency-transition, shutdown, timing, robustness, and per-ACK reports remain
> unchanged. See [scientific report documentation](scientific_report_presentation.md).
>
The extended analysis operates offline, using **only the source-locked,
operator-signed biphasic pulse inventory** and the original raw waveform,
MCU samples, BLE command trace and provisional clock mapping. It does not
change the recording, create new electrical pulse candidates, edit approvals,
command hardware or infer physical application time from BLE acknowledgments.

Before analyzing an existing recording, perform the [verified migration](recording_results_separation.md); files remain in the original raw recording until then.\n\n## Run everything (one command)

From the repository root (on `refactor/separate-recordings-results`):

```bash
MTB4_RUN="data/recordings/20261124T114542660847Z_integrated_hw_10M_20s_resume_check"
python -u -m experiment_analysis.run_extended_analysis --run-dir "$MTB4_RUN"
```

The entry point generates independent module outputs then combines them into:

```text
results/<RUN_ID>/analysis/final_experiment_report.md
results/<RUN_ID>/analysis/extended_analysis_manifest.json
```

Each module may also be run on its own:

```bash
python -u -m experiment_analysis.analyze_frequency_transitions --run-dir "$MTB4_RUN"
python -u -m experiment_analysis.analyze_stimulation_shutdown --run-dir "$MTB4_RUN"
python -u -m experiment_analysis.analyze_command_timing --run-dir "$MTB4_RUN"
python -u -m experiment_analysis.build_final_experiment_report --run-dir "$MTB4_RUN"
```

The last command requires all three module summaries. It refuses to combine
incompatible source hashes or missing figures. Existing per-ACK and frequency
robustness reports are linked when present and checked for consistency.

## Answering "how many requests were ACKed, and how many were executed?"

The consolidated report begins with a **Requested → ACKed → physically observed**
scorecard. It is regenerated automatically by the runner, or independently:

```bash
python -u -m experiment_analysis.build_final_experiment_report --run-dir "$MTB4_RUN"
```

The scorecard uses the recorded SF/SC requests and replies, source-linked
host-timing pairs, the existing per-ACK observability audit (when available),
and the settled-frequency robustness summary (when available). The builder
**fails closed on inconsistent command totals or mismatched source hashes**.

For the 2026-11-24 reference recording there are 266 SF requests and 266
successful SF ACKs (including 22 muted-scheduler SF400 changes), plus 41/41
successful SC ACKs. There are 246 SF ACKs inside the *estimated* scope window:
59 have matching, settled electrical-period evidence; the remaining 187 lack
individually attributable settled evidence (31 potentially superseded, 37
muted at ACK, 6 muted before the next pulse, 27 insufficiently settled, and
86 with other insufficient isolated evidence). The other 20 SF ACKs lie outside
the scope window. The independent steady-state audit has 85/85 compatible
eligible *intervals*. **Do not combine those interval and command counts into
an executed-command percentage.**

The report deliberately labels the numbers of SF commands **proved executed**
and **proved unexecuted** as **not determinable**. A BLE OK indicates protocol
acknowledgment only, an electrical period matching one ACK is not unique proof
of its execution, and the host–scope time offset remains unqualified.

Additional outputs under `results/<RUN_ID>/analysis/`:
`command_delivery_scorecard.json` and `command_delivery_scorecard.png`.
These outputs never modify source waveforms, clock fits, operator decisions,
or existing signed approvals.

## Full-session throughput and active/muted timing

The command-timing module now additionally generates:

- `figures/throughput_per_second.png` and `throughput_per_second.csv`: one-second host-clock event counts for MCU messages, SF requests, BLE write starts, and SF ACKs, including one-second fractions of the last acknowledged SC current state.
- `figures/active_muted_timing_boxplots.png`, `state_timing_statistics.csv`, and `state_timing_samples.csv`: MCU arrival spacing, SF request/ACK spacing, request-to-ACK completion and BLE transport-stage latency, grouped by ACK-indicated ACTIVE/MUTED state, with mean, sample SD, median, P05/P25/P75/P95/P99 and maximum.
- `summary.json` contains `host_state_timing` with conservation checks of raw host event counts and state sample totals.

**State is the most recent successful SC ACK**, not measured electrical current. Before the first SC ACK it is UNKNOWN. A duration is compared as active or muted only if **it does not cross an intervening SC ACK**; unknown and transition-crossing durations remain in the exported samples but are excluded from the box plots. The time-bin state bar can show mixtures within a second. Long SF-inactivity gaps are retained; they must not be called communication jitter without inspecting controller state.

Each Markdown figure in the three extended analysis reports and the newly regenerated consolidated report is followed immediately by a short *italic descriptive caption*. The captions describe plotted quantities and how to read marks, without making an execution claim. Historic signed review reports and the earlier independent per-ACK audit are not retroactively modified.

Regenerate all figures and captions with the existing runner:

```bash
python -u -m experiment_analysis.run_extended_analysis --run-dir "$MTB4_RUN"
```

Or regenerate only the command-timing data and combined report (if the other two analyses already exist):

```bash
python -u -m experiment_analysis.analyze_command_timing --run-dir "$MTB4_RUN"
python -u -m experiment_analysis.build_final_experiment_report --run-dir "$MTB4_RUN"
```

Focused hardware-free regression test:

```bash
python -m unittest discover -s tests -p 'test_host_timing_views.py' -v
```

## Output structure

```text
analysis/
  frequency_transitions/
    report.md                    # prior-vs-in-interval SF compatibility
    transitions.csv              # ALL approved intervals
    transition_classifications.csv
    offset_sensitivity.json
    summary.json
    figures/transition_overview.png
    figures/transition_timing_sensitivity.png
    figures/transition_01.png ...
  stimulation_shutdown/
    report.md                    # signed negative-only events + SC0/SC50
    incomplete_events.csv        # only explicit signed EXCLUDE events
    shutdown_restart_events.csv  # every successful SC ACK
    summary.json
    figures/shutdown_overview.png
    figures/incomplete_pulse_morphology.png
    figures/Rxxxxx_raw.png ...
  command_timing/
    report.md                    # host-monotonic request→ACK latencies
    sf_command_latency.csv
    sc_command_latency.csv
    command_rate_vs_cycle.csv
    summary.json
    figures/latency_distribution.png
    figures/command_burst_analysis.png
    figures/command_timeline.png
    figures/message_cadence_boxplots.png
    figures/command_latency_boxplots.png
    timing_distribution_statistics.csv
    timing_distribution_samples.csv
  final_experiment_report.md
  extended_analysis_manifest.json
```

## Incoming MCU and Matrix32 communication cadence

The command-timing module additionally reports **two box plots**, with a
machine-readable table of n, mean, sample standard deviation, median,
5th/25th/75th/95th/99th percentiles and maximum:

- **Cadence:** PC arrival-to-arrival MCU Δt; native MCU source-timestamp Δt
  (1-ms quantized); SF request-to-request, BLE write-begin-to-write-begin,
  and SF ACK-to-ACK Δt.
- **Exchange delays:** MCU PC arrival to SF request, SF request to BLE write
  begin, BLE write begin to SF ACK, SF request to SF ACK, and SC request to
  SC ACK.

Whiskers show P05–P95; a diamond shows the mean, triangles show P99,
and the table preserves the full maximum and standard deviation. The
vertical axes are logarithmic because some host inter-command gaps are
much longer than normal MCU arrival intervals.

All statistics describe the **entire recorded MCU/controller session**,
which may extend beyond the 20-second oscilloscope trace. Long
inter-command gaps can reflect muted or idle periods and must not
automatically be counted as radio jitter. Host-monotonic write/ACK
durations are **not electrical application latency**. No MCU-to-scope
clock synchronization is claimed by these host-local statistics.

Regenerate the plots and table without rerunning other analyses:

```bash
python -u -m experiment_analysis.analyze_command_timing --run-dir "$MTB4_RUN"
python -u -m experiment_analysis.build_final_experiment_report --run-dir "$MTB4_RUN"
```

## Interpretation

1. **Frequency transitions:** physical periods use approved negative onset
   timestamps. Comparing the last pre-interval ACK with any in-interval ACK
   is *compatibility evidence*, never proof of internal application at a
   particular electrical cycle boundary. The −3.5, −1.8, 0, +1.8, and +3.5 ms comparisons are
   hypothetical offset shifts and **not calibrated uncertainty bounds**.
2. **Shutdown/restart:** `REJECTED_MISSING_POSITIVE_PHASE` source events must
   remain operator-excluded from the frequency inventory. Original CH1 samples
   around those crossings are plotted for diagnostics. An SC0/SC50 ACK is not
   a physical on/off marker; voltage morphology does not establish injected
   current, charge balance or electrical safety.
3. **Command timing:** SF/SC request→ACK durations and BLE write→ACK durations
   are measured on a single host-monotonic clock; they are *protocol timing*,
   not electrical execution latency. Comparing inter-ACK spacing to a previous
   commanded cycle indicates *command pressure*, not executed cycles. Muted
   scheduler SF400 updates are distinguished from physical 400 Hz output.

Tests (hardware-free):

```bash
python -m unittest discover -s tests -p 'test_extended_matrix32_analysis.py' -v
```

Repeat the signed-recording regression (runs the analyses and generates their
figures without changing source evidence):

```bash
MTB4_EXTENDED_RUN="$MTB4_RUN" \
python -m unittest discover -s tests -p 'test_extended_matrix32_analysis.py' -v
```

## Provenance

All three `summary.json` documents include the raw CH1, approved-onset,
command-log, MCU-rate and epoch-clock file hashes and a version string.
The aggregate report rejects mismatched or stale input, and none of the
three modules requires the upstream detector to run again.
