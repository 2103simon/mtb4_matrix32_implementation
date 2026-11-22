# SA-II phase-preserving scheduling — firmware behavior v2

**Status:** proposed algorithm revision; **not** a maintainability-only refactor.
Current implementation lives in `FalangeM4/src/encoding.cpp::SaEncoding()`
and `FalangeM4/include/encoding.h::SAParams`.
Do **not** attribute recordings made before reflashing to this revision.

## Why this revision exists

The manually applied E04 sustained-pressure recording made with the earlier
firmware (v1 / `legacy_anchor_v1`) showed SA-II rates locked to approximately
one or two spikes per MTB4 acquisition (~69/138 Hz). The legacy algorithm
computed an interspike interval but placed the newest event at
`currentTime` and set `lastEventTime[id] = currentTime` after each batch.
Thus the oscillator's phase was reset to each new ~14/15-ms taxel sample.

## Required invariants

For every channel, every emitted SA-II timestamp `t` must satisfy:

```text
previous_sample_timestamp < t <= current_sample_timestamp
```

- No event may be created in the already closed previous sample window.
- No event may be created after the current sample.
- Two SA-II events from one channel must not share a timestamp, including
  at sample boundaries; `previous_sample_timestamp` is excluded.
- The requested frequency comes from the **current taxel value**:
  `f_Hz = P_uint8 * (sa_max_frequency_hz / 255)`; `P=0` emits no spikes.
- The refractory period remains a minimum interspike spacing.
- `lastEventTime[id]` always denotes an **actually emitted** event, not
  `currentTime` or a skipped/past-due virtual event.

## Algorithm

1. Compute the desired interspike period `1000/f_Hz` milliseconds.
2. Bound it by the refractory period, then represent it as unsigned fixed-
   point Q16 milliseconds (`1 ms = 65536` units).
3. Each channel stores `lastEventTime` and the fractional remainder of
   the scheduled phase `fractionalPeriodPhaseQ16`.
4. For a new `(lastTime, currentTime]` window, use integer division to
   find the first step on the existing phase lattice that occurs **after**
   `lastTime`. Skip scheduled past times without manufacturing old events
   or backlogged bursts.
5. Emit every due spike not later than `currentTime` and update both the
   actual last-emitted timestamp and fractional phase. Both timestamp
   endpoints and the source's integer-millisecond timebase are respected.
6. Use unsigned differences for the 32-bit MCU millisecond timer. Windows
   spanning >= 2^31 ms are deliberately rejected as ambiguous; other
   components of the frame transport have their own wrap limitations.

For a constant pressure code `P=255` and `SA_FREQ=150`, the target period
is ~6.667 ms. With 1-ms output precision, the schedule emits near:
`6, 13, 19, 26, 33, 39, 46, 53, 59, ...` ms. Adjacent spacings alternate
6/7 ms, approximating 150 Hz *on average* despite a 14–15 ms taxel sampling
interval. At sufficiently high target frequencies the refractory limit still
caps the rate.

The algorithm is a **time-stamped software encoder** that reconstructs
spikes using a piecewise-constant pressure over each sample interval; it
does not make the physical sensor sample faster and does not imply measured
sub-sample pressure information.

## Scientific and implementation scope

- **RA-I** equations, thresholds, polarity and refractory logic unchanged.
- No changes to CAN acquisition, taxel inversion, serial commands/ACKs,
  event packet layout or event buffer.
- The existing K2 >63-event-per-batch transport defect is **NOT fixed**;
  simultaneous rapid changes across many taxels could still exceed it.
- Measured ~69 Hz sensor acquisition, source-side loss accounting and
  MCU/host clock drift remain independently unresolved (issue #3).
- Test evidence from the old E03/E04 manual recordings remains valid for
  the **legacy firmware behavior** and must not be relabeled as v2 data.

## Versioned analysis and native gates

`experiments/mcu_validation/analysis/encoder_oracle.py` retains two named
models. `legacy_anchor_v1` is the **default** to reproduce existing archived
E03/E04 results; `phase_q16_v2` is the new firmware behavior. This selection
is *not automatically inferred* from a source Git checkout SHA, which
does not verify the binary actually flashed on the Feather.

**Caveat for new live recordings:** the MCU's fractional scheduling phase
was established before the raw capture began, and the captured wire frames
do not expose it. Seeding a Python oracle at the first recorded taxel
with fractional phase 0 does NOT reconstruct this unknown initial phase,
and excluding the first 5 seconds does NOT guarantee convergence of the
persistent phase. Compare **sustained firing rates and interval
distributions** to the model first. Event-timestamp equality from a seeded
live replay is a diagnostic, not a required exact-match gate unless the
initial phase is independently established.

The new model is compared against actual host-compiled production
`encoding.cpp` in `tests/mcu_validation/test_encoder_oracle.py`,
including event timestamps, channels, counts and final per-taxel
fractional phase state. Additional tests exercise 69 Hz acquisition,
refractory 1/3/10 ms, changes in pressure, zero pressure, missing sample
intervals and uint32 millis rollover.

Device-free regression and compile-only gates:

```bash
workon tactile
python -m pytest tests/firmware/ tests/serial/test_ra_threshold_ack.py tests/mcu_validation/ -v
cd FalangeM4
platformio run -e adafruit_feather_m4_can
```

## Required operator retest

After reviewing and approving this separate branch, flash it using the
laboratory's established Feather M4 procedure. Before claiming new SA-II
behavior, confirm the build/flash result and GUI communication and parameter
ACKs. Then acquire a **new** E04 manual hold recording with the same applied
settings (RA=3, refractory=3 ms, SA maximum=150 Hz), for example
`E04_MANUAL_HOLD_SA2_001`, using the existing single-owner configured
capture. **Do not overwrite** `E04_MANUAL_HOLD_001`.

Analyze the new recording explicitly with
`--sa-mode phase_q16_v2` in the E04 SA and encoder-comparison scripts.
Use `--sa-mode legacy_anchor_v1` for archived pre-flash trials.
The physical response is not force-calibrated; all scientific acceptance
gates remain unchanged pending appropriate hardware evidence.
