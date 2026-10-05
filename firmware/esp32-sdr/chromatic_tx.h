// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Part of the Chromatic port of ESP-SDR (https://espargos.net/espsdr/, ESPARGOS: Florian Euchner).
//
// ESP-SDR Chromatic bounded transmit (original ESP32). ESP-SDR is receive only, deliberately: an
// arbitrary-waveform transmitter on the ESP32 modem path is a jamming risk, which is why upstream
// left it out. This adds only two bounded, legitimate transmitters, both built on documented
// ESP-IDF APIs, each duration/count capped and auto-stopped, to close the loop with the Chromatic's
// own receive tools (a known emitter to find, measure and calibrate the LO against):
// - a single-carrier CW test tone (esp_phy_wifi_tx_tone / esp_phy_bt_tx_tone, the RF certification
//   test path): one controllable carrier for RF-path characterization, not an arbitrary waveform;
// - a self-identifying Wi-Fi beacon (SoftAP, the standard radio stack): a discoverable test network.
//
// Commands (replies on the burst serial; the SDR receive setup is restored after):
// - "WTONE <channel 1-14> <ms 10-2000> [backoff]"  Wi-Fi-band CW (2412 + 5*(ch-1) MHz),
// - "WTONEB <channel 0-39> <ms 10-2000> [backoff]" BLE-band CW (2402 + 2*ch MHz),
//   backoff: transmit attenuation in 0.25dB units (larger = lower power; a floor is enforced so the
//   tone is never emitted at full power), reply "WTONE OK <channel> <ms> <backoff>",
// - "WTX <channel 1-14> <ms 100-10000>" beacons an open SoftAP "ChromatiX-TX" on the channel for the
//   window, then stops, reply "WTX <channel> <ms> <status>".

#ifndef CHROMATIC_TX_H
#define CHROMATIC_TX_H

#include <stdbool.h>

/* Transmit commands (replies on the burst serial), restore: SDR receive setup after transmitting. */
bool chromatic_tx_command(const char *line, void (*restore)(void));

#endif
