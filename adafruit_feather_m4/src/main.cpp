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
#include "ra_threshold.h"
#include "taxel_assembler.h"
#include "encoding.h"
#include "event_transport.h"
#include "diagnostic_telemetry.h"
#include "firmware_mode.h"

// chmod 777 /dev/ttyACM0
// Create a new CAN object
CANSAME5x CAN;

// Serial logging macros
#define _INFO_ Serial.print("[INFO]: ");
#define _DEBUG_ Serial.print("[DEBUG]: ");
#define _ERROR_ Serial.print("[ERROR]: ");
#define _WARN_ Serial.print("[WARNING]: ");

#define CAN_BD_RATE 1000000
#define NOLD 0xFF

// E06 experimental build option: this is an MTB4 skin-service request,
// not a Feather timer. On Feather startup, this CAN command changes the
// MTB4's live transmission-period setting (without flashing MTB4 firmware).
// Production/default builds retain the historical 10 ms request.
// Only approved test intervals are accepted to avoid arbitrary CAN values.
#ifndef E06_MTB4_PERIOD_MS
#define E06_MTB4_PERIOD_MS 10
#endif
static_assert(E06_MTB4_PERIOD_MS == 10 || E06_MTB4_PERIOD_MS == 12 ||
              E06_MTB4_PERIOD_MS == 16 || E06_MTB4_PERIOD_MS == 20,
              "E06_MTB4_PERIOD_MS must be one of 10, 12, 16, or 20 ms");

#define TAXEL_ID 0x00
#define SENS 0x00
#define CAN_FRAME_MAX_BYTES 8
#define CAN_RX_QUEUE_SIZE 32
#define CAN_RX_QUEUE_MASK (CAN_RX_QUEUE_SIZE - 1)

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
TaxelSampleAssembler taxelAssembler;

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
volatile uint32_t canRxObservedCount = 0;
volatile uint32_t canRxQueuedCount = 0;
uint32_t canRxDequeuedCount = 0;
uint32_t canRxSequenceMissingCount = 0;
uint32_t taxelSamplesCompletedCount = 0;

// Foreground-only event storage and diagnostic accounting.
// Serialized counts are NOT proof that a downstream USB host received data.
EventData *events = nullptr;
int eventCount = 0;
int maxEvents = 0;
uint32_t eventAllocationFailureCount = 0;
uint32_t eventAllocationDroppedCount = 0;
uint32_t eventFramesSerializedCount = 0;
uint32_t eventRecordsSerializedCount = 0;
uint32_t eventRecordsEnqueuedCount = 0;

// Timing
uint32_t currentTime = 0;
uint32_t lastTime = 0;

// CRC32 computation
FastCRC32 crc32;

// Frame protocol constants
#define FRAME_TYPE_TAXEL 0x02
#define FRAME_TYPE_EVENTS 0x04
#define FRAME_TYPE_TACTILE_RATES 0x05
#define FRAME_TYPE_DIAGNOSTICS 0x12
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
    // A zero threshold would divide by zero inside RaEncoding().
    // ACK the effective (clamped) value so the host can see what was applied.
    uint8_t newThreshold = applyRaThreshold(rawValue, raParams.threshold);
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

/// @brief Send a single timestamped sample with 12 raw taxels and 24 rates.
/// @details Frame type 0x05, 112 payload bytes:
/// [timestamp:uint32 BE][taxel[12]:uint8]
/// [RA-I[12]:uint32 BE, centi-Hz][SA-II[12]:uint32 BE, centi-Hz].
/// The existing writer adds length, type, and CRC32 (119 bytes total).
void transmitTactileRateSnapshot(uint32_t sampleTimeMs, uint32_t sampleDtMs)
{
  uint32_t raRateCentiHz[NUM_TAXELS] = {};
  uint32_t saRateCentiHz[NUM_TAXELS] = {};
  calculateTactileRates(taxelVal, sampleDtMs, raRateCentiHz, saRateCentiHz);

  uint8_t payload[4 + NUM_TAXELS + 8 * NUM_TAXELS] = {};
  payload[0] = (uint8_t)(sampleTimeMs >> 24);
  payload[1] = (uint8_t)(sampleTimeMs >> 16);
  payload[2] = (uint8_t)(sampleTimeMs >> 8);
  payload[3] = (uint8_t)sampleTimeMs;

  for (int id = 0; id < NUM_TAXELS; ++id)
    payload[4 + id] = taxelVal[id];

  for (int id = 0; id < NUM_TAXELS; ++id)
  {
    const size_t raOffset = 4 + NUM_TAXELS + 4 * id;
    const size_t saOffset = 4 + NUM_TAXELS + 4 * NUM_TAXELS + 4 * id;
    const uint32_t ra = raRateCentiHz[id];
    const uint32_t sa = saRateCentiHz[id];
    payload[raOffset] = (uint8_t)(ra >> 24);
    payload[raOffset + 1] = (uint8_t)(ra >> 16);
    payload[raOffset + 2] = (uint8_t)(ra >> 8);
    payload[raOffset + 3] = (uint8_t)ra;
    payload[saOffset] = (uint8_t)(sa >> 24);
    payload[saOffset + 1] = (uint8_t)(sa >> 16);
    payload[saOffset + 2] = (uint8_t)(sa >> 8);
    payload[saOffset + 3] = (uint8_t)sa;
  }
  writeFrame(FRAME_TYPE_TACTILE_RATES, payload, sizeof(payload));
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
    const int requestedCapacity = maxEvents == 0 ? 10 : maxEvents * 2;
    // Never overwrite the old pointer on allocation failure: previously
    // queued events must remain intact and can still be transmitted.
    EventData *resized = (EventData *)realloc(events, requestedCapacity * sizeof(EventData));
    if (resized == nullptr)
    {
      ++eventAllocationFailureCount;
      ++eventAllocationDroppedCount;
#if (COMM_MODE == ASCII_MODE)
      Serial.println("Event allocation failed; one event not queued");
#endif
      return;
    }
    events = resized;
    maxEvents = requestedCapacity;
  }

  // Add the event to the array
  events[eventCount].ID0 = ID0;
  events[eventCount].ID1 = ID1;
  events[eventCount].time = time;
  eventCount++;
  ++eventRecordsEnqueuedCount;
}

/// @brief Wrap serialized event payload in the existing CRC-framed 0x04 packet.
/// Each call receives at most 256 payload bytes (63 event records).
static void transmitEventPayload(const uint8_t *payload, uint16_t payloadLen)
{
  writeFrame(FRAME_TYPE_EVENTS, payload, payloadLen);
}

/// @brief Serialize all current taxel-sample events over one or more 0x04 frames.
/// @details Uses (lastTime, currentTime] as chronological origin, including
///          the ordinary uint32 millis() wrap. The timestamp/reference format
///          and host decoder are unchanged; no events are dropped at 63 records.
void sortAndSendEvents()
{
  if (eventCount <= 0)
  {
    return;
  }

  const EventBatchStats sent = sortAndEmitEventBatches(
      events, (size_t)eventCount, lastTime, transmitEventPayload);
  eventFramesSerializedCount += (uint32_t)sent.frames;
  eventRecordsSerializedCount += (uint32_t)sent.records;

  // Fail closed if a programmer error in the batcher would leave records
  // unsent. Never clear an incomplete event buffer.
  if (sent.records == (size_t)eventCount)
  {
    clearEvents();
  }
#if (COMM_MODE == ASCII_MODE)
  else
  {
    Serial.println("Event batch incomplete; queue preserved");
  }
#endif
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
  ++canRxObservedCount;

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
  ++canRxQueuedCount;
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
  ++canRxDequeuedCount;
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
bool configMTB4()
{
  // The discovery request is correct: broadcast to all iCub CAN boards.
  // Discovery replies use CAN class 0x700 and command 0xFF; periodic
  // tactile data use class 0x400 and must not be mistaken for a reply.
  constexpr uint16_t kDiscoveryRequestId = 0x70F;
  constexpr uint8_t kDiscoveryCommand = 0xFF;
  constexpr uint32_t kReplyWindowMs = 500;
  constexpr uint8_t kMaxAttempts = 3;

  MTB4 = {};

  for (uint8_t attempt = 0; attempt < kMaxAttempts; ++attempt)
  {
    CAN.beginPacket(kDiscoveryRequestId);
    CAN.write(kDiscoveryCommand);
    CAN.endPacket();

    const uint32_t startedAt = millis();
    while ((uint32_t)(millis() - startedAt) < kReplyWindowMs)
    {
      const int packetSize = CAN.parsePacket();
      if (packetSize <= 0)
      {
        delay(1);
        continue;
      }

      const uint32_t canId = CAN.packetId();
      uint8_t payload[8] = {};
      size_t received = 0;
      // Always drain a frame; bound the destination even if malformed.
      while (CAN.available())
      {
        const int value = CAN.read();
        if (value < 0)
          break;
        if (received < sizeof(payload))
          payload[received] = (uint8_t)value;
        ++received;
      }

      // 0x7<board>0 is an iCub CANLoader reply addressed to the host.
      // Reject sensor data (e.g., 0x4C0), unrelated replies and bad DLCs.
      if ((canId & 0x70F) != 0x700 ||
          ((canId >> 4) & 0x0F) == 0 ||
          (packetSize != 4 && packetSize != 5) ||
          received != (size_t)packetSize ||
          payload[0] != kDiscoveryCommand)
        continue;

      // Only a supported MTB4 family board may configure our skin stream.
      const uint8_t boardType = payload[1];
      if (boardType != 11 && boardType != 16 && boardType != 20)
        continue;

      // An application reply has five bytes; a bootloader reply has
      // four and must never be used to start the tactile data stream.
      if (packetSize == 4)
      {
        _WARN_ Serial.println("MTB4 is in bootloader mode; cannot start skin stream.");
        systemStatus = WARN;
        LED(_YELLOW);
        return false;
      }

      MTB4.info.ID = (uint8_t)((canId >> 4) & 0x0F);
      for (size_t i = 0; i < 5 && i + 1 < sizeof(MTB4.bytes); ++i)
        MTB4.bytes[i + 1] = payload[i];
      return true;
    }
  }

  _ERROR_ Serial.println("No valid MTB4 discovery response; skin stream not started.");
  systemStatus = ERROR;
  LED(_RED);
  return false;
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
  CAN.write((uint8_t)E06_MTB4_PERIOD_MS); // requested MTB4 period, ms
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

  // Do not configure or stream from an unverified board address.
  if (!configMTB4())
    return;

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

/// @brief Periodically publish an immutable snapshot of MCU-side counters.
/// @details Does not issue CAN requests or serial commands.  Sequence numbers
///          permit the host to detect missing status snapshots; counters are
///          cumulative since reset. Only the short ISR-owned counter read is
///          protected against interrupts; encoding/USB I/O remain foreground.
void maybeTransmitDiagnostics()
{
  static uint32_t lastDiagnosticMs = 0;
  static uint32_t nextDiagnosticSequence = 0;
  static bool started = false;
  const uint32_t now = millis();
  if (!started)
  {
    lastDiagnosticMs = now;
    started = true;
    return;
  }
  if ((uint32_t)(now - lastDiagnosticMs) < 10000u)
    return;

  lastDiagnosticMs = now;
  DiagnosticSnapshot snapshot = {};
  snapshot.sequence = nextDiagnosticSequence++;
  snapshot.uptime_ms = now;

  noInterrupts();
  snapshot.counters[CAN_RX_OBSERVED] = canRxObservedCount;
  snapshot.counters[CAN_RX_QUEUED] = canRxQueuedCount;
  snapshot.counters[CAN_RX_MALFORMED] = canRxMalformedCount;
  snapshot.counters[CAN_RX_QUEUE_OVERFLOW] = canRxOverflowCount;
  interrupts();

  snapshot.counters[CAN_RX_DEQUEUED] = canRxDequeuedCount;
  snapshot.counters[CAN_RX_SEQUENCE_MISSING] = canRxSequenceMissingCount;
  snapshot.counters[TAXEL_SAMPLES_COMPLETED] = taxelSamplesCompletedCount;
  snapshot.counters[EVENT_RECORDS_ENQUEUED] = eventRecordsEnqueuedCount;
  snapshot.counters[EVENT_RECORDS_SERIALIZED] = eventRecordsSerializedCount;
  snapshot.counters[EVENT_FRAMES_SERIALIZED] = eventFramesSerializedCount;
  snapshot.counters[EVENT_ALLOCATION_FAILURES] = eventAllocationFailureCount;
  snapshot.counters[EVENT_RECORDS_DROPPED] = eventAllocationDroppedCount;

  uint8_t payload[DIAGNOSTIC_PAYLOAD_SIZE] = {};
  if (encodeDiagnosticPayload(snapshot, payload, sizeof(payload)) ==
      sizeof(payload))
    writeFrame(FRAME_TYPE_DIAGNOSTICS, payload, sizeof(payload));
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
      canRxSequenceMissingCount += frame.sequence - lastCanSequence - 1u;
      taxelAssembler.resetPending();
    }
    lastCanSequence = frame.sequence;
    sequenceTrackingStarted = true;

#if (COMM_MODE == ASCII_MODE)
    Serial.print(frame.canId, HEX);
    Serial.println(" ");
#endif

    if (taxelAssembler.assembleTaxelSample(frame.canId, frame.payload, frame.payloadLength, taxelVal))
    {
      ++taxelSamplesCompletedCount;
      currentTime = frame.arrivalTime;
#if MCU_RATE_STREAMING
      // Initial sample establishes the RA reference; rate=0 until a valid dt.
      static bool previousSampleValid = false;
      const uint32_t sampleDtMs =
          previousSampleValid ? (uint32_t)(currentTime - lastTime) : 0;
      transmitTactileRateSnapshot(currentTime, sampleDtMs);
      previousSampleValid = true;
#else
      transmitTaxelData(currentTime);
      mechanoreceptorEncoding(currentTime);
      sortAndSendEvents();
#endif
      // Keep timing state consistent in either output mode.
      updateTime();
    }
  }

  maybeTransmitDiagnostics();
  idleUntilInterrupt();
}
