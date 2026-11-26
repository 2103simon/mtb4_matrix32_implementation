# Experimental shared SF/SC ACK session

This is an unqualified, opt-in bench transport, not a change to the default
controller and not evidence of physical stimulation safety. Use only the
existing isolated resistive dummy-load setup with independent physical
disconnect and operator verification. No person or electrodes attached.

## Design

- Both SF and SC use the same existing PersistentConfigAckSession and the
  same device-level persistent exchange implementation. Default SC retains
  its per-command subscription path.
- The configuration lock rejects concurrent commands. A unique positive
  application ACK is required, and failed exchanges invalidate the verified
  configuration and close the session. Generic ACKs lack command identifiers:
  an unrelated delayed ACK arriving during the next exchange cannot be
  conclusively identified by host code.
- Experimental mode conservatively applies the existing 7.5 ms minimum
  ACK-to-next-write guard after either SF or SC, rather than assuming SC
  can be sent immediately. SF's configurable larger guard remains supported.
- No changes to integer target conversion, mute/resume command ordering,
  watchdog or independent STOP writes. STOP invalidates pending control
  generations and active persistent exchanges retain teardown ownership.
  A write already handed to BLE cannot be retracted.
- SF and SC records retain separate command histories and record transport,
  subscription reuse and host timing. Experimental persistent SC uses
  the SF exchange timeout semantics (not the default SC complete-exchange
  timeout); independent shutdown supervision is still required.

## Qualification sequence

Run the synthetic session, controller and integrated recording regression
tests before any hardware trial. A mock transport cannot establish physical
ACK attribution, electrical output behavior or real BLE timing.

The integrated recorder accepts `--experimental-shared-sf-sc` and passes it
to the controller. Omitting the flag retains the default transport.
The selected mode is recorded in the capture plan and MCU settings. An armed
experimental CLI session requires explicit dummy-load confirmation.

After reviewing passing tests and verifying the isolated bench, compare
matched recordings using the same existing operator prompts, scope settings,
controller configuration and duration. Do not change firmware, current,
frequency encoding or pulse-detection rules for the comparison.

Use `experiment_analysis.analyze_ble_session_overhead` on each recording
and compare per-group transaction coverage, subscription counts, host
request-to-write and write-to-ACK distributions. Review errors, shutdown,
mute and resume behavior and approved CH1 output before drawing conclusions.
Do not treat ACK time as physical application time. Do not promote the
experimental mode or update approved reports until the results are reviewed.

Raw recordings stay in place and existing analyses are preserved.
