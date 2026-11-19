#pragma once

#include <stddef.h>
#include <stdint.h>

// The existing 0x04 event wire format is unchanged:
// timestamp_reference:uint32 BE, then (source:uint8, channel:uint8,
// timestamp_offset:uint16 BE) repeated up to 63 times per frame.
struct EventData
{
  uint8_t ID0;
  uint8_t ID1;
  uint32_t time;
};

struct EventBatchStats
{
  size_t frames;
  size_t records;
  size_t maximumRecordsPerFrame;
};

typedef void (*EventPayloadWriter)(const uint8_t *payload, uint16_t length);

// Sort a foreground-only, per-acquisition event array using its previous
// sample time as the uint32 clock origin (including millis() rollover).
// Emit all records in sequential <=256-byte payloads; rebase the reference
// timestamp at the start of every frame. A new frame also begins if the
// uint16 timestamp offset would overflow. The caller owns the event array
// and may clear it only after stats.records == count.
//
// Unlike wire delivery acknowledgement, records here means *serialized*
// records handed to the callback; the USB/host link has no delivery ACK.
EventBatchStats sortAndEmitEventBatches(
    EventData *events, size_t count, uint32_t previousSampleTime,
    EventPayloadWriter writer);
