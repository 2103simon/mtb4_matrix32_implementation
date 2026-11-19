"""Native regression for real MCU rate calculator (no Feather hardware needed).

Run: python -m unittest discover -s tests -v
A C++17 compiler (g++) is needed to run this test; skipped if unavailable.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "adafruit_feather_m4"


class McuRateEncoderTest(unittest.TestCase):
    def test_unconstrained_rates(self):
        compiler = shutil.which("g++")
        if not compiler:
            self.skipTest("g++ not installed")
        program = r"""
#include "encoding.h"
#include <cassert>
#include <cstdint>

// Required by legacy encoding.cpp, although the rate path never emits events.
uint8_t taxelVal[NUM_TAXELS] = {};
uint32_t lastTime = 0;
void addEvent(uint8_t, uint8_t, uint32_t) {}

int main() {
  uint8_t values[NUM_TAXELS] = {};
  uint32_t ra[NUM_TAXELS] = {}, sa[NUM_TAXELS] = {};

  // First sample calibrates the RA reference (no synthetic start-up spike).
  values[0] = 30;
  calculateTactileRates(values, 0, ra, sa);
  assert(ra[0] == 0);
  assert(sa[0] == 3529); // 30 * 300 / 255 * 100, rounded

  // Changes below threshold accumulate against the same RA reference.
  values[0] = 32;
  calculateTactileRates(values, 10, ra, sa);
  assert(ra[0] == 0);
  values[0] = 36; // 6 counts / threshold 3 = 2 requested spikes
  calculateTactileRates(values, 10, ra, sa);
  assert(ra[0] == 20000); // 200 Hz, no refractory cap

  // Unchanged pressure gives zero transient RA rate.
  calculateTactileRates(values, 10, ra, sa);
  assert(ra[0] == 0);

  // Full-scale SA: 300 Hz at 255, without clipping to 333.3 Hz/3 ms.
  values[1] = 255;
  calculateTactileRates(values, 10, ra, sa);
  assert(sa[1] == 30000);

  // Updating legacy refractory configuration must not affect rate output.
  refPeriod = 100;
  values[0] = 51; // 15 counts / 3 = 5 requested spikes in 10 ms
  calculateTactileRates(values, 10, ra, sa);
  assert(ra[0] == 50000);

  // Zero dt cannot divide by zero; SA remains a meaningful target rate.
  values[0] = 60;
  calculateTactileRates(values, 0, ra, sa);
  assert(ra[0] == 0);
  assert(sa[0] > 0);

  // Runtime SA frequency setting remains respected without a rate cap.
  saParams.slope = 1000.f / 255.f;
  values[1] = 255;
  calculateTactileRates(values, 10, ra, sa);
  assert(sa[1] == 100000);
  return 0;
}
"""
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "check_rates.cpp"
            output = Path(td) / "check_rates"
            source.write_text(program)
            subprocess.run([
                compiler, "-std=c++17", "-Wall", "-Wextra",
                "-I", str(FIRMWARE / "include"),
                str(source), str(FIRMWARE / "src" / "encoding.cpp"),
                "-o", str(output)
            ], check=True, capture_output=True, text=True)
            subprocess.run([str(output)], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
