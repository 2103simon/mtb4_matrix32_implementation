#include "taxel_assembler.h"

namespace {
const uint8_t TAXEL_FIRST_FRAME_BYTES = 8;
const uint8_t TAXEL_SECOND_FRAME_MIN_BYTES = 6;
}

/// @brief Adds one CAN payload to the two-frame MTB4 taxel sample assembler.
/// @details A complete sample is accepted only when a valid 0x40 frame is
/// followed by a valid 0xC0 frame from the same CAN source. A new 0x40 frame
/// supersedes an incomplete sample, preventing halves from different samples
/// from being combined after a dropped frame.
/// @param canId CAN identifier of the received frame.
/// @param payload Copied CAN payload.
/// @param payloadLength Number of bytes copied into payload.
/// @return true when taxelVal contains one new complete sample.
bool TaxelSampleAssembler::assembleTaxelSample(uint32_t canId, const uint8_t *payload, uint8_t payloadLength, uint8_t taxelVal[12])
{
  if (payloadLength == 0)
  {
    return false;
  }

  if (payload[0] == 0x40)
  {
    if (payloadLength != TAXEL_FIRST_FRAME_BYTES)
    {
      firstTaxelFramePending = false;
      return false;
    }

    for (uint8_t i = 0; i < 7; i++)
    {
      pendingTaxelValues[i] = ~payload[i + 1];
    }
    pendingTaxelCanId = canId;
    firstTaxelFramePending = true;
    return false;
  }

  if (payload[0] == 0xC0)
  {
    bool matchesPendingFrame = firstTaxelFramePending && canId == pendingTaxelCanId;
    firstTaxelFramePending = false;

    if (!matchesPendingFrame || payloadLength < TAXEL_SECOND_FRAME_MIN_BYTES)
    {
      return false;
    }

    for (uint8_t i = 0; i < 7; i++)
    {
      taxelVal[i] = pendingTaxelValues[i];
    }
    for (uint8_t i = 0; i < 5; i++)
    {
      taxelVal[i + 7] = ~payload[i + 1];
    }
    return true;
  }

  return false;
}

