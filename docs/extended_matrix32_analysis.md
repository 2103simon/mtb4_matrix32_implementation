# Matrix32 extended scientific analyses

The extended analysis operates offline, using **only the source-locked,
operator-signed biphasic pulse inventory** and the original raw waveform,
MCU samples, BLE command trace and provisional clock mapping. It does not
change the recording, create new electrical pulse candidates, edit approvals,
command hardware or infer physical application time from BLE acknowledgments.

## Run everything (one command)

From the repository root (on `feature/live-oscilloscope-recording`):

```bash
MTB4_RUN="data/recordings/20261124T114542660847Z_integrated_hw_10M_20s_resume_check"
python -u -m oscilloscope.run_extended_analysis --run-dir "$MTB4_RUN"
```

The entry point generates independent module outputs then combines them into:

```text
$MTB4_RUN/analysis/final_experiment_report.md
$MTB4_RUN/analysis/extended_analysis_manifest.json
```

Each module may also be run on its own:

```bash
python -u -m oscilloscope.analyze_frequency_transitions --run-dir "$MTB4_RUN"
python -u -m oscilloscope.analyze_stimulation_shutdown --run-dir "$MTB4_RUN"
python -u -m oscilloscope.analyze_command_timing --run-dir "$MTB4_RUN"
python -u -m oscilloscope.build_final_experiment_report --run-dir "$MTB4_RUN"
```

The last command requires all three module summaries. It refuses to combine
incompatible source hashes or missing figures. Existing per-ACK and frequency
robustness reports are linked when present and checked for consistency.

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
  final_experiment_report.md
  extended_analysis_manifest.json
```

## Interpretation

1. **Frequency transitions:** physical periods use approved negative onset
   timestamps. Comparing the last pre-interval ACK with any in-interval ACK
   is *compatibility evidence*, never proof of internal application at a
   particular electrical cycle boundary. The ±0.5–3.5 ms comparisons are
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
