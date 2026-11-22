#include "diagnostic_telemetry.h"

namespace
{
void writeU32BE(uint8_t *out, uint32_t value)
{
  out[0] = (uint8_t)(value >> 24);
  out[1] = (uint8_t)(value >> 16);
  out[2] = (uint8_t)(value >> 8);
  out[3] = (uint8_t)value;
}
}

size_t encodeDiagnosticPayload(const DiagnosticSnapshot &snapshot,
                               uint8_t *output, size_t capacity)
{
  if (output == nullptr || capacity < DIAGNOSTIC_PAYLOAD_SIZE)
    return 0;

  output[0] = DIAGNOSTIC_PAYLOAD_VERSION;
  output[1] = (uint8_t)DIAGNOSTIC_COUNTER_COUNT;
  writeU32BE(output + 2, snapshot.sequence);
  writeU32BE(output + 6, snapshot.uptime_ms);
  for (size_t i = 0; i < DIAGNOSTIC_COUNTER_COUNT; ++i)
    writeU32BE(output + 10 + 4 * i, snapshot.counters[i]);

  return DIAGNOSTIC_PAYLOAD_SIZE;
}
