// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// ESP-SDR Chromatic Wi-Fi scanner, see chromatic_wifi.h.

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_wifi.h"

#include "burst_serial.h"
#include "chromatic_tune.h"
#include "chromatic_wifi.h"

#define MAX_APS  32
#define MAX_STAS 48
#define MAX_DAS  8


enum {
    SEC_OPEN = 0, SEC_WEP, SEC_WPA, SEC_WPA2, SEC_WPA3, SEC_WPA23, SEC_ENT,
};

static const char *const sec_names[] = {
    "OPEN", "WEP", "WPA", "WPA2", "WPA3", "WPA2/3", "ENT",
};

struct ap {
    uint8_t  bssid[6];
    uint8_t  channel;
    int8_t   rssi;
    uint8_t  security;
    uint16_t frames;
    char     ssid[33];
};

struct sta {
    uint8_t  mac[6];
    uint8_t  bssid[6];
    uint8_t  associated;
    int8_t   rssi;
    uint16_t frames;
    char     probe[33];
};

struct da {
    uint8_t  src[6], dst[6];
    uint16_t count;
};

static struct ap  aps[MAX_APS];
static struct sta stas[MAX_STAS];
static struct da  das[MAX_DAS];
static int        naps, nstas, ndas;
static unsigned   frames, mgmt, data, ctrl;
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;

static void reply(const char *text)
{
    burst_serial_send(text, strlen(text));
}

static int unicast(const uint8_t *mac)
{
    return !(mac[0] & 1);
}

static void copy_ssid(char *dst, const uint8_t *src, int len)
{
    /* Printable characters only (the SSID is the last field on the reply line). */
    len = (len > 32) ? 32 : len;
    for (int i = 0; i < len; i++)
        dst[i] = (src[i] >= 0x20 && src[i] < 0x7f) ? src[i] : '.';
    dst[len] = 0;
}

static int security(const uint8_t *ies, int len, int privacy)
{
    /* RSN element (WPA2/WPA3/enterprise from the AKM suites), WPA vendor element, else the privacy
       bit: WEP. */
    int sec = privacy ? SEC_WEP : SEC_OPEN;
    for (int i = 0; i + 2 <= len && i + 2 + ies[i + 1] <= len; i += 2 + ies[i + 1]) {
        const uint8_t *v = &ies[i + 2];
        int l = ies[i + 1];
        if (ies[i] == 221 && l >= 4 && v[0] == 0x00 && v[1] == 0x50 && v[2] == 0xf2 && v[3] == 1 &&
            sec < SEC_WPA)
            sec = SEC_WPA;
        if (ies[i] == 48 && l >= 8) {
            int pairwise = v[6] | (v[7] << 8), k = 8 + 4*pairwise;
            int psk = 0, sae = 0, ent = 0;
            if (k + 2 <= l) {
                int akms = v[k] | (v[k + 1] << 8);
                for (int a = 0; a < akms && k + 2 + 4*a + 4 <= l; a++) {
                    int type = v[k + 2 + 4*a + 3];
                    psk |= (type == 2 || type == 6);
                    sae |= (type == 8);
                    ent |= (type == 1 || type == 5);
                }
            }
            sec = (psk && sae) ? SEC_WPA23 : sae ? SEC_WPA3 : ent ? SEC_ENT : SEC_WPA2;
        }
    }
    return sec;
}

static struct ap *ap_get(const uint8_t *bssid, int channel)
{
    for (int i = 0; i < naps; i++)
        if (!memcmp(aps[i].bssid, bssid, 6))
            return &aps[i];
    if (naps >= MAX_APS)
        return NULL;
    struct ap *a = &aps[naps++];
    memset(a, 0, sizeof(*a));
    memcpy(a->bssid, bssid, 6);
    a->rssi    = -127;
    a->channel = channel;
    return a;
}

static struct sta *sta_get(const uint8_t *mac)
{
    for (int i = 0; i < nstas; i++)
        if (!memcmp(stas[i].mac, mac, 6))
            return &stas[i];
    if (nstas >= MAX_STAS)
        return NULL;
    struct sta *s = &stas[nstas++];
    memset(s, 0, sizeof(*s));
    memcpy(s->mac, mac, 6);
    s->rssi = -127;
    return s;
}

static void note_da(const uint8_t *src, const uint8_t *dst)
{
    /* Deauth/disassoc frames aggregated by source and destination (attack indicator). */
    for (int i = 0; i < ndas; i++)
        if (!memcmp(das[i].src, src, 6) && !memcmp(das[i].dst, dst, 6)) {
            das[i].count++;
            return;
        }
    if (ndas >= MAX_DAS)
        return;
    struct da *d = &das[ndas++];
    memcpy(d->src, src, 6);
    memcpy(d->dst, dst, 6);
    d->count = 1;
}

static void management(const uint8_t *f, int len, int subtype, int rssi, int channel)
{
    const uint8_t *addr2 = &f[10], *addr3 = &f[16];
    if ((subtype == 8 || subtype == 5) && len >= 36) {
        /* Beacon/probe response: fixed fields (12 bytes after the 24-byte header), then the IEs. */
        struct ap *a = ap_get(addr3, channel);
        if (!a)
            return;
        int privacy = (f[34] | (f[35] << 8)) & 0x10;
        const uint8_t *ies = &f[36];
        int n = len - 36;
        for (int i = 0; i + 2 <= n && i + 2 + ies[i + 1] <= n; i += 2 + ies[i + 1]) {
            if (ies[i] == 0 && ies[i + 1] && !a->ssid[0])
                copy_ssid(a->ssid, &ies[i + 2], ies[i + 1]);
            if (ies[i] == 3 && ies[i + 1] == 1)
                a->channel = ies[i + 2];
        }
        a->security = security(ies, n, privacy);
        a->rssi     = (rssi > a->rssi) ? rssi : a->rssi;
        a->frames++;
    } else if (subtype == 4 && len >= 24) {
        /* Probe request: the sender is a station looking for a network (presence; the address is
           often randomized, so it does not identify a device over time). */
        if (!unicast(addr2))
            return;
        struct sta *s = sta_get(addr2);
        if (!s)
            return;
        const uint8_t *ies = &f[24];
        int n = len - 24;
        for (int i = 0; i + 2 <= n && i + 2 + ies[i + 1] <= n; i += 2 + ies[i + 1])
            if (ies[i] == 0 && ies[i + 1] && !s->probe[0])
                copy_ssid(s->probe, &ies[i + 2], ies[i + 1]);
        s->rssi = (rssi > s->rssi) ? rssi : s->rssi;
        s->frames++;
    } else if (subtype == 12 || subtype == 10) {
        /* Deauthentication (12) / disassociation (10). */
        note_da(addr2, &f[4]);
    }
}

static void data_frame(const uint8_t *f, int len, int rssi)
{
    /* Associated station: the non-BSSID transmitter linked to its AP (to/from DS bits). */
    if (len < 24)
        return;
    int tods = f[1] & 1, fromds = f[1] & 2;
    const uint8_t *bssid, *sta_mac;
    if (tods && !fromds) {
        bssid   = &f[4];
        sta_mac = &f[10];
    } else if (!tods && fromds) {
        bssid   = &f[10];
        sta_mac = &f[4];
    } else
        return;
    if (!unicast(sta_mac))
        return;
    struct sta *s = sta_get(sta_mac);
    if (!s)
        return;
    memcpy(s->bssid, bssid, 6);
    s->associated = 1;
    s->rssi = (rssi > s->rssi) ? rssi : s->rssi;
    s->frames++;
}

static void sniffer_cb(void *buf, wifi_promiscuous_pkt_type_t type)
{
    const wifi_promiscuous_pkt_t *p = buf;
    const uint8_t *f = p->payload;
    int len = p->rx_ctrl.sig_len;
    int rssi = p->rx_ctrl.rssi;
    int channel = p->rx_ctrl.channel;
    if (len < 10)
        return;
    int ftype = (f[0] >> 2) & 3, subtype = (f[0] >> 4) & 15;
    taskENTER_CRITICAL(&lock);
    frames++;
    if (ftype == 0) {
        mgmt++;
        management(f, len, subtype, rssi, channel);
    } else if (ftype == 2) {
        data++;
        data_frame(f, len, rssi);
    } else if (ftype == 1)
        ctrl++;
    taskEXIT_CRITICAL(&lock);
}

static void mac_str(char *s, const uint8_t *m)
{
    sprintf(s, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static bool sniff(int channel, int ms)
{
    char line[96], bssid[18], mac[18];
    if (channel < 1 || channel > 14 || ms < 10 || ms > 2000) {
        reply("ERR args\n");
        return true;
    }
    /* Reset the tables, Wi-Fi reception on the channel (normal LO), collect, then stop. */
    taskENTER_CRITICAL(&lock);
    naps = nstas = ndas = 0;
    frames = mgmt = data = ctrl = 0;
    taskEXIT_CRITICAL(&lock);
    chromatic_tune_normal_lo();
    esp_wifi_set_promiscuous_rx_cb(sniffer_cb);
    esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
    vTaskDelay((ms + portTICK_PERIOD_MS - 1)/portTICK_PERIOD_MS);
    esp_wifi_set_promiscuous_rx_cb(NULL);
    for (int i = 0; i < naps; i++) {
        mac_str(bssid, aps[i].bssid);
        snprintf(line, sizeof(line), "WAP %s %d %d %s %d %s\n", bssid, aps[i].channel, aps[i].rssi,
            sec_names[aps[i].security], aps[i].frames, aps[i].ssid);
        reply(line);
    }
    for (int i = 0; i < nstas; i++) {
        mac_str(mac, stas[i].mac);
        if (stas[i].associated)
            mac_str(bssid, stas[i].bssid);
        else
            strcpy(bssid, "-");
        snprintf(line, sizeof(line), "WST %s %s %d %d %s\n", mac, bssid, stas[i].rssi,
            stas[i].frames, stas[i].probe);
        reply(line);
    }
    for (int i = 0; i < ndas; i++) {
        char src[18], dst[18];
        mac_str(src, das[i].src);
        mac_str(dst, das[i].dst);
        snprintf(line, sizeof(line), "WDA %s %s %d\n", src, dst, das[i].count);
        reply(line);
    }
    snprintf(line, sizeof(line), "WEND %u %u %u %u\n", frames, mgmt, data, ctrl);
    reply(line);
    return true;
}

bool chromatic_wifi_command(const char *line, void (*restore)(void))
{
    int channel, ms;
    if (sscanf(line, "WSNIFF %d %d", &channel, &ms) == 2) {
        bool handled = sniff(channel, ms);
        if (restore)
            restore();
        return handled;
    }
    return false;
}
