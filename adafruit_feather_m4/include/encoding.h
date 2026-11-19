#pragma once

#include <stdint.h>

#define NUM_TAXELS 12

// Mutable foreground-only encoder state. Commands update the same parameters
// used by the production encoding functions. CAN ISR never accesses these.
struct RAParams
{
  uint8_t prevTaxelVal[NUM_TAXELS];
  uint8_t threshold;
  uint32_t lastEventTime[NUM_TAXELS];
};

struct SAParams
{
  uint32_t maxSpikingFrequency;
  float slope;
  uint32_t lastEventTime[NUM_TAXELS];
  // Fractional Q16 millisecond phase of the last emitted SA-II event.
  // Retained across sensor acquisition cycles so 6/7 ms spacing can
  // approximate a 6.67 ms target period without duplicate timestamps.
  uint32_t fractionalPeriodPhaseQ16[NUM_TAXELS];
};

extern RAParams raParams;
extern SAParams saParams;
extern SAParams saAltParams;
extern uint32_t refPeriod;

// The encoder reads the current taxels and previous sample timestamp, and
// emits through addEvent() supplied by the owning event buffer in main.cpp.
// Only loop() calls this function, never the CAN ISR.
void mechanoreceptorEncoding(uint32_t currentTime);
