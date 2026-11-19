#include "encoding.h"
#include "firmware_mode.h"
#include <algorithm>
#include <stdlib.h>

#if (COMM_MODE == ASCII_MODE)
#include <Arduino.h>
#endif

// Inputs and event collector are owned by main.cpp and used in foreground only.
extern uint8_t taxelVal[NUM_TAXELS];
extern uint32_t lastTime;
extern void addEvent(uint8_t ID0, uint8_t ID1, uint32_t time);

// These initial values are intentionally identical to the pre-extraction
// definitions. Command handling still updates these exact objects.
RAParams raParams = {
    .prevTaxelVal = {},
    .threshold = 3,
    .lastEventTime = {}
};

SAParams saParams = {
    .maxSpikingFrequency = 300,
    .slope = 300.f / 255.f,
    .lastEventTime = {},
    .fractionalPeriodPhaseQ16 = {}
};

SAParams saAltParams = {
    .maxSpikingFrequency = 100,
    .slope = 100.f / 255.f,
    .lastEventTime = {},
    .fractionalPeriodPhaseQ16 = {}
};

uint32_t refPeriod = 3;

// Event time was previously a global written only by encoder routines.
static uint32_t eventTime = 0;

using std::max;

/// @brief Applies refractory period constraint to inter-event time interval.
/// @param dtEvents Time interval between events (milliseconds).
/// @return The maximum of dtEvents and refPeriod to ensure minimum spacing.
inline uint32_t applyRefractoryPeriod(uint32_t dtEvents)
{
  return dtEvents < refPeriod ? refPeriod : dtEvents;
}

/// @brief Encodes rapidly Adapting (RA-I) mechanoreceptor events.
/// @param currentTime Current timestamp (milliseconds).
/// @param id Taxel channel ID (0-11).
void RaEncoding(uint32_t currentTime, int id)
{
  uint8_t abs_change = abs(taxelVal[id] - raParams.prevTaxelVal[id]);
  if (abs_change >= raParams.threshold)
  {
    // Strict threshold-crossing count.
    uint8_t requested_events = (uint8_t)(abs_change / raParams.threshold);

    // One-sided window policy: exclude lastTime, allow currentTime.
    uint32_t first_allowed = max(lastTime + 1, raParams.lastEventTime[id] + refPeriod);
    if (first_allowed > currentTime)
    {
      raParams.prevTaxelVal[id] = taxelVal[id];
      return;
    }

    uint32_t max_events = ((currentTime - first_allowed) / refPeriod) + 1;
    uint8_t nb_events = (requested_events < max_events) ? requested_events : (uint8_t)max_events;

#if (COMM_MODE == ASCII_MODE)
    Serial.print("Taxel: ");
    Serial.print(id);
    Serial.print(", TaxelVal: ");
    Serial.print(taxelVal[id]);
    Serial.print(", prevTaxelVal: ");
    Serial.print(raParams.prevTaxelVal[id]);
    Serial.print(", diffTaxel: ");
    Serial.print(abs_change);
    Serial.print(", above thr: ");
    Serial.print(abs_change >= raParams.threshold);
    Serial.print(", # events: ");
    Serial.println(nb_events);
#endif

    // Emit events backward from currentTime to avoid placing spikes at both window boundaries.
    for (uint8_t event_counter = 0; event_counter < nb_events; event_counter++)
    {
      uint8_t reverse_idx = nb_events - 1 - event_counter;
      eventTime = currentTime - ((uint32_t)reverse_idx * refPeriod);

      addEvent(0, id, eventTime);
      raParams.lastEventTime[id] = eventTime; // Update last event time for this channel

#if (COMM_MODE == ASCII_MODE)
      Serial.print("RA spike time: ");
      Serial.println(eventTime);
#endif
    }

    raParams.prevTaxelVal[id] = taxelVal[id];
  }
}

/// @brief Encodes Slow Adaptation (SA-II) mechanoreceptor events based on taxel value magnitude.
/// @param currentTime Current timestamp (milliseconds).
/// @param id Taxel channel ID (0-11).
void SaEncoding(uint32_t currentTime, int id)
{
  if (taxelVal[id] == 0)
  {
    return;  // No SA-II events without contact; retain last emitted phase.
  }

  // Current pressure selects the target rate for THIS sample window.
  // Preserve a Q16 fractional-millisecond remainder across emitted spikes.
  // Integer event timestamps naturally alternate nearby spacings (e.g. 6/7
  // ms for 150 Hz) instead of converting 6.67 ms into permanent 6-ms ticks.
  const float local_frequ = (float)taxelVal[id] * saParams.slope;
  const uint64_t phaseScale = 65536ULL;
  uint64_t periodQ16 =
      (uint64_t)(((1.0f / local_frequ) * 1000.0f) * 65536.0f);
  const uint64_t minPeriodQ16 = (uint64_t)refPeriod * phaseScale;
  if (periodQ16 < minPeriodQ16)
  {
    periodQ16 = minPeriodQ16;
  }

  // One-sided sample window (lastTime, currentTime]. Subtracting uint32_t
  // values handles the normal millis() wrap; spans >= 2^31 ms are ambiguous.
  const uint32_t windowMs = currentTime - lastTime;
  if (windowMs == 0 || windowMs >= 0x80000000UL)
  {
    return;
  }

  // At the START of this sample window, find the first lattice timestamp
  // strictly > lastTime. If intermediate spikes were due during periods of
  // no contact or missed acquisitions, skip them WITHOUT emitting a backlog.
  //
  // lastEventTime is always an ACTUALLY EMITTED event, never a fictitious
  // skipped event or the acquisition timestamp. The fractional phase carries
  // ideal sub-millisecond progress between these emitted events.
  const uint64_t elapsedAtStart =
      (uint32_t)(lastTime - saParams.lastEventTime[id]);
  const uint32_t remainderQ16 =
      saParams.fractionalPeriodPhaseQ16[id] & 0xffffUL;

  // ceil(((elapsedAtStart + 1) * Q16 - remainder) / periodQ16)
  // gives the first step with integer timestamp > lastTime. 64-bit math
  // avoids overflow even when the MCU millisecond clock has wrapped.
  const uint64_t distanceQ16 =
      (elapsedAtStart + 1ULL) * phaseScale - remainderQ16;
  const uint64_t steps = (distanceQ16 + periodQ16 - 1ULL) / periodQ16;
  uint64_t dueQ16 = (uint64_t)remainderQ16 + steps * periodQ16;

#if (COMM_MODE == ASCII_MODE)
  Serial.print("SA target Hz: ");
  Serial.print(local_frequ);
  Serial.print(", SA desired ms: ");
  Serial.println((float)periodQ16 / 65536.0f);
#endif

  while (true)
  {
    const uint64_t dueOffsetMs =
        (dueQ16 / phaseScale) - elapsedAtStart;
    if (dueOffsetMs > windowMs)
    {
      break;
    }
    eventTime = lastTime + (uint32_t)dueOffsetMs;
    addEvent(1, id, eventTime);
    saParams.lastEventTime[id] = eventTime;
    saParams.fractionalPeriodPhaseQ16[id] =
        (uint32_t)(dueQ16 % phaseScale);
#if (COMM_MODE == ASCII_MODE)
    Serial.print("SA spike time: ");
    Serial.println(eventTime);
#endif
    dueQ16 += periodQ16;
  }
}

/// @brief Encodes alternate Slow Adaptation (SA-II alt) events based on inverse taxel value.
/// @param currentTime Current timestamp (milliseconds).
/// @param id Taxel channel ID (0-11).
void SaAltEncoding(uint32_t currentTime, int id)
{
  if (taxelVal[id] < 255)
  {
    float local_frequ = (255.f - (float)taxelVal[id]) * saAltParams.slope;
    uint32_t dtSaAltEvents = applyRefractoryPeriod((uint32_t)((1.0f / local_frequ) * 1E3));
    // Ensure we respect the refPeriod
    if (dtSaAltEvents < refPeriod)
    {
      dtSaAltEvents = refPeriod;
    }
#if (COMM_MODE == ASCII_MODE)
    Serial.print("Taxel: ");
    Serial.print(id);
    Serial.print(", TaxelVal: ");
    Serial.print(taxelVal[id]);
    Serial.print(", Frequ: ");
    Serial.print(local_frequ);
    Serial.print("Hz, SA dt: ");
    Serial.print(dtSaAltEvents);
    Serial.print("ms, dt sample: ");
    Serial.print(currentTime - lastTime);
    Serial.print("ms");
    Serial.print("ms, last event at: ");
    Serial.print(saAltParams.lastEventTime[id]);
    Serial.println("ms");
#endif
    if (saAltParams.lastEventTime[id] + dtSaAltEvents > currentTime)
    {
#if (COMM_MODE == ASCII_MODE)
      Serial.println("Event to add would be in the future to respect the frequ. No event added.");
#endif
      return;
    }
    else if (saAltParams.lastEventTime[id] + dtSaAltEvents < lastTime)
    {
#if (COMM_MODE == ASCII_MODE)
      Serial.println("Event should be added even before last sample time, so we start at last sample time.");
#endif
      for (eventTime = lastTime; eventTime <= currentTime; eventTime += dtSaAltEvents)
      {
#if (COMM_MODE == ASCII_MODE)
        Serial.print("SA alt spike time: ");
        Serial.print(eventTime);
        Serial.println("ms");
#endif
        addEvent(1, id, eventTime); // Add event with ID 1 and the channel ID `id`
      }
      saAltParams.lastEventTime[id] = eventTime;
    }
    else
    {
      for (eventTime = saAltParams.lastEventTime[id] + dtSaAltEvents; eventTime <= currentTime; eventTime += dtSaAltEvents)
      {
#if (COMM_MODE == ASCII_MODE)
        Serial.print("SA alt spike time: ");
        Serial.print(eventTime);
        Serial.println("ms");
#endif
        addEvent(1, id, eventTime); // Add event with ID 1 and the channel ID `id`
      }
      saAltParams.lastEventTime[id] = eventTime;
    }
  }
}

/// @brief Processes all mechanoreceptor encodings for the current sample.
/// @param currentTime Current timestamp (milliseconds).
void mechanoreceptorEncoding(uint32_t currentTime)
{
#if (COMM_MODE == ASCII_MODE)
  Serial.print("Mechanoreceptor encoding for time: ");
  Serial.print(currentTime);
  Serial.println("ms");
#endif
  for (int id = 0; id < NUM_TAXELS; id++)
  {
    RaEncoding(currentTime, id);
    SaEncoding(currentTime, id);
    // SaAltEncoding(currentTime, id);
  }
}

