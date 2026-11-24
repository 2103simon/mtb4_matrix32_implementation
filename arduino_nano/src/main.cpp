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
#if defined(GRAY_BUFFERED_V1) || defined(GRAY_HW_TCA_V1)
static constexpr uint32_t FRAME_US = 100000UL;
static constexpr bool BUFFERED = true;
static constexpr bool SLOW_DEFERRED = false;
#elif defined(GRAY_SLOW_DEFERRED)
static constexpr uint32_t FRAME_US = 200000UL;
static constexpr bool BUFFERED = false;
static constexpr bool SLOW_DEFERRED = true;
#else
static constexpr uint32_t FRAME_US = 100000UL;
static constexpr bool SLOW_DEFERRED = false;
static constexpr bool BUFFERED = false;
#endif
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
static volatile uint32_t frameIndex = 0;
static volatile uint16_t frameGray = 0;
static volatile uint32_t timingOverruns = 0;
static bool frameLogPending = false;
static uint32_t pendingFrameEdgeUs = 0;
// Separate, opt-in 10Hz buffered profile. At most 256 frames / 25.6 s.
// Each 12-byte entry is a *measured* post-GPIO timestamp, not a predicted
// frame time. Fixed storage avoids heap fragmentation on a 6KB SRAM MCU.
struct BufferedFrame {
  uint32_t counter;
  uint32_t edgeUs;
  uint32_t overruns;
};
static constexpr uint16_t BUFFER_CAPACITY = 256;
#if defined(GRAY_BUFFERED_V1) || defined(GRAY_HW_TCA_V1)
static BufferedFrame frameBuffer[BUFFER_CAPACITY];
#endif
static volatile uint16_t bufferedCount = 0;
static volatile uint32_t bufferedLost = 0;
static volatile bool buffering = false;
static char input[64];
static uint8_t inputSize = 0;

// Binary SYNC v1: A5 5A 01 TYPE SEQ_LE32 [TIMES_LE32] CRC32_LE.
// Separate from the legacy ASCII command/FRAME protocol.
static constexpr uint8_t BINARY_MAGIC0 = 0xA5;
static constexpr uint8_t BINARY_MAGIC1 = 0x5A;
static constexpr uint8_t BINARY_REQUEST_BYTES = 12;
static constexpr uint8_t BINARY_ACK_BYTES = 20;
static uint8_t binaryRx[BINARY_REQUEST_BYTES];
static uint8_t binarySize = 0;

static uint32_t crc32IEEE(const uint8_t* data, uint8_t size) {
  uint32_t crc = 0xFFFFFFFFUL;
  for (uint8_t i = 0; i < size; ++i) {
    crc ^= (uint32_t)data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ ((crc & 1) ? 0xEDB88320UL : 0UL);
    }
  }
  return crc ^ 0xFFFFFFFFUL;
}

static uint32_t readLe32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void writeLe32(uint8_t* p, uint32_t value) {
  for (uint8_t i = 0; i < 4; ++i) p[i] = (uint8_t)(value >> (8 * i));
}

// Binary response is emitted entirely by one loop iteration; text FRAME
// output is only emitted by tickGenerator outside that iteration.
static void handleBinarySync(uint32_t receivedUs) {
  if (binaryRx[2] != 1 || binaryRx[3] != 1 ||
      readLe32(binaryRx + 8) != crc32IEEE(binaryRx + 2, 6)) {
    // Silent reject: a damaged sync request must not produce a valid ACK.
    return;
  }
  uint8_t response[BINARY_ACK_BYTES] = {
      BINARY_MAGIC0, BINARY_MAGIC1, 1, 2
  };
  for (uint8_t i = 4; i < BINARY_ACK_BYTES; ++i) response[i] = 0;
  const uint32_t seq = readLe32(binaryRx + 4);
  writeLe32(response + 4, seq);
  writeLe32(response + 8, receivedUs);
  // Timestamp as close as possible before enqueueing response bytes.
  // This is NOT USB bus transmission time and does not eliminate buffering.
  writeLe32(response + 12, micros());
  writeLe32(response + 16, crc32IEEE(response + 2, 14));
  Serial.write(response, BINARY_ACK_BYTES);
}


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

static void emitFrameValues(uint32_t index, uint32_t edgeUs,
                            uint32_t overruns) {
  Serial.print(F("FRAME "));
  Serial.print(index);
  Serial.print(F(" GRAY "));
  Serial.print(gray16(index), HEX);
  Serial.print(F(" EDGE_US "));
  Serial.print(edgeUs);
  Serial.print(F(" OVERRUNS "));
  Serial.println(overruns);
}

static void emitFrameLog(uint32_t actualEdgeUs) {
  emitFrameValues(frameIndex, actualEdgeUs, timingOverruns);
}

static void storeFrame(uint32_t actualEdgeUs) {
#if defined(GRAY_BUFFERED_V1) || defined(GRAY_HW_TCA_V1)
  if (!buffering) return;
  if (bufferedCount < BUFFER_CAPACITY) {
    BufferedFrame &entry = frameBuffer[bufferedCount++];
    entry.counter = frameIndex;
    entry.edgeUs = actualEdgeUs;
    entry.overruns = timingOverruns;
  } else {
    ++bufferedLost;  // Never overwrite earlier frame evidence.
  }
#else
  (void)actualEdgeUs;
#endif
}

#ifdef GRAY_HW_TCA_V1
// Nano Every PA1 = D7 = TCA0-WO1 when PORTMUX routes TCA0 to PORTA.
// Preserve the Arduino core TCA DIV64 prescaler (used to clock TCBs).
// PlatformIO nano_every defaults to 16 MHz: 250 timer ticks = 1ms.
// For an explicitly configured 20 MHz build, alternate 312 and 313
// ticks to yield a 2ms exact pair with <=1.6us per-cell phase error.
// D7 is controlled by double-buffered TCA PWM hardware; the overflow
// ISR computes ONLY the next cell's duty, not the physical edge time.
#if F_CPU == 16000000UL
static constexpr uint16_t HW_TICKS_A = 250;
static constexpr uint16_t HW_TICKS_B = 250;
static constexpr uint16_t HW_FULL_HIGH = 250;  // > PER=249.
#elif F_CPU == 20000000UL
static constexpr uint16_t HW_TICKS_A = 312;
static constexpr uint16_t HW_TICKS_B = 313;
static constexpr uint16_t HW_FULL_HIGH = 313;  // > PER=311 or 312.
#else
#error Hardware Gray TCA profile supports only 16MHz or 20MHz Nano Every
#endif
#include <avr/interrupt.h>
static volatile uint8_t hwPhase = 50;  // 50ms low before first START.
static volatile uint32_t hwFramesStarted = 0;
static volatile uint32_t hwInterruptsLate = 0;

static inline bool hwHighAtPhase(uint8_t phase) {
  if (phase < 4) return true;
  if (phase >= 7 && phase < 87) {
    uint8_t bit = (uint8_t)((phase - 7)/5);
    return ((phase - 7)%5 == 0) &&
        ((frameGray >> (15-bit)) & 1);
  }
  return phase >= 90 && phase < 97;
}

// CMP1=HW_FULL_HIGH > PER means the whole cell is
// HIGH. CMP1=0 means the whole cell is LOW. Hardware applies this
// double-buffered duty at each BOTTOM without waiting for the ISR.
ISR(TCA0_OVF_vect) {
  TCA0.SINGLE.INTFLAGS = TCA_SINGLE_OVF_bm;
  uint8_t phase = hwPhase + 1;
  if (phase == 100) phase = 0;
  hwPhase = phase;
  if (phase == 0) {
    frameIndex = hwFramesStarted++;
    frameGray = gray16(frameIndex);
    // This is sampled after the hardware-driven rising edge, in its
    // interrupt. It is NOT an input-capture timestamp or an exact
    // electrical edge measurement. The physical edge itself is HW PWM.
    const uint32_t edgeUs = micros();
    storeFrame(edgeUs);
  }

  // Capture a missed 1ms preparation deadline as an explicit warning.
  // Threshold ~60% of a cell. A late ISR would cause
  // one cell to repeat the previously buffered duty.
  if (TCA0.SINGLE.CNT > HW_TICKS_A*3/5) {
    ++hwInterruptsLate;
    ++timingOverruns;
  }
  uint8_t nextPhase = phase+1;
  if (nextPhase == 100) nextPhase = 0;
  TCA0.SINGLE.CMP1BUF = hwHighAtPhase(nextPhase) ? HW_FULL_HIGH : 0;
  // Current cell length was buffered at the previous overflow.
  // 16MHz: 250 ticks in each cell. 20MHz: alternate 312/313.
  TCA0.SINGLE.PERBUF = (phase & 1) ?
      HW_TICKS_A-1 : HW_TICKS_B-1;
}

static void hardwareStop() {
  // Do not stop or change the TCA prescaler: the Arduino Nano Every
  // core also clocks TCB peripherals from CLK_TCA. Keep it running.
  TCA0.SINGLE.INTCTRL = 0;
  TCA0.SINGLE.CTRLB = 0;  // Release peripheral ownership of PA1.
  PORTA.OUTCLR = PIN1_bm;
  PORTMUX.TCAROUTEA = PORTMUX_TCA0_PORTB_gc;
}
static void hardwareStart() {
  // Configure before enabling: pin is LOW, first 50 timer cells LOW.
  PORTA.DIRSET = PIN1_bm;
  PORTA.OUTCLR = PIN1_bm;
  TCA0.SINGLE.CTRLA = 0;
  TCA0.SINGLE.CTRLB = 0;
  TCA0.SINGLE.INTCTRL = 0;
  hwPhase = 50;
  hwFramesStarted = 0;
  hwInterruptsLate = 0;
  TCA0.SINGLE.CNT = 0;
  TCA0.SINGLE.PER = HW_TICKS_A-1;
  TCA0.SINGLE.PERBUF = HW_TICKS_B-1;
  TCA0.SINGLE.CMP1 = 0;
  TCA0.SINGLE.CMP1BUF = 0;
  PORTMUX.TCAROUTEA = PORTMUX_TCA0_PORTA_gc;
  TCA0.SINGLE.CTRLB = TCA_SINGLE_WGMODE_SINGLESLOPE_gc |
                      TCA_SINGLE_CMP1EN_bm;
  TCA0.SINGLE.INTFLAGS = TCA_SINGLE_OVF_bm;
  TCA0.SINGLE.INTCTRL = TCA_SINGLE_OVF_bm;
  TCA0.SINGLE.CTRLA = TCA_SINGLE_CLKSEL_DIV64_gc |
                      TCA_SINGLE_ENABLE_bm;
}
#endif  // GRAY_HW_TCA_V1

static void stopGenerator() {
  running = false;
  frameLogPending = false;
  buffering = false;
#ifdef GRAY_HW_TCA_V1
  hardwareStop();
#else
  setMarker(false);
#endif
}

static void startGenerator() {
  stopGenerator();
  frameIndex = 0;
  timingOverruns = 0;
  frameLogPending = false;
  frameGray = gray16(frameIndex);
  // Defer the first frame for 50 ms to allow command ACK to transmit.
#ifdef GRAY_HW_TCA_V1
  hardwareStart();
#else
  frameOriginUs = micros() + 50000UL;
#endif
  running = true;
}

static void handleCommand(const char* line, uint32_t receivedUs) {
  // Sequence numbers are returned verbatim so the host can match ACKs.
  char op[20] = {0};
  unsigned long seq = 0;
  if (sscanf(line, "%19s %lu", op, &seq) != 2) {
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
    Serial.print(F(" FRAME_MODE "));
    Serial.print(BUFFERED ?
#ifdef GRAY_HW_TCA_V1
                 F("10HZ_HW_TCA_BUFFERED") :
#else
                 F("10HZ_BUFFERED") :
#endif
                 (SLOW_DEFERRED ? F("5HZ_DEFERRED") : F("10HZ_ORIGINAL")));
    Serial.print(F(" NEXT ")); Serial.print(frameIndex);
    Serial.print(F(" OVERRUNS ")); Serial.println(timingOverruns);
  } else if (strcmp(op, "BUFFER_BEGIN") == 0 && BUFFERED) {
    if (!running || buffering) {
      Serial.print(F("ERR ")); Serial.println(seq);
    } else {
      noInterrupts();
      bufferedCount = 0;
      bufferedLost = 0;
      buffering = true;
      interrupts();
      Serial.print(F("ACK BUFFER_BEGIN ")); Serial.print(seq);
      Serial.print(F(" CAP ")); Serial.println(BUFFER_CAPACITY);
    }
  } else if (strcmp(op, "BUFFER_END") == 0 && BUFFERED) {
    if (!buffering) {
      Serial.print(F("ERR ")); Serial.println(seq);
    } else {
      buffering = false;
      Serial.print(F("ACK BUFFER_END ")); Serial.print(seq);
      Serial.print(F(" COUNT ")); Serial.print(bufferedCount);
      Serial.print(F(" LOST ")); Serial.print(bufferedLost);
      Serial.print(F(" CAP ")); Serial.println(BUFFER_CAPACITY);
    }
  } else if (strcmp(op, "BUFFER_DUMP") == 0 && BUFFERED) {
    if (buffering) {
      Serial.print(F("ERR ")); Serial.println(seq);
    } else {
#if defined(GRAY_BUFFERED_V1) || defined(GRAY_HW_TCA_V1)
      for (uint16_t i = 0; i < bufferedCount; ++i) {
        const BufferedFrame &row = frameBuffer[i];
        emitFrameValues(row.counter, row.edgeUs, row.overruns);
      }
#endif
      Serial.print(F("ACK BUFFER_DUMP ")); Serial.print(seq);
      Serial.print(F(" COUNT ")); Serial.print(bufferedCount);
      Serial.print(F(" LOST ")); Serial.println(bufferedLost);
    }
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
    const uint8_t byteValue = (uint8_t)Serial.read();
    if (binarySize != 0 || (inputSize == 0 && byteValue == BINARY_MAGIC0)) {
      if (binarySize == 0) {
        binaryRx[binarySize++] = byteValue;
      } else if (binarySize == 1 && byteValue != BINARY_MAGIC1) {
        binarySize = 0;  // Ignore corrupt binary prefix.
      } else {
        binaryRx[binarySize++] = byteValue;
        if (binarySize == BINARY_REQUEST_BYTES) {
          const uint32_t receivedUs = micros();
          handleBinarySync(receivedUs);
          binarySize = 0;
        }
      }
      continue;
    }
    char c = (char)byteValue;
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
    if (SLOW_DEFERRED && frameLogPending) {
      // A frame ended before its log could be safely flushed.
      ++timingOverruns;
      frameLogPending = false;
    }
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
    const uint32_t edgeTimestamp = micros();
    if (BUFFERED) {
      storeFrame(edgeTimestamp);
    } else if (SLOW_DEFERRED) {
      pendingFrameEdgeUs = edgeTimestamp;
      frameLogPending = true;
    } else {
      emitFrameLog(edgeTimestamp);
    }
  } else {
    setMarker(high);
  }
  if (SLOW_DEFERRED && frameLogPending &&
      position >= END_BEGIN_US + END_US) {
    // D7 is already LOW; we have 103 ms idle before next START.
    // EDGE_US remains the original post-digitalWrite timestamp, NOT
    // the later serial message transmission time.
    emitFrameLog(pendingFrameEdgeUs);
    frameLogPending = false;
  }
}

void setup() {
  pinMode(MARKER_PIN, OUTPUT);
  digitalWrite(MARKER_PIN, LOW);
  Serial.begin(BAUD);
  // Do not wait indefinitely for USB serial. The host explicitly STARTs.
}

void loop() {
#ifndef GRAY_HW_TCA_V1
  tickGenerator();
#endif
  if (!SLOW_DEFERRED || !running ||
      (int32_t)(micros() - frameOriginUs) >=
          (int32_t)(END_BEGIN_US + END_US) ) {
    // During the 97-ms encoded waveform, avoid executing serial
    // commands/SYNC; UART hardware reception remains buffered.
    pollSerial();
  }
#ifndef GRAY_HW_TCA_V1
  tickGenerator();
#endif
}
