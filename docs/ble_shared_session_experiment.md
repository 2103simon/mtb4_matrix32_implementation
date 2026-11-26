# Shared persistent live SF/SC ACK session

The former opt-in transport is now the sole live SF/SC implementation on
this branch. There is no runtime legacy selector. Use the existing verified
resistive dummy-load bench only, with independent physical disconnect and
operator verification. No person or stimulation electrodes attached.

## Evidence and decision

Two matched deterministic replay pairs collected on 2026-11-26 used identical
recorded MCU inputs, 1,414 snapshots over 20.5 s, and the same controller
settings. Both showed substantially lower SC and SF-after-SC latency
(about 38-40% and 48-49% lower medians), fewer unreferenced target changes,
and no observed ACK failures. The second pair avoided digital storage-rail
clipping; exploratory resistor-branch charge estimates showed comparable
ordinary biphasic pulses and negative-only transient magnitude. Its pulse
inventories remain pending formal operator sign-off. Physical frequency
improvement is not established; electrical safety is not qualified.
The negative-only transients and SC0 -> SF400 workaround remain separate
investigations. No stimulation policy or firmware change is part of this
consolidation.

Historical raw runs (preserve in place):
- 20261126T090848918698Z_ble_ab_baseline
- 20261126T091212099927Z_ble_ab_shared
- 20261126T094712943788Z_ble_ab_baseline_20vdiv
- 20261126T095023427339Z_ble_ab_shared_20vdiv

The pre-consolidation revision 0baa0c0056967627fa431ff0f7f502c603049931 preserves the original experimental
selector and baseline transport for historical reproduction. Do not copy
raw recordings or change approved reports to reproduce the comparison.

## Design

- SF and SC use the same PersistentConfigAckSession and device exchange.
  Initial full configuration, SW, stopped probes and other indication-owning
  operations retain their own existing paths.
- One outstanding command; unique positive application ACK required. A failed
  exchange invalidates the verified configuration and closes its session.
  Generic ACKs lack request IDs: delayed unrelated ACKs cannot be conclusively
  identified by host code.
- Minimum 7.5 ms ACK-to-next-write guard after either SF or SC. SF can request
  a larger guard. SC uses the previously tested shared exchange timeout
  semantics (subscription, guard and optional marker precede exchange timeout).
- Integer conversion, SC0 -> SF400, restore SF -> SC, watchdog and independent
  STOP behavior are unchanged. Generation invalidation prevents stale control
  operations from succeeding; writes already submitted cannot be retracted.
- SF and SC retain separate records and timing evidence; new metadata uses
  live_transport=shared_persistent_sf_sc. Historical A/B readers accept the
  original experimental_shared_sf_sc boolean for archived evidence only.

## Operation and regression

Run all no-hardware tests before an armed bench acquisition:

    python -m unittest discover -s tests -v

For the direct controller CLI, --arm requires --dummy-load-confirmed.
The integrated recorder already requires --run --arm --dummy-load-confirmed
and propagates that confirmation. No experimental transport flag is needed.
Verify physical shutdown independently of software STOP or ACK.
