#pragma once

#include <stddef.h>
#include <stdint.h>

// Versioned 10-second diagnostics over the existing USB CRC frame protocol.
// Frame type is 0x12; this is NOT a CAN identifier or a serial command.
constexpr uint8_t DIAGNOSTIC_PAYLOAD_VERSION = 1;
constexpr size_t DIAGNOSTIC_COUNTER_COUNT = 12;
constexpr size_t DIAGNOSTIC_PAYLOAD_SIZE = 2 + 4 + 4 + 4 * DIAGNOSTIC_COUNTER_COUNT;

// The index order is part of the documented v1 binary wire protocol.
// All counters are cumulative uint32, reset on physical MCU restart.
enum DiagnosticCounter : size_t
{
  CAN_RX_OBSERVED = 0,           // All receive ISR callbacks.
  CAN_RX_QUEUED = 1,             // Frames successfully copied to software queue.
  CAN_RX_MALFORMED = 2,          // ISR-rejected malformed payloads.
  CAN_RX_QUEUE_OVERFLOW = 3,     // ISR-dropped frames due to full queue.
  CAN_RX_DEQUEUED = 4,           // Frames consumed by foreground.
  CAN_RX_SEQUENCE_MISSING = 5,   // Gaps in ISR sequence observed by foreground.
  TAXEL_SAMPLES_COMPLETED = 6,   // Complete matching 0x40/0xC0 samples.
  EVENT_RECORDS_ENQUEUED = 7,    // Events successfully appended to RAM queue.
  EVENT_RECORDS_SERIALIZED = 8,  // Event records passed to USB frame writer.
  EVENT_FRAMES_SERIALIZED = 9,   // Event frames passed to USB frame writer.
  EVENT_ALLOCATION_FAILURES = 10,
  EVENT_RECORDS_DROPPED = 11,
};

struct DiagnosticSnapshot
{
  uint32_t sequence;
  uint32_t uptime_ms;
  uint32_t counters[DIAGNOSTIC_COUNTER_COUNT];
};

// Big-endian wire layout:
// [version u8=1][counter_count u8=12][sequence u32][uptime_ms u32]
// [12 counters, each u32]. Return 0 when capacity is insufficient.
size_t encodeDiagnosticPayload(const DiagnosticSnapshot &snapshot,
                               uint8_t *output, size_t capacity);
