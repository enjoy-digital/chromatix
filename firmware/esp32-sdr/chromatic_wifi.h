// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Part of the Chromatic port of ESP-SDR (https://espargos.net/espsdr/, ESPARGOS: Florian Euchner).
//
// ESP-SDR Chromatic Wi-Fi sniffer: the ESP32 Wi-Fi receiver (promiscuous mode, already enabled by
// ESP-SDR) on a channel for a time, frames summarized: access points (beacons/probe responses:
// SSID, channel, security), stations (data frames: associated BSSID, probe requests: probed SSID),
// deauthentication/disassociation frames. The SDR receive setup is restored after.
//
// Command: "WSNIFF <channel 1-14> <ms 10-2000>", replies:
// - "WAP <bssid> <channel> <rssi> <security> <frames> <ssid>" (security: OPEN, WEP, WPA, WPA2,
//   WPA3, WPA2/3, ENT; ssid: printable, hidden: empty),
// - "WST <mac> <bssid or -> <rssi> <frames> <probed ssid>",
// - "WDA <source> <destination> <count>" (deauthentication/disassociation),
// - "WEND <frames> <mgmt> <data> <ctrl>".

#ifndef CHROMATIC_WIFI_H
#define CHROMATIC_WIFI_H

#include <stdbool.h>

/* Wi-Fi commands (replies on the burst serial), restore: SDR receive setup after the sniffing. */
bool chromatic_wifi_command(const char *line, void (*restore)(void));

#endif
