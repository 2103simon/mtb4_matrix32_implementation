# FalangeM4 focused architecture review (2026-11-12)

## Decision

Two modest extractions were justified; further fragmentation is not.

- **HIGH VALUE — two-frame MTB4 assembler:** separate the data-only pairing
  state from the CAN ISR and main execution loop. Native tests can exercise
  source mismatch, malformed frames, replacement, reset-after-gap, inversion
  and ordering. The ISR queue and MTB4 hardware setup remain unchanged.
- **HIGH VALUE — RA-I/SA-II encoding:** isolate the original scientific
  algorithm implementations and their parameters so host-native C++ can
  execute production code with deterministic timestamps and event sinks.
  Keep the dormant SA-II-alt implementation with the other encoders.
- **OPTIONAL — serial command/framing split:** it is coherent, but would
  add parameter-sharing, CRC and hardware/Serial dependencies; current code
  is small enough to understand and no independent test benefit yet
  outweighs the extra interfaces.
- **NOT RECOMMENDED — CAN ISR, 32-frame ring queue, idle/hardware setup or
  event batch storage/sorting:** concurrency and allocation/timing sensitivity
  make a structural move riskier than leaving these in main.cpp. K2 requires
  its own functional change and is deliberately not corrected here.

## Coupling and risk

The mutable CAN indices remain volatile and protected by existing Cortex-M4
memory barriers; queue ownership and ISR/foreground boundaries are untouched.
Taxel assembly state is foreground-only and resets after a sequence gap.
Encoding parameters now have a single definition in encoding.cpp; command
parsing uses the same fields via encoding.h. The encoder intentionally retains
direct reads of the existing taxel/timing globals and calls the original
event sink rather than introducing a dynamic callback or class hierarchy.

RA-I and SA-II arithmetic, sample window edges, refractory behavior,
event multiplicity and timestamps, alternate encoder enablement, MTB4
configuration, 12-channel order, 255-minus-raw inversion, CRC, ACK semantics,
binary packet structure, event buffer allocation, overflow behavior and
MCU/GUI protocol are not intentionally changed.

The only external-facing C++ interface is the new
`TaxelSampleAssembler::assembleTaxelSample(canId, payload, length, taxels)`
plus `resetPending()`, and the existing
`mechanoreceptorEncoding(currentTime)` function now declared in
`encoding.h`. Both are intended for the foreground firmware or
deterministic native harness, **not** the host GUI wire protocol.

## Verification and continuation

The pre-refactor commit `10b0bbfa` passed the existing GitHub Actions
device-free checks and a compile-only PlatformIO firmware build. The
additional native C++ regression sources are
`tests/firmware/test_taxel_assembler.py` and
`tests/firmware/test_encoding.py`. Their post-refactor execution, the
full suite, and a fresh PlatformIO build must be reported from actual CI or
local results; do not infer PASS from source review alone.

No hardware was flashed or operated. Experiment framework files and tests
were not modified. Integrate this branch into
`docs/mcu-omega-experiment-plan-20261111` only after operator approval.
Do not merge with `main` independently.
