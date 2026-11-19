#include "event_transport.h"

#include <algorithm>

namespace
{
constexpr size_t EVENT_PAYLOAD_BYTES = 256;
constexpr size_t EVENT_REFERENCE_BYTES = 4;
constexpr size_t EVENT_RECORD_BYTES = 4;
constexpr size_t EVENT_RECORDS_PER_FRAME =
    (EVENT_PAYLOAD_BYTES - EVENT_REFERENCE_BYTES) / EVENT_RECORD_BYTES;

static_assert(EVENT_RECORDS_PER_FRAME == 63,
              "MCU event framing requires at most 63 records per payload");
static_assert(EVENT_REFERENCE_BYTES + EVENT_RECORDS_PER_FRAME *
              EVENT_RECORD_BYTES == EVENT_PAYLOAD_BYTES,
              "Packed event payload must fit the 256-byte protocol bound");
}

EventBatchStats sortAndEmitEventBatches(
    EventData *events, size_t count, uint32_t previousSampleTime,
    EventPayloadWriter writer)
{
  EventBatchStats result = {0, 0, 0};
  if (count == 0 || events == nullptr || writer == nullptr)
  {
    return result;
  }

  // An event from the current acquisition can straddle uint32 millis()
  // rollover. Comparing its unsigned distance from the previous sample
  // preserves chronological order across that wrap.
  std::sort(events, events + count,
            [previousSampleTime](const EventData &a, const EventData &b)
            {
              return (uint32_t)(a.time - previousSampleTime) <
                     (uint32_t)(b.time - previousSampleTime);
            });

  size_t next = 0;
  while (next < count)
  {
    uint8_t payload[EVENT_PAYLOAD_BYTES] = {};
    const uint32_t referenceTime = events[next].time;
    payload[0] = (uint8_t)(referenceTime >> 24);
    payload[1] = (uint8_t)(referenceTime >> 16);
    payload[2] = (uint8_t)(referenceTime >> 8);
    payload[3] = (uint8_t)referenceTime;

    uint16_t payloadLen = EVENT_REFERENCE_BYTES;
    size_t recordsInFrame = 0;
    while (next < count && recordsInFrame < EVENT_RECORDS_PER_FRAME)
    {
      const uint32_t elapsed = (uint32_t)(events[next].time - referenceTime);
      if (elapsed > 0xFFFFUL)
      {
        // uint16 offsets cannot encode a larger span: rebase to this event
        // in a fresh frame. No event is discarded.
        break;
      }
      payload[payloadLen++] = events[next].ID0;
      payload[payloadLen++] = events[next].ID1;
      payload[payloadLen++] = (uint8_t)(elapsed >> 8);
      payload[payloadLen++] = (uint8_t)elapsed;
      ++recordsInFrame;
      ++next;
    }

    // Each iteration includes the reference event and therefore makes
    // progress. The current 0x04 frame remains backward-compatible.
    writer(payload, payloadLen);
    ++result.frames;
    result.records += recordsInFrame;
    if (recordsInFrame > result.maximumRecordsPerFrame)
    {
      result.maximumRecordsPerFrame = recordsInFrame;
    }
  }
  return result;
}
