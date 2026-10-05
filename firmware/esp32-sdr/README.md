# Chromatic ESP-SDR (ESP32 firmware)

[ESP-SDR](https://espargos.net/espsdr/) by [ESPARGOS](https://espargos.net/) (Florian Euchner,
[esp-sdr](https://github.com/ESPARGOS/esp-sdr), GPL-3.0) for the Chromatic ESP32, with a Chromatic
transport: captures written to the FPGA PSRAM over the ESP32 -> FPGA QSPI link (the ModRetro
menu/OSD link) instead of the UART. Commands and `DATA` headers stay on the UART (protocol
unchanged), so the firmware also works as a stock ESP-SDR (PC viewers through the USB bridge of the
standard bitstream).

- `chromatic_qspi.c/h` (BSD-2-Clause): QSPI master (VSPI/SPI3, 40MHz, ModRetro transfer format:
  11-bit command, 32-bit address, 3 dummy bits, 1KB quad data per transfer, written to the PSRAM as
  one burst) and the commands:
  - `QSPI <address>`: captures (`CAP16`/`CAP20`/`CAP`) payloads written to the PSRAM at `address`
    (1KB aligned, the last transfer padded to 1KB), only the `DATA` header on the UART; `0`: UART.
  - `QSPI?`: current address.
- `chromatic_tune.c/h` (BSD-2-Clause): tuning. ESP-SDR tunes out of channel frequencies by
  calibrating the RF PLL on 2412MHz then writing the PLL divider: the VCO stays calibrated for
  2412MHz and the LO doesn't move (measured on the Chromatic). The fork loads an entry of the PHY
  PLL frequency table (85 entries: 2400-2484MHz) with the requested divider and a VCO capacitor
  code close to the result, then runs the PHY software calibration (`set_chan_freq_sw_start`):
  2130-2890MHz (VCO capacitor bank ends); below, the 5/6 LO mode (CKGEN 0x65 host 4 reg 0 bit 4,
  found by [h0m3us3r's eSpDR](https://github.com/h0m3us3r/eSpDR), qualified on the ESP32 by
  ESP-SDR): PLL at 6/5 of the frequency, selector set after the RX setup: **1775-2890MHz in 1kHz
  steps, LO within
  ~2-4kHz** (console crystal harmonics as references, see doc/SDR.md). Commands:
  - `FREQK <kHz>` (and `FREQ <MHz>`), `RANGEK?` (`RANGEK 1775000 2890000`).
  - `TUNEMODE <0|1|2>`/`TUNEMODE?`: 0: ESP-SDR tuning, 1: table tuning (default), 2: calibration
    + offset (2386-2504MHz).
  - Experiments: `TUNESW <index> <offset>` (raw calibration, index = MHz - 2400, offset in 1/1024
    MHz), `I2CR/I2CW/I2CD` (analog registers, RF PLL: block 98 host 1), `REGR/REGW` (registers),
    `FTAB <index>` (PLL table entry).
  - With `LPF 0` (ESP-SDR), 80MS/s captures show +-38MHz around the LO.
- `chromatic_capture.c/h` (BSD-2-Clause): capture timing. Commands are handled on the FreeRTOS
  tick (1ms), so the captures start on a 1ms grid of the ESP32 clock. Against a periodic signal,
  a capture then always sees the same part of the period, drifting by the clocks' ppm only (LTE:
  the PSS always ~0.8ms in, the PBCH never in the capture). Command:
  - `CAPDLY <max_us>`/`CAPDLY?`: random delay 0..max_us (<= 10000) before each capture (0: none).
- `chromatic_wifi.c/h` (BSD-2-Clause): Wi-Fi scanner on the ESP32's own radio (promiscuous mode).
  - `WSNIFF <channel 1-14> <ms>`: summarizes the frames (access points, stations, deauthentication),
    then restores the SDR receive setup.
- `chromatic_tx.c/h` (BSD-2-Clause): bounded, legitimate transmit (ESP-SDR is receive only by
  design: an arbitrary-waveform transmitter is a jamming risk). Both on documented ESP-IDF paths,
  duration capped, auto-stopped, then the receive setup is restored:
  - `WTONE <channel 1-14> <ms> [backoff]` / `WTONEB <channel 0-39> <ms> [backoff]`: single-carrier CW
    test tone (RF certification-test path, `esp_phy_wifi_tx_tone`/`esp_phy_bt_tx_tone`).
  - `WTX <channel 1-14> <ms>`: self-identifying open SoftAP beacon "ChromatiX-TX".
- `esp-sdr-chromatic.patch` (GPL-3.0, ESP-SDR): hooks in the ESP32 receiver (`QSPI CTUNE FREQK
  LO56 CAPDLY WSNIFF WTONE WTX` capabilities, LO selector after the RX setup, capture delay).
- `build.sh`: fetches ESP-SDR (pinned), applies the patch, builds for the ESP32 (needs the ESP-IDF
  pinned by ESP-SDR in `firmware-targets.json`, sourced: `. export.sh`).

The resulting firmware is GPL-3.0 (ESP-SDR).

## Flash

From the standard bitstream (its USB bridge drives the ESP32 EN/IO0):

```sh
cd build/esp-sdr/build-esp32
python -m esptool --chip esp32 -p /dev/ttyACM0 -b 460800 write-flash @flash_args
```

This replaces the ModRetro ESP32 firmware (menu/OSD, settings): back it up first
(`esptool read-flash 0 0x400000 backup.bin`, 460800 baud) and write it back to restore.

## Measured (Chromatic)

- `CAP16 16380` over QSPI: 7.3ms command to `DATA` header (PC, through the USB bridge), vs ~170ms
  over the UART at 2Mbaud.
- `firmware/sdr` (`--with-app`): 4096-sample captures in 10ms (command, capture, QSPI, cache
  invalidation, CRC check), 25.6 updates/s, no CRC errors.
