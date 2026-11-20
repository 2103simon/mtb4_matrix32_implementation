/*
 * Gray timecode marker for Arduino Nano Every (D7).
 *
 * Independent bench reference only; NOT a stimulator interface.
 * Frame = 100 ms: START high 4 ms, guard low 3 ms,
 * 16 MSB-first Gray bits (5 ms/slot; HIGH 1 ms for 1),
 * guard low 3 ms, END high 7 ms, idle low 3 ms.
 *
 * START at phase 0; data start at 7 ms; END at 90 ms.
 * 10 frames/s; full frame index logged over USB serial.
 * uint32_t micros() wraps ~71.6 min; host log includes frame index
 * to disambiguate 16-bit waveform counter wraps.
 *
 * Wire only D7 -> grounded scope CH2 probe tip and Nano GND -> scope CH2
 * ground in an isolated scope-only experiment. Never connect this ground
 * directly to stimulation electrodes.
 */
#include <Arduino.h>

static constexpr uint8_t MARKER_PIN = 7;
static constexpr uint32_t BAUD = 115200;
static constexpr uint32_t FRAME_US = 100000UL;
static constexpr uint32_t START_US = 4000UL;
static constexpr uint32_t DATA_BEGIN_US = 7000UL;
static constexpr uint32_t BIT_SLOT_US = 5000UL;
static constexpr uint32_t BIT_HIGH_US = 1000UL;
static constexpr uint32_t END_BEGIN_US = 90000UL;
static constexpr uint32_t END_US = 7000UL;
static constexpr uint16_t BITS = 16;
static constexpr uint32_t MAX_SCHEDULER_LATENESS_US = 1500UL;

static bool running = false;
static bool pinHigh = false;
static uint32_t frameOriginUs = 0;
static uint32_t frameIndex = 0;
static uint16_t frameGray = 0;
static uint32_t timingOverruns = 0;
static char input[64];
static uint8_t inputSize = 0;

static void setMarker(bool high) {
  if (high != pinHigh) {
    digitalWrite(MARKER_PIN, high ? HIGH : LOW);
    pinHigh = high;
  }
}

static uint16_t gray16(uint32_t index) {
  uint16_t binary = (uint16_t)index;
  return binary ^ (binary >> 1);
}

static void emitFrameLog(uint32_t actualEdgeUs) {
  Serial.print(F("FRAME "));
  Serial.print(frameIndex);
  Serial.print(F(" GRAY "));
  Serial.print(frameGray, HEX);
  Serial.print(F(" EDGE_US "));
  Serial.print(actualEdgeUs);
  Serial.print(F(" OVERRUNS "));
  Serial.println(timingOverruns);
}

static void stopGenerator() {
  running = false;
  setMarker(false);
}

static void startGenerator() {
  stopGenerator();
  frameIndex = 0;
  timingOverruns = 0;
  frameGray = gray16(frameIndex);
  // Defer the first frame for 50 ms to allow command ACK to transmit.
  frameOriginUs = micros() + 50000UL;
  running = true;
}

static void handleCommand(const char* line, uint32_t receivedUs) {
  // Sequence numbers are returned verbatim so the host can match ACKs.
  char op[12] = {0};
  unsigned long seq = 0;
  if (sscanf(line, "%11s %lu", op, &seq) != 2) {
    Serial.println(F("ERR 0 BAD_COMMAND"));
    return;
  }
  if (strcmp(op, "PING") == 0) {
    Serial.print(F("ACK PING ")); Serial.println(seq);
  } else if (strcmp(op, "SYNC") == 0) {
    // RX_US is sampled immediately after the command newline was read.
    // TX_US is sampled before reply formatting; serial buffering adds
    // unmeasured latency, so the host must bound rather than assume it.
    const uint32_t transmitUs = micros();
    Serial.print(F("ACK SYNC ")); Serial.print(seq);
    Serial.print(F(" RX_US ")); Serial.print(receivedUs);
    Serial.print(F(" TX_US ")); Serial.println(transmitUs);
  } else if (strcmp(op, "INFO") == 0) {
    Serial.print(F("ACK INFO ")); Serial.print(seq);
    Serial.print(F(" FW GRAY_TC_V1 PIN 7 FRAME_US "));
    Serial.print(FRAME_US);
    Serial.print(F(" BITS ")); Serial.print(BITS);
    Serial.print(F(" RUN ")); Serial.print(running ? 1 : 0);
    Serial.print(F(" NEXT ")); Serial.print(frameIndex);
    Serial.print(F(" OVERRUNS ")); Serial.println(timingOverruns);
  } else if (strcmp(op, "START") == 0) {
    startGenerator();
    Serial.print(F("ACK START ")); Serial.print(seq);
    Serial.println(F(" FW GRAY_TC_V1"));
  } else if (strcmp(op, "STOP") == 0) {
    stopGenerator();
    Serial.print(F("ACK STOP ")); Serial.println(seq);
  } else {
    Serial.print(F("ERR ")); Serial.print(seq);
    Serial.println(F(" UNKNOWN_COMMAND"));
  }
}

static void pollSerial() {
  // Limit work per loop; serial I/O jitter is exposed via OVERRUNS.
  uint8_t consumed = 0;
  while (Serial.available() && consumed++ < 16) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      const uint32_t receivedUs = micros();
      input[inputSize] = 0;
      if (inputSize != 0) handleCommand(input, receivedUs);
      inputSize = 0;
    } else if (inputSize < sizeof(input) - 1) {
      input[inputSize++] = c;
    } else {
      inputSize = 0;
      Serial.println(F("ERR 0 LINE_TOO_LONG"));
    }
  }
}

static void tickGenerator() {
  if (!running) return;
  uint32_t now = micros();
  // Signed unsigned-difference comparison also handles the initial
  // scheduled future origin and ordinary micros() wraparound.
  int32_t offset = (int32_t)(now - frameOriginUs);
  if (offset < 0) return;
  uint32_t position = (uint32_t)offset;
  if (position >= FRAME_US) {
    uint32_t skipped = position / FRAME_US;
    frameOriginUs += skipped * FRAME_US;
    frameIndex += skipped;
    frameGray = gray16(frameIndex);
    timingOverruns += skipped > 1 ? skipped - 1 : 0;
    position = (uint32_t)(now - frameOriginUs);
    setMarker(false);
  }
  bool high = false;
  if (position < START_US) {
    high = true;
  } else if (position >= DATA_BEGIN_US &&
             position < DATA_BEGIN_US + BITS * BIT_SLOT_US) {
    uint32_t bitPosition = position - DATA_BEGIN_US;
    uint8_t bitNumber = (uint8_t)(bitPosition / BIT_SLOT_US);
    uint32_t slotPhase = bitPosition % BIT_SLOT_US;
    high = slotPhase < BIT_HIGH_US &&
           ((frameGray >> (15 - bitNumber)) & 1);
  } else if (position >= END_BEGIN_US && position < END_BEGIN_US + END_US) {
    high = true;
  }
  if (high && !pinHigh && position < START_US) {
    uint32_t expected = frameOriginUs;
    if ((uint32_t)(now - expected) > MAX_SCHEDULER_LATENESS_US) {
      ++timingOverruns;
    }
    setMarker(true);
    emitFrameLog(micros());
  } else {
    setMarker(high);
  }
}

void setup() {
  pinMode(MARKER_PIN, OUTPUT);
  digitalWrite(MARKER_PIN, LOW);
  Serial.begin(BAUD);
  // Do not wait indefinitely for USB serial. The host explicitly STARTs.
}

void loop() {
  tickGenerator();
  pollSerial();
  tickGenerator();
}
