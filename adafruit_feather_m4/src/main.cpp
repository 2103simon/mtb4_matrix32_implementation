/**
 * @file main.cpp
 * @brief Real-time tactile event encoding and sensor data acquisition for finger-mounted MTB4 skin board.
 *
 * @details
 * This module implements neuromorphic event-based tactile encoding for the iCub humanoid robot's
 * tactile fingertip sensors. It reads pressure data from a 12-taxel MTB4 skin board via CAN,
 * encodes mechanoreceptor responses (RA-I, SA-II), and transmits sorted spike events over UART.
 *
 * @features
 * - Rapidly Adapting (RA-I): Threshold-based event generation with refractory period
 * - Slowly Adapting (SA-II): Frequency-modulated spike generation proportional to pressure
 * - Event sorting and transmission: Chronological spike ordering before serial transmission
 * - Framed CRC-protected binary protocol for telemetry and parameter commands
 * - Optional ASCII debug output mode during firmware development
 * - NeoPixel LED status indicator: Visual feedback for system state (OK, WARNING, ERROR)
 *
 * @author Simon F. Muller-Cleve <simon.mullercleve@iit.it>
 * @version 1.0.0
 * @date 2026-03-23
 *
 * @copyright Copyright (c) 2024 Istituto Italiano di Tecnologia (IIT)
 * @license GNU General Public License v3.0
 *
 * @hardware
 * - Adafruit Feather M4 CAN microcontroller (Adafruit)
 * - MTB4 skin board (12 taxels, CAN interface)
 *
 * @dependencies
 * - CANSAME5x.h (CAN bus communication)
 * - Adafruit_NeoPixel.h (LED control)
 *
 * @todo
 * - Implement SA-II alt encoding (currently commented out)
 * - Extend command protocol if more runtime-configurable parameters are needed
 * - Optimize dynamic memory allocation for event storage
 * - Add data logging capability
 *
 * @see https://github.com/event-driven-robotics/biointernect-edpr
 */

#include <CANSAME5x.h>
#include <Adafruit_NeoPixel.h>
#include <stdlib.h>
#include <FastCRC.h>

// chmod 777 /dev/ttyACM0
// Create a new CAN object
CANSAME5x CAN;

// Serial logging macros
#define _INFO_ Serial.print("[INFO]: ");
#define _DEBUG_ Serial.print("[DEBUG]: ");
#define _ERROR_ Serial.print("[ERROR]: ");
#define _WARN_ Serial.print("[WARNING]: ");

#define BINARY_MODE 0
#define ASCII_MODE 1
#define COMM_MODE BINARY_MODE // binary mode uses framed telemetry/command packets; ASCII mode is for debug output only
#define CAN_BD_RATE 1000000
#define NOLD 0xFF
#define TAXEL_ID 0x00
#define SENS 0x00
#define NUM_TAXELS 12
#define CAN_FRAME_MAX_BYTES 8
#define CAN_RX_QUEUE_SIZE 32
#define CAN_RX_QUEUE_MASK (CAN_RX_QUEUE_SIZE - 1)
#define TAXEL_FIRST_FRAME_BYTES 8
#define TAXEL_SECOND_FRAME_MIN_BYTES 6

static_assert((CAN_RX_QUEUE_SIZE & CAN_RX_QUEUE_MASK) == 0,
              "CAN_RX_QUEUE_SIZE must be a power of two");

// MTB4 board information union
typedef union
{
  struct
  {
    uint8_t ID;
    uint8_t REP;
    uint8_t BRD;
    uint8_t MAJ;
    uint8_t MIN;
    uint8_t AP;
  } info;
  uint8_t bytes[6];
} MTB4UNION_t;

typedef enum
{
  OK,
  WARN,
  ERROR
} Status;

// MTB4 board info
MTB4UNION_t MTB4 = {};

// System status
Status systemStatus = OK;

// NeoPixel LED
Adafruit_NeoPixel __LED(1, PIN_NEOPIXEL, NEO_GRB + NEO_KHZ800);
#define _RED Adafruit_NeoPixel::Color(255, 0, 0)
#define _GREEN Adafruit_NeoPixel::Color(0, 255, 0)
#define _BLUE Adafruit_NeoPixel::Color(0, 0, 255)
#define _YELLOW Adafruit_NeoPixel::Color(255, 255, 0)
#define _OFF Adafruit_NeoPixel::Color(0, 0, 0)

// Taxel data processing
uint8_t taxelVal[NUM_TAXELS] = {};
uint8_t pendingTaxelValues[7] = {};
bool firstTaxelFramePending = false;
uint32_t pendingTaxelCanId = 0;

// CAN receive queue. The interrupt owns head; loop() owns tail. Monotonic
// uint8_t counters allow all 32 slots to be used while keeping each shared
// index update atomic on the Cortex-M4.
struct CanRxFrame
{
  uint32_t canId;
  uint32_t arrivalTime;
  uint32_t sequence;
  uint8_t payloadLength;
  uint8_t payload[CAN_FRAME_MAX_BYTES];
};

CanRxFrame canRxQueue[CAN_RX_QUEUE_SIZE] = {};
volatile uint8_t canRxHead = 0;
volatile uint8_t canRxTail = 0;
volatile uint32_t canRxSequence = 0;
volatile uint32_t canRxOverflowCount = 0;
volatile uint32_t canRxMalformedCount = 0;

// Event structure and storage
struct EventData
{
  uint8_t ID0;
  uint8_t ID1;
  uint32_t time;
};

EventData *events = nullptr;
int eventCount = 0;
int maxEvents = 0;
uint32_t eventTime = 0;

// RA-I (Rapidly Adapting) parameters
struct RAParams
{
  uint8_t prevTaxelVal[NUM_TAXELS];
  uint8_t threshold;
  uint32_t lastEventTime[NUM_TAXELS]; // Track last event time per channel
} raParams = {
    .threshold = 3,
    .lastEventTime = {} // Initialize all to 0
};

// SA-II (Slowly Adapting) parameters
struct SAParams
{
  uint32_t maxSpikingFrequency;
  float slope;
  uint32_t lastEventTime[NUM_TAXELS];
};

SAParams saParams = {
    .maxSpikingFrequency = 300,
    .slope = 300.f / 255.f,
    .lastEventTime = {}};

SAParams saAltParams = {
    .maxSpikingFrequency = 100,
    .slope = 100.f / 255.f,
    .lastEventTime = {}};

// define refractory period in ms (minimum inter-event interval)
uint32_t refPeriod = 3;

// Timing
uint32_t currentTime = 0;
uint32_t lastTime = 0;

// CRC32 computation
FastCRC32 crc32;

// Frame protocol constants
#define FRAME_TYPE_TAXEL 0x02
#define FRAME_TYPE_EVENTS 0x04
#define FRAME_TYPE_COMMAND 0x10
#define FRAME_TYPE_ACK 0x11

// Command payload IDs (for FRAME_TYPE_COMMAND)
#define CMD_RA_THR 0x01
#define CMD_REF 0x02
#define CMD_SA_FREQ 0x03
#define CMD_GET_CONFIG 0x04

/// @brief Clears the event buffer without deallocating memory.
/// @details Resets eventCount to 0, keeping the allocated memory intact for reuse.
void clearEvents()
{
  // Simply reset eventCount to 0, leaving the memory intact
  eventCount = 0;
}

/// @brief Updates the last event time to the current event time.
void updateTime()
{
  lastTime = currentTime;
}

/// @brief Writes a framed packet: [length(2BE)] [type(1)] [payload] [crc32(4BE)]
/// @param frameType Frame type identifier (0x02=Taxel, 0x04=Events)
/// @param payload Pointer to payload data (without type or crc)
/// @param payloadLen Length of payload in bytes
void writeFrame(uint8_t frameType, const uint8_t* payload, uint16_t payloadLen)
{
  // Frame format: [length(2)] [type(1)] [payload] [crc32(4)]
  // length = 1 (type) + payloadLen
  uint16_t length = 1 + payloadLen;
  
  // Compute CRC over type + payload
  uint8_t crc_data[1 + payloadLen];
  crc_data[0] = frameType;
  if (payloadLen > 0) {
    memcpy(&crc_data[1], payload, payloadLen);
  }
  uint32_t crc = crc32.crc32(crc_data, 1 + payloadLen);
  
  // Write frame: length, type, payload, crc
  Serial.write((length >> 8) & 0xFF);
  Serial.write(length & 0xFF);
  Serial.write(frameType);
  if (payloadLen > 0) {
    Serial.write(payload, payloadLen);
  }
  Serial.write((crc >> 24) & 0xFF);
  Serial.write((crc >> 16) & 0xFF);
  Serial.write((crc >> 8) & 0xFF);
  Serial.write(crc & 0xFF);
}

/// @brief Apply a parsed binary command payload.
/// @param payload Pointer to command payload.
/// @param payloadLen Command payload length.
void applyCommandPayload(const uint8_t* payload, uint16_t payloadLen)
{
  if (payloadLen < 1)
  {
    return;
  }

  uint8_t cmdId = payload[0];
  uint8_t ackPayload[4] = {cmdId, 0x00, 0x00, 0x00};

  if (cmdId == CMD_GET_CONFIG)
  {
    ackPayload[1] = 0x01;
    ackPayload[2] = 0x00;
    ackPayload[3] = 0x00;
    writeFrame(FRAME_TYPE_ACK, ackPayload, sizeof(ackPayload));
    return;
  }

  if (payloadLen < 3)
  {
    writeFrame(FRAME_TYPE_ACK, ackPayload, sizeof(ackPayload));
    return;
  }

  uint16_t rawValue = ((uint16_t)payload[1] << 8) | payload[2];

  if (cmdId == CMD_RA_THR)
  {
    uint8_t newThreshold = (uint8_t)constrain((int)rawValue, 0, 255);
    raParams.threshold = newThreshold;
    ackPayload[1] = 0x01;
    ackPayload[2] = 0x00;
    ackPayload[3] = newThreshold;
#if (COMM_MODE == ASCII_MODE)
    Serial.print("[CONFIG] RA-I threshold set to: ");
    Serial.println(newThreshold);
#endif
  }
  else if (cmdId == CMD_REF)
  {
    uint32_t newRefPeriod = (uint32_t)constrain((int)rawValue, 1, 100);
    refPeriod = newRefPeriod;
    ackPayload[1] = 0x01;
    ackPayload[2] = (newRefPeriod >> 8) & 0xFF;
    ackPayload[3] = newRefPeriod & 0xFF;
#if (COMM_MODE == ASCII_MODE)
    Serial.print("[CONFIG] Refractory period set to: ");
    Serial.print(newRefPeriod);
    Serial.println(" ms");
#endif
  }
  else if (cmdId == CMD_SA_FREQ)
  {
    uint32_t newMaxFreq = (uint32_t)constrain((int)rawValue, 10, 1000);
    saParams.maxSpikingFrequency = newMaxFreq;
    saParams.slope = (float)newMaxFreq / 255.0f;
    ackPayload[1] = 0x01;
    ackPayload[2] = (newMaxFreq >> 8) & 0xFF;
    ackPayload[3] = newMaxFreq & 0xFF;
#if (COMM_MODE == ASCII_MODE)
    Serial.print("[CONFIG] SA-II max frequency set to: ");
    Serial.print(newMaxFreq);
    Serial.println(" Hz");
#endif
  }

  writeFrame(FRAME_TYPE_ACK, ackPayload, sizeof(ackPayload));
}

/// @brief Transmits the current time as a 32-bit value over UART.
/// @param timeToSend The time value (milliseconds) to send as 4 bytes.
void sendCurrentTime(uint32_t timeToSend)
{
  Serial.write(timeToSend >> 24);  // First byte (most significant byte)
  Serial.write(timeToSend >> 16);  // Second byte
  Serial.write(timeToSend >> 8);   // Third byte
  Serial.write(timeToSend & 0xFF); // Fourth byte (least significant byte)
}

/// @brief Transmits current taxel data with timestamp using the new framed protocol.
/// @param currentTime The timestamp (milliseconds) for this taxel data.
/// @details Frame format: [length] [type=0x02] [timestamp(4)] [12 pressures] [crc32]
void transmitTaxelData(uint32_t currentTime)
{
  uint8_t payload[16]; // 4 bytes timestamp + 12 bytes pressures
  
  // Write timestamp (big-endian)
  payload[0] = (currentTime >> 24) & 0xFF;
  payload[1] = (currentTime >> 16) & 0xFF;
  payload[2] = (currentTime >> 8) & 0xFF;
  payload[3] = currentTime & 0xFF;
  
  // Write 12 taxel pressure values
  for (int i = 0; i < 12; i++) {
    payload[4 + i] = taxelVal[i];
  }
  
  writeFrame(FRAME_TYPE_TAXEL, payload, 16);
}

/// @brief Adds an event to the dynamic event array with automatic memory reallocation.
/// @param ID0 First identifier (mechanoreceptor type: 0=RA-I, 1=SA-II, 2=SA-II alt).
/// @param ID1 Second identifier (taxel channel 0-11).
/// @param time Timestamp (milliseconds) when the event occurred.
void addEvent(uint8_t ID0, uint8_t ID1, uint32_t time)
{
  // Check if we need to reallocate memory
  if (eventCount >= maxEvents)
  {
#if (COMM_MODE == ASCII_MODE)
    Serial.print("Will extend memory to: ");
    Serial.print(maxEvents);
    Serial.println(" events.");
#endif
    maxEvents = maxEvents == 0 ? 10 : maxEvents * 2; // Start with 10 events and double as needed
    events = (EventData *)realloc(events, maxEvents * sizeof(EventData));
    if (events == nullptr)
    {
      Serial.println("Memory allocation failed!");
      return;
    }
  }

  // Add the event to the array
  events[eventCount].ID0 = ID0;
  events[eventCount].ID1 = ID1;
  events[eventCount].time = time;
  eventCount++;
}

/// @brief Comparison function for qsort to sort EventData by timestamp.
/// @param a Pointer to first EventData.
/// @param b Pointer to second EventData.
/// @return Negative if a < b, zero if a == b, positive if a > b.
int compareEvents(const void *a, const void *b)
{
  EventData *eventA = (EventData *)a;
  EventData *eventB = (EventData *)b;

  // Return comparison result directly
  return (eventA->time > eventB->time) - (eventA->time < eventB->time);
}

/// @brief Sorts all stored events by timestamp and transmits them over UART.
/// @details Events are sorted chronologically, transmitted, and then cleared from memory.
/// @brief Sorts all stored events by timestamp and transmits them using the new framed protocol.
/// @details Frame format: [length] [type=0x04] [timestamp(4)] [ID0 ID1 time_offset(2)]×N [crc32]
///          time_offset is delta from frame timestamp in milliseconds (uint16_t)
void sortAndSendEvents()
{
  if (eventCount > 0)
  {
    // Sort events by time using qsort
    qsort(events, eventCount, sizeof(EventData), compareEvents);
    
    // Get the reference timestamp (first event's timestamp)
    uint32_t referenceTime = events[0].time;
    
    // Build payload: reference timestamp + events
    // Max payload: 4 (timestamp) + 50*(1+1+2) = 204 bytes (fits comfortably)
    uint8_t payload[256];
    uint16_t payloadLen = 0;
    
    // Write reference timestamp (big-endian)
    payload[0] = (referenceTime >> 24) & 0xFF;
    payload[1] = (referenceTime >> 16) & 0xFF;
    payload[2] = (referenceTime >> 8) & 0xFF;
    payload[3] = referenceTime & 0xFF;
    payloadLen = 4;
    
    // Write each event: ID0, ID1, time_offset(2 bytes BE)
    for (int i = 0; i < eventCount; i++)
    {
      if ((size_t)(payloadLen + 4) > sizeof(payload)) {
        // Payload too large; transmit this batch and start a new one
        // For now, just skip remaining events (rare case)
        break;
      }
      
      payload[payloadLen++] = events[i].ID0;
      payload[payloadLen++] = events[i].ID1;
      
      // Time offset from reference timestamp (uint16_t, big-endian)
      uint16_t timeOffset = (uint16_t)(events[i].time - referenceTime);
      payload[payloadLen++] = (timeOffset >> 8) & 0xFF;
      payload[payloadLen++] = timeOffset & 0xFF;
    }
    
    writeFrame(FRAME_TYPE_EVENTS, payload, payloadLen);
    clearEvents();
  }
}

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
  if (taxelVal[id] > 0)
  {
    // Taxel values are uint8_t, already constrained to [0, 255].
    float local_frequ = (float)taxelVal[id] * saParams.slope;
    uint32_t dtSaEvents = applyRefractoryPeriod((uint32_t)((1.0f / local_frequ) * 1E3));
    // Ensure we respect the refPeriod
    if (dtSaEvents < refPeriod)
    {
      dtSaEvents = refPeriod;
    }
#if (COMM_MODE == ASCII_MODE)
    Serial.print("Taxel: ");
    Serial.print(id);
    Serial.print(", TaxelVal: ");
    Serial.print(taxelVal[id]);
    Serial.print(", Frequ: ");
    Serial.print(local_frequ);
    Serial.print("Hz, SA dt: ");
    Serial.print(dtSaEvents);
    Serial.print("ms, dt sample: ");
    Serial.print(currentTime - lastTime);
    Serial.print("ms");
    Serial.print("ms, last event at: ");
    Serial.print(saParams.lastEventTime[id]);
    Serial.println("ms");
#endif

    // One-sided window policy: exclude lastTime, allow currentTime.
    uint32_t first_allowed = max(lastTime + 1, saParams.lastEventTime[id] + dtSaEvents);
    if (first_allowed > currentTime)
    {
#if (COMM_MODE == ASCII_MODE)
      Serial.println("Event to add would be in the future to respect the frequ. No event added.");
#endif
      return;
    }

    uint32_t nb_events = ((currentTime - first_allowed) / dtSaEvents) + 1;
    for (uint32_t event_counter = 0; event_counter < nb_events; event_counter++)
    {
      uint32_t reverse_idx = nb_events - 1 - event_counter;
      eventTime = currentTime - reverse_idx * dtSaEvents;
#if (COMM_MODE == ASCII_MODE)
      Serial.print("SA spike time: ");
      Serial.print(eventTime);
      Serial.println("ms");
#endif
      addEvent(1, id, eventTime); // Add event with ID 1 and the channel ID `id`
    }
    saParams.lastEventTime[id] = currentTime;
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

/// @brief Initializes the NeoPixel LED.
void LED()
{
  __LED.begin();
  __LED.show();
}

/// @brief Initializes LED and sets color.
/// @param _color Color value as Adafruit_NeoPixel::Color(R, G, B). Defaults to no initialization if omitted.
void LED(uint32_t _color)
{
  if (__LED.numPixels() == 0)
    __LED.begin(); // Initialize if needed
  __LED.clear();
  __LED.setBrightness(50);
  __LED.setPixelColor(0, _color);
  __LED.show();
}

/// @brief Copies one received CAN packet into the firmware-owned queue.
/// @details Runs from the CAN interrupt. The Adafruit CAN driver reuses its
/// internal receive buffer, so every byte must be consumed before returning.
/// No encoding, allocation, sorting, or serial I/O is performed here.
/// @param packetSize CAN payload size reported by the driver.
void onCanReceive(int packetSize)
{
  CanRxFrame frame = {};
  frame.canId = (uint32_t)CAN.packetId();
  frame.arrivalTime = millis();
  frame.sequence = canRxSequence++;

  bool payloadOverflow = false;
  while (CAN.available())
  {
    int value = CAN.read();
    if (value < 0)
    {
      break;
    }

    if (frame.payloadLength < sizeof(frame.payload))
    {
      frame.payload[frame.payloadLength++] = (uint8_t)value;
    }
    else
    {
      payloadOverflow = true;
    }
  }

  if (payloadOverflow || packetSize != frame.payloadLength)
  {
    canRxMalformedCount++;
    return;
  }

  uint8_t head = canRxHead;
  uint8_t queuedFrames = (uint8_t)(head - canRxTail);
  if (queuedFrames >= CAN_RX_QUEUE_SIZE)
  {
    canRxOverflowCount++;
    return;
  }

  canRxQueue[head & CAN_RX_QUEUE_MASK] = frame;
  __DMB(); // Publish the completed entry only after every field is written.
  canRxHead = (uint8_t)(head + 1);
}

/// @brief Removes one CAN frame from the interrupt-owned receive queue.
/// @param frame Destination for the copied queue entry.
/// @return true when a frame was available.
bool dequeueCanFrame(CanRxFrame &frame)
{
  uint8_t tail = canRxTail;
  if (tail == canRxHead)
  {
    return false;
  }

  __DMB(); // Observe the entry after observing the interrupt's head update.
  frame = canRxQueue[tail & CAN_RX_QUEUE_MASK];
  __DMB(); // Do not release this slot until its contents have been copied.
  canRxTail = (uint8_t)(tail + 1);
  return true;
}

/// @brief Enters shallow Cortex-M idle until CAN, USB, or another IRQ wakes it.
/// @details SLEEPDEEP remains clear so peripheral clocks required by CAN, USB,
/// and Arduino timing are not intentionally disabled.
void idleUntilInterrupt()
{
  noInterrupts();
  if (canRxHead == canRxTail && Serial.available() == 0)
  {
    SCB->SCR &= ~SCB_SCR_SLEEPDEEP_Msk;
    __DSB();
    __WFI();
  }
  interrupts();
}

/// @brief Discovers and configures the MTB4 sensor board via CAN.
/// @details Sends discovery command and retrieves MTB4 board information.
void configMTB4()
{
  // Discore CAN network
  CAN.beginPacket(0x70F); // mng command to everyone
  CAN.write(0xFF);        // discover
  CAN.endPacket();

  delay(500);

  int packetSize = CAN.parsePacket();

  if (packetSize)
  {
    _DEBUG_ Serial.print(CAN.packetId(), HEX);
    Serial.print(" of size ");
    Serial.println(packetSize);
    MTB4.info.ID = (CAN.packetId() & 0xf0) >> 4;
    int _c = 1;
    // received a packet
    while (CAN.available())
    {
      MTB4.bytes[_c++] = CAN.read();
    }

    if (packetSize == 4)
    {
      _WARN_ Serial.println("Your MTB4 board is in bootloader mode. Fix it!!!");

      systemStatus = WARN;
      LED(_YELLOW);
    }
  }
}

/// @brief Prints MTB4 board information to serial output.
/// @details Displays ID, board type, firmware version, and application info.
void dumpMTB4info()
{
  _INFO_
  Serial.print("MTB4 info >");
  Serial.print(" ID: ");
  Serial.print(MTB4.info.ID, HEX);
  Serial.print(" | Type: ");
  Serial.print(MTB4.info.BRD, HEX);
  Serial.print(" | V: ");
  Serial.print(MTB4.info.MAJ, HEX);
  Serial.print(".");
  Serial.print(MTB4.info.MIN, HEX);
  Serial.print(" | AP: ");
  Serial.println(MTB4.info.AP, HEX);
}

/// @brief Starts the MTB4 skin data stream via CAN.
/// @details Configures skin service, enables the specified triangle, and initiates transmission.
void startMTB4stream()
{

  uint16_t ID = 0x200 | MTB4.info.ID; // start skin service command ID

  // stop skin service
  CAN.beginPacket(ID);
  CAN.write(0x07); // SET_TXMODE
  CAN.write(0x01); // STOP
  CAN.endPacket();

  // config skin service common to every triangle
  CAN.beginPacket(ID);
  CAN.write(0x4D); // SKIN_SET_BRD_CFG
  CAN.write(0x01); // contains the compensation type
  CAN.write(0x0A); // contains the tx rate: 10 ms (100 Hz)
  // CAN.write(0xC); // contains the tx rate: 12 ms.
  // CAN.write(0x10); // contains the tx rate: 16 ms.
  CAN.write(NOLD); // contains the no-load value
  CAN.endPacket();

  delay(100);

  // disable all triangles
  CAN.beginPacket(ID);
  CAN.write(0x50); // SET_TRIANG_CFG
  CAN.write(0x00); // contains the first triangle upon which apply the command
  CAN.write(0x0f); // contains last triangle upon which apply the command
  CAN.write(SENS); // contains the shift value
  CAN.write(0x00); // contains the enable flag: 0 - off 1 - on
  CAN.write(0x00); // contain the CDC offset: 0x2200 in little endian
  CAN.write(0x00); //
  CAN.endPacket();

  delay(100);

  // enable TAXEL_ID triangle
  CAN.beginPacket(ID);
  CAN.write(0x50);     // SET_TRIANG_CFG
  CAN.write(TAXEL_ID); // contains the first triangle upon which apply the command
  CAN.write(TAXEL_ID); // contains last triangle upon which apply the command
  CAN.write(SENS);     // contains the shift value
  CAN.write(0x01);     // contains the enable flag: 0 - off 1 - on
  CAN.write(0x00);     // contain the CDC offset: 0x2200 in little endian
  CAN.write(0x00);     //
  CAN.endPacket();

  delay(100);

  // start skin service
  CAN.beginPacket(ID);
  CAN.write(0x07); // SET_TXMODE
  CAN.write(0x00); // START
  CAN.endPacket();
}

/// @brief Arduino setup function - initializes hardware and sensors.
/// @details Configures LED, serial, CAN, and MTB4.
void setup()
{
  // Initialize LED with color in one call
  LED();
  LED(_BLUE);

  // Start serial
  Serial.begin(115200);
  delay(1000);
  Serial.println("----- Finger Interface - Start! -----");
  delay(5000); // Seconds to allow a possible bootloader mode to go back into app mode on the MTB4

  // Configure CAN pins
  pinMode(PIN_CAN_STANDBY, OUTPUT);
  digitalWrite(PIN_CAN_STANDBY, false); // turn off STANDBY
  pinMode(PIN_CAN_BOOSTEN, OUTPUT);
  digitalWrite(PIN_CAN_BOOSTEN, true); // turn on booster
  delay(200);

  Serial.print("[INFO]: Starting CAN...");
  // start the CAN bus at 250 kbps
  while (!CAN.begin(CAN_BD_RATE))
  {
    _ERROR_ Serial.println("CAN not connected, check wiring and power! (retrying in 5 seconds)");
    LED(_RED);
    systemStatus = ERROR;
    // Wait a bit to see if connection is established
    delay(5000);
  }
  Serial.println("connected!");

  // Discover MTB4 and configure data
  configMTB4();

#if (COMM_MODE == ASCII_MODE)
  dumpMTB4info();
#endif

  startMTB4stream();
  // Discovery and configuration above use polling. Enable interrupt-driven
  // reception only after those replies can no longer be consumed by the ISR.
  CAN.onReceive(onCanReceive);

  Serial.println("----- Config completed -----");
  if (systemStatus == OK)
    LED(_GREEN);
}

/// @brief Processes incoming framed serial commands to update encoding parameters.
/// @details Command frame format:
///   [length(2, BE)] [type=0x10] [command_id(1) [+ value_u16(2, BE)]] [crc32(4, BE)]
/// Supported command IDs:
/// - 0x01 (CMD_RA_THR): Set RA-I threshold (0-255)
/// - 0x02 (CMD_REF): Set refractory period in ms (1-100)
/// - 0x03 (CMD_SA_FREQ): Set SA-II max frequency in Hz (10-1000)
/// - 0x04 (CMD_GET_CONFIG): Dump current configuration (ASCII debug mode only)
void processSerialCommands()
{
  static uint8_t rxBuffer[512];
  static uint16_t rxLen = 0;

  while (Serial.available() > 0)
  {
    if (rxLen >= sizeof(rxBuffer))
    {
      // Buffer overflow guard: drop oldest byte and continue.
      memmove(rxBuffer, rxBuffer + 1, rxLen - 1);
      rxLen -= 1;
    }
    rxBuffer[rxLen++] = (uint8_t)Serial.read();
  }

  while (rxLen >= 7)
  {
    uint16_t length = ((uint16_t)rxBuffer[0] << 8) | rxBuffer[1];
    if (length < 1 || length > 257)
    {
      // Invalid frame header; discard one byte and retry.
      memmove(rxBuffer, rxBuffer + 1, rxLen - 1);
      rxLen -= 1;
      continue;
    }

    uint16_t totalSize = (uint16_t)(2 + length + 4);
    if (rxLen < totalSize)
    {
      // Need more bytes for full frame.
      break;
    }

    uint8_t frameType = rxBuffer[2];
    uint8_t* payload = &rxBuffer[3];
    uint16_t payloadLen = (uint16_t)(length - 1);

    uint32_t receivedCrc =
      ((uint32_t)rxBuffer[2 + length] << 24) |
      ((uint32_t)rxBuffer[2 + length + 1] << 16) |
      ((uint32_t)rxBuffer[2 + length + 2] << 8) |
      ((uint32_t)rxBuffer[2 + length + 3]);
    uint32_t computedCrc = crc32.crc32(&rxBuffer[2], length);

    if (receivedCrc != computedCrc)
    {
      // CRC mismatch: discard one byte and resync.
      memmove(rxBuffer, rxBuffer + 1, rxLen - 1);
      rxLen -= 1;
      continue;
    }

    if (frameType == FRAME_TYPE_COMMAND)
    {
      applyCommandPayload(payload, payloadLen);
    }

    // Consume full frame.
    memmove(rxBuffer, rxBuffer + totalSize, rxLen - totalSize);
    rxLen -= totalSize;
  }
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
bool assembleTaxelSample(uint32_t canId, const uint8_t *payload, uint8_t payloadLength)
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

/// @brief Arduino main loop - processes queued CAN frames and then idles.
/// @details CAN packets are copied by onCanReceive(); all encoding and serial
/// transmission remain in normal thread context.
void loop()
{
  // Check for serial commands (non-blocking)
  processSerialCommands();

  static bool sequenceTrackingStarted = false;
  static uint32_t lastCanSequence = 0;
  CanRxFrame frame;
  while (dequeueCanFrame(frame))
  {
    if (sequenceTrackingStarted && frame.sequence != lastCanSequence + 1)
    {
      // At least one frame was dropped or malformed. Never pair a staged
      // first half with a second half across that receive gap.
      firstTaxelFramePending = false;
    }
    lastCanSequence = frame.sequence;
    sequenceTrackingStarted = true;

#if (COMM_MODE == ASCII_MODE)
    Serial.print(frame.canId, HEX);
    Serial.println(" ");
#endif

    if (assembleTaxelSample(frame.canId, frame.payload, frame.payloadLength))
    {
      currentTime = frame.arrivalTime;
      transmitTaxelData(currentTime);
      mechanoreceptorEncoding(currentTime);
      sortAndSendEvents();
      // store the new last time
      updateTime();
    }
  }

  idleUntilInterrupt();
}
