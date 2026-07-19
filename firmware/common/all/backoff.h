// SoC-agnostic retry backoff policy, shared by every hokku screen firmware.
//
// The *policy* (how the retry interval grows with consecutive failures) lives
// here in common/all so all screens behave identically. The *state* (the
// failure counter) stays platform-specific: the ESP32 screens keep an
// RTC-persistent `consecutive_refresh_failures` in common/esp32/state.c; the
// Bigme F7 keeps its own thread-local count. Each just calls this for the
// interval.
#pragma once

// Exponential backoff interval, in seconds, for a repeated failure.
//
// `prior_failures` is the number of consecutive failures that already occurred
// BEFORE this one (0 for the first failure). Returns `base_seconds` doubled once
// per prior failure, capped at `max_seconds`:
//   prior_failures = 0 -> base
//                    1 -> 2*base
//                    2 -> 4*base   ... capped at max_seconds.
// Overflow-safe for any `prior_failures`. Guards nonsense inputs (base < 1 -> 1;
// max < base -> base) so a caller can never get a smaller-than-base or negative
// interval.
int hokku_backoff_seconds(unsigned prior_failures, int base_seconds, int max_seconds);
