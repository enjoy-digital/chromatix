# Chromatic SDR (2.4 GHz)

Goal: a portable SDR from the Chromatic, using [ESP-SDR](https://espargos.net/espsdr/)
([esp-sdr](https://github.com/ESPARGOS/esp-sdr), GPL-3.0): an undocumented ESP32 modem debug path
captures raw I/Q samples from the ESP32 radio, without extra hardware. The console adds the screen,
buttons, FPGA processing and a USB 2.0 link to the PC.

What to expect: a **2.4 GHz** receiver (Wi-Fi, Bluetooth/BLE, ISM: spectrum, waterfall, bursts), not
a wideband SDR (no broadcast FM/AM). Captures are bursts (low duty cycle), not continuous.

## Phase 0: feasibility (done)

- **ESP32**: original ESP32 (revision 3, dual core 240MHz, embedded 4MB flash, 40MHz crystal; chip ID
  0 in the flash image), supported by ESP-SDR (UART transport, GPIO1/GPIO3 = UART0).
- **Install**: prebuilt ESP-SDR image (`esp32` variant, commit 550fade, checksums from the
  [installer manifest](https://espargos.net/espsdr/app/firmware/manifest.json)) written with
  `esptool` through the ChromatiX USB CDC <-> ESP32 bridge (standard bitstream), after a full backup
  of the ModRetro firmware (identical to the 2026-09-30 dump):

  ```bash
  esptool.py --port /dev/ttyACM0 --chip esp32 --baud 460800 read_flash 0 0x400000 esp32_backup.bin
  esptool.py --port /dev/ttyACM0 --chip esp32 --baud 460800 write_flash --flash_mode dio \
      --flash_freq 40m --flash_size 2MB 0x1000 0-bootloader.bin 0x8000 1-partition-table.bin \
      0x10000 2-esp_sdr.bin
  # Restore: esptool.py ... write_flash 0 esp32_backup.bin
  ```

- **Protocol** (2 Mbaud through the bridge, DTR/RTS released: they drive the ESP32 EN/IO0):
  `CAPS SPEC SPECN SPECCAPS SPECSTAT DCT UARTBAUD RXLIMITS SERIALLEASE TUNEEXT RX40 RX16 LPFANA GAIN
  HWAGC IQ8`, gain 0-72, rates 80/40/16 MS/s, 8/10-bit I/Q, tuning 100-6000 MHz (RF front end
  for 2.4 GHz), snapshot spectra 256-2048 bins (`SPECINFO?`). `CAP16 4096 1` (40 MS/s): `DATA 4096
  <crc> 104` (104us of capture), 8KB payload.
- **Reception** through the console's ESP32 antenna: the nearby access points are all on Wi-Fi
  channel 1 (2412 MHz). Over 60 captures, channel 1 shows ~10 dB more max-hold/average energy across
  its 20 MHz than channel 11 (no access point), and Bluetooth-like bursts around 2398 MHz.

  <img src="images/sdr_ch1_ch11.png" width="800" alt="Channel 1 (access points) vs channel 11, max hold and average">

  Band sweep (40 MS/s captures, AGC: levels differ between 20 MHz segments; the narrow regularly
  spaced lines are likely internal spurs, to be characterized):

  <img src="images/sdr_sweep.png" width="800" alt="2.4 GHz band sweep with the Chromatic ESP32">

- **USB bridge limitation** (standard bitstream): at 2 Mbaud, ~1 capture in 12 loses a few bytes
  (CRC error) and the throughput is ~31KB/s: the CDC bridge FIFO (64 bytes) overflows when the host
  doesn't poll fast enough. Fine for tests (retries), not for streaming: the console design reads the
  ESP32 directly (FPGA UART, QSPI).
- **ESP32 duties** in the console ([ModRetro MCU firmware](https://github.com/ModRetro/oss-chromatic-console-mcu)):
  menu/OSD (QSPI writes to the FPGA PSRAM: 40MHz quad, 11-bit command, 32-bit address, CS GPIO5,
  CLK GPIO18, D0-D3 GPIO23/19/22/21), settings/config to the FPGA (UART1 115200, GPIO9 TX / GPIO10
  RX), power management (light sleep, wake-up on the FPGA UART), I2S (GPIO33/25/26/27). With ESP-SDR,
  these are not available (no menu/OSD); the SDR ESP32 firmware (phase 2b) keeps what the console
  needs.

## Phase 1: CPU application SoC (done)

`--with-app` (ported from the `doom` branch): VexRiscv (8KB I/D caches) at 67MHz, 7.5MB PSRAM main
RAM (16KB L2), tear-free PSRAM framebuffer, PCM audio, buttons, and a CPU UART to the ESP32 UART0
(`esp32_uart`, 2 Mbaud, 512-byte RX FIFO). Gowin build: logic 69%, BSRAM 48/56, timing met at 67MHz.
`firmware/fbtest` checks the framebuffer/buttons/audio on the hardware.

## Phase 2a: I/Q over the ESP32 UART (done)

The CPU drives ESP-SDR directly (`firmware/sdr/esp32sdr.c`: `FREQ`/`GAIN`/`CAP16`, CRC32 check,
resync on errors): 50/50 CRC-valid 4096-sample captures in a row, 122KB/s (the USB bridge losses
are gone). A 4096-sample capture takes ~67ms (41ms for the 8KB transfer at 2 Mbaud): the stock
ESP-SDR UART is limited to 2 Mbaud (`BAUD 1000000|2000000`), faster needs the ESP-SDR fork (phase 2b).

## Phase 3: SDR application (done, UART version)

`firmware/sdr` (`make BUILD_DIR=../../build`, then
`scripts/chromatic.py --serial /dev/ttyACM0 run firmware/sdr/sdr.bin --no-verify`):

- **DSP** (`dsp.c`): 512-point int32 FFT (Q15 twiddles, tables from `gen_tables.py`), per-segment DC
  removal, Hann window, 8 segments averaged per capture (4096 samples), 1/4 dB log. Checked against
  numpy (same peak bin, dB shape correlation 0.9999).
- **Display** (`lcd.c`, 160x144, 8-bit palette): header (frequency, span, gain, reference level,
  peak frequency/level, update rate), spectrum (60dB, 10dB/10MHz grid, peak hold), Wi-Fi channel
  numbers (20MHz bands of channels 1/6/11/14), BLE advertising channels 37/38/39, waterfall (heat map
  scaled from the noise floor: median of the center half, ESP32 RX filter passband).
- **Controls**: Left/Right: tune (5MHz), Up/Down: reference level (5dB), A: span (16/40MHz), B: gain
  (AGC, manual 20-70), Start: peak hold, Select: auto reference level.
- **Measured**: 9.5 updates/s (capture 67ms, DSP 30ms, draw 6ms, present 2ms), no capture errors
  after the startup resync. 80MS/s gives no wider view (the ESP32 RX filter is ~40MHz wide, its CAPS
  only list RX40/RX16), so the spans are 16 and 40MHz.

<img src="images/sdr_console.png" width="320" alt="Chromatic SDR: spectrum and waterfall around Wi-Fi channel 6">

Wi-Fi channel 6 band (2437MHz, AGC) on the console: the ~36MHz RX filter passband and the regular
narrow lines (internal spurs, also seen in phase 0) are visible in the spectrum and waterfall.

## Next phases

1. **ESP-SDR fork** (phase 2b): faster I/Q into the FPGA (QSPI to a PSRAM ring, or a faster UART),
   on-ESP32 spectrum mode, keeping the console ESP32 duties.
2. **SDR application extras**: RSSI sonification, channel occupancy view, DSP speedups (30ms/update).
3. **I/Q streaming to the PC** over USB 2.0 (dedicated bulk endpoint from the PSRAM ring) and a
   SoapySDR module (GNU Radio, gqrx), or ESP-SDR protocol emulation for SoapyESPSDR/ESP-WebSDR.
