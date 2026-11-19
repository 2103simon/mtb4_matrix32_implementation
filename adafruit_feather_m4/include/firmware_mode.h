#pragma once

// Single mode selection shared by the firmware entry point and encoder.
// Binary is the default; ASCII remains available for development diagnostics.
#define BINARY_MODE 0
#define ASCII_MODE 1
#define COMM_MODE BINARY_MODE

// Build-time output selection: 1 = one tactile/rate snapshot per sample;
// 0 = original taxel + scheduled event packets for regression experiments.
#ifndef MCU_RATE_STREAMING
#define MCU_RATE_STREAMING 1
#endif
