// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ESP-SDR Chromatic bounded transmit, see chromatic_tx.h.

#include <stdio.h>
#include <string.h>
#include <stdarg.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_wifi.h"
#include "esp_err.h"
#include "esp_phy_cert_test.h"

#include "burst_serial.h"
#include "chromatic_tx.h"

#define TONE_MS_MAX      2000
#define TONE_BACKOFF_MIN 8     /* 0.25dB units: never below 2dB of attenuation (not full power). */
#define TONE_BACKOFF_DEF 24    /* 6dB: a comfortable bench level, received across a room. */
#define BEACON_MS_MIN    100
#define BEACON_MS_MAX    10000

static void reply(const char *fmt, ...)
{
    char text[96];
    va_list ap;
    va_start(ap, fmt);
    int length = vsnprintf(text, sizeof(text), fmt, ap);
    va_end(ap);
    if (length > 0)
        burst_serial_send(text, length < (int)sizeof(text) ? length : (int)sizeof(text) - 1);
}

/* Single-carrier CW tone: enter the RF test state, key one carrier, stop, leave the test state. The
   carrier is a fixed test signal (the certification-test path), not an arbitrary waveform. */
static bool tone(int chan, int ms, int backoff, int ble)
{
    int chan_min = ble ? 0 : 1, chan_max = ble ? 39 : 14;
    if (chan < chan_min || chan > chan_max || ms < 10 || ms > TONE_MS_MAX) {
        reply("ERR args\n");
        return true;
    }
    if (backoff < TONE_BACKOFF_MIN)
        backoff = TONE_BACKOFF_MIN;
    if (backoff > 255)
        backoff = 255;
    esp_phy_test_start_stop(3);
    if (ble)
        esp_phy_bt_tx_tone(1, chan, backoff);
    else
        esp_phy_wifi_tx_tone(1, chan, backoff);
    vTaskDelay((ms + portTICK_PERIOD_MS - 1)/portTICK_PERIOD_MS);
    if (ble)
        esp_phy_bt_tx_tone(0, chan, backoff);
    else
        esp_phy_wifi_tx_tone(0, chan, backoff);
    esp_phy_test_start_stop(0);
    reply("WTONE OK %d %d %d\n", chan, ms, backoff);
    return true;
}

/* A self-identifying open access point, through the normal radio stack: the Wi-Fi hardware beacons
   "ChromatiX-TX" on the channel for the requested time, then the null + promiscuous receive state is
   restored. SoftAP (rather than raw frame injection): the standard, reliably discoverable way for
   the ESP32 to transmit a Wi-Fi beacon, and clearly a test network, not an impersonation. */
static bool beacon(int chan, int ms)
{
    if (chan < 1 || chan > 14 || ms < BEACON_MS_MIN || ms > BEACON_MS_MAX) {
        reply("ERR args\n");
        return true;
    }
    wifi_config_t ap = { 0 };
    static const char ssid[] = "ChromatiX-TX";
    memcpy(ap.ap.ssid, ssid, sizeof(ssid) - 1);
    ap.ap.ssid_len        = sizeof(ssid) - 1;
    ap.ap.channel         = chan;
    ap.ap.authmode        = WIFI_AUTH_OPEN;
    ap.ap.max_connection  = 1;
    ap.ap.beacon_interval = 100;

    esp_err_t err = esp_wifi_set_mode(WIFI_MODE_AP);
    if (err == ESP_OK)
        err = esp_wifi_set_config(WIFI_IF_AP, &ap);
    if (err == ESP_OK)
        vTaskDelay(pdMS_TO_TICKS(ms));            /* The hardware beacons for the window. */
    esp_wifi_set_mode(WIFI_MODE_NULL);
    esp_wifi_set_promiscuous(true);
    reply("WTX %d %d %s\n", chan, ms, esp_err_to_name(err));
    return true;
}

bool chromatic_tx_command(const char *line, void (*restore)(void))
{
    int chan, ms, backoff, got;
    bool handled = false;
    if ((got = sscanf(line, "WTONE %d %d %d", &chan, &ms, &backoff)) >= 2)
        handled = tone(chan, ms, got >= 3 ? backoff : TONE_BACKOFF_DEF, 0);
    else if ((got = sscanf(line, "WTONEB %d %d %d", &chan, &ms, &backoff)) >= 2)
        handled = tone(chan, ms, got >= 3 ? backoff : TONE_BACKOFF_DEF, 1);
    else if (sscanf(line, "WTX %d %d", &chan, &ms) == 2)
        handled = beacon(chan, ms);
    if (handled && restore)
        restore();
    return handled;
}
