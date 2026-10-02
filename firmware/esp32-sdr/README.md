# Chromatic ESP-SDR (ESP32 firmware)

[ESP-SDR](https://github.com/ESPARGOS/esp-sdr) (GPL-3.0) for the Chromatic ESP32, with a Chromatic
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
- `esp-sdr-chromatic.patch` (GPL-3.0, ESP-SDR): hooks in the ESP32 receiver (`QSPI` capability).
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
