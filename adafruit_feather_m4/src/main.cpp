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

// RA-I (Rapidly Adapting) parameters
struct RAParams
{
  uint8_t prevTaxelVal[NUM_TAXELS];
  uint8_t threshold;
} raParams = {
    .threshold = 3
};

// SA-II (Slowly Adapting) parameters
struct SAParams
{
  uint32_t maxSpikingFrequency;
  float slope;
};

SAParams saParams = {
    .maxSpikingFrequency = 300,
    .slope = 300.f / 255.f};

// define refractory period in ms (minimum inter-event interval)
uint32_t refPeriod = 3;

// Timing
uint32_t currentTime = 0;
uint32_t lastTime = 0;

// CRC32 computation
FastCRC32 crc32;

// Frame protocol constants
#define FRAME_TYPE_TAXEL 0x02
#define FRAME_TYPE_EVENTS 0x04 // Legacy; rate-streaming firmware does not emit this
#define FRAME_TYPE_TACTILE_RATES 0x05
#define FRAME_TYPE_COMMAND 0x10
#define FRAME_TYPE_ACK 0x11

// Command payload IDs (for FRAME_TYPE_COMMAND)
#define CMD_RA_THR 0x01
#define CMD_REF 0x02
#define CMD_SA_FREQ 0x03
#define CMD_GET_CONFIG 0x04

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
    uint8_t newThreshold = (uint8_t)constrain((int)rawValue, 1, 255);
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

/// @brief Encode a non-negative frequency as unsigned centi-Hz (0.01 Hz).
/// @details uint32 can represent up to 42,949,672.95 Hz without overflow.
///          No physiological or stimulation refractory limit is applied here.
uint32_t hzToCentiHz(float frequencyHz)
{
  if (!(frequencyHz > 0.0f))
    return 0;

  const float maxHz = 42949672.0f;
  if (frequencyHz >= maxHz)
    return UINT32_MAX;

  return (uint32_t)(frequencyHz * 100.0f + 0.5f);
}

/// @brief Return a threshold-count-derived RA-I rate, with no refractory cap.
/// @details Preserve the legacy adaptive reference: prevTaxelVal is only
///          advanced when a change reaches threshold. Each threshold crossing
///          contributes one requested spike; rate is requested spikes /
///          elapsed sample duration. Zero dt or first sample returns zero.
float calculateRaRateHz(int id, uint32_t sampleDtMs)
{
  const int change = abs((int)taxelVal[id] - (int)raParams.prevTaxelVal[id]);
  const uint8_t threshold = raParams.threshold > 0 ? raParams.threshold : 1;
  if (change < threshold)
    return 0.0f;

  const uint32_t requestedEvents = (uint32_t)(change / threshold);
  raParams.prevTaxelVal[id] = taxelVal[id];

  if (sampleDtMs == 0)
    return 0.0f;

  return (1000.0f * (float)requestedEvents) / (float)sampleDtMs;
}

/// @brief Return the SA-II requested firing rate, with no refractory cap.
float calculateSaRateHz(int id)
{
  return (float)taxelVal[id] * saParams.slope;
}

/// @brief Transmit one coherent tactile sample with both unconstrained rates.
/// @details type 0x05 payload (112 bytes):
///   timestamp_ms:uint32 BE; taxel[12]:uint8;
///   ra_rate[12]:uint32 BE in centi-Hz;
///   sa_rate[12]:uint32 BE in centi-Hz.
/// The 7-byte framing overhead makes 119 bytes per sample.
void transmitTactileRates(uint32_t timestampMs, uint32_t sampleDtMs)
{
  uint8_t payload[4 + NUM_TAXELS + 8 * NUM_TAXELS];
  payload[0] = (uint8_t)(timestampMs >> 24);
  payload[1] = (uint8_t)(timestampMs >> 16);
  payload[2] = (uint8_t)(timestampMs >> 8);
  payload[3] = (uint8_t)timestampMs;

  for (int id = 0; id < NUM_TAXELS; ++id)
    payload[4 + id] = taxelVal[id];

  for (int id = 0; id < NUM_TAXELS; ++id)
  {
    const uint32_t rate = hzToCentiHz(calculateRaRateHz(id, sampleDtMs));
    const int offset = 4 + NUM_TAXELS + 4 * id;
    payload[offset] = (uint8_t)(rate >> 24);
    payload[offset + 1] = (uint8_t)(rate >> 16);
    payload[offset + 2] = (uint8_t)(rate >> 8);
    payload[offset + 3] = (uint8_t)rate;
  }
  for (int id = 0; id < NUM_TAXELS; ++id)
  {
    const uint32_t rate = hzToCentiHz(calculateSaRateHz(id));
    const int offset = 4 + NUM_TAXELS + 4 * NUM_TAXELS + 4 * id;
    payload[offset] = (uint8_t)(rate >> 24);
    payload[offset + 1] = (uint8_t)(rate >> 16);
    payload[offset + 2] = (uint8_t)(rate >> 8);
    payload[offset + 3] = (uint8_t)rate;
  }
  writeFrame(FRAME_TYPE_TACTILE_RATES, payload, sizeof(payload));
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
      // First sample has no meaningful interval. Avoid a synthetic RA burst.
      static bool previousSampleValid = false;
      const uint32_t sampleDtMs = previousSampleValid ? (uint32_t)(currentTime - lastTime) : 0;
      transmitTactileRates(currentTime, sampleDtMs);
      previousSampleValid = true;
      updateTime();
    }
  }

  idleUntilInterrupt();
}
