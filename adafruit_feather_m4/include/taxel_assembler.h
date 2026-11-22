#pragma once

#include <stdint.h>

// Foreground-only MTB4 assembler. CAN ISR copies full frames into the queue;
// loop() alone owns this state and resets it when a receive gap is detected.
class TaxelSampleAssembler
{
public:
  void resetPending() { firstTaxelFramePending = false; }

  // Accept 0x40 + 0xC0 halves with a matching CAN ID. On completion, write
  // all 12 inverted taxels in the original hardware channel order.
  bool assembleTaxelSample(uint32_t canId, const uint8_t *payload,
                           uint8_t payloadLength, uint8_t taxelVal[12]);

private:
  uint8_t pendingTaxelValues[7] = {};
  bool firstTaxelFramePending = false;
  uint32_t pendingTaxelCanId = 0;
};
