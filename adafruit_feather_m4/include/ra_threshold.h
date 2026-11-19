#pragma once

#include <stdint.h>

// RA-I event counts divide by this threshold. Zero must never be stored.
// Keep this small helper Arduino-independent so host-native tests can verify it.
inline uint8_t applyRaThreshold(uint16_t requested, uint8_t& storedThreshold)
{
  const uint8_t applied = requested == 0 ? 1
                        : requested > 255 ? 255
                        : static_cast<uint8_t>(requested);
  storedThreshold = applied;
  return applied;
}
