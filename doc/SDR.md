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

## Next phases

1. **CPU application SoC** (`--with-app`, ported from the `doom` branch): VexRiscv at 67MHz, PSRAM
   main RAM, tear-free PSRAM framebuffer, PCM audio, buttons, plus a CPU UART to the ESP32.
2. **I/Q into the FPGA**: (a) ESP-SDR commands and captures/snapshot spectra over the ESP32 UART,
   (b) ESP-SDR fork writing captures over QSPI into a PSRAM ring (MB/s).
3. **SDR application** on the console: FFT, spectrum + waterfall, Wi-Fi/BLE channel markers, tuning/
   span/gain on the buttons, RSSI sonification; also on the PC (SDL) with recorded I/Q.
4. **I/Q streaming to the PC** over USB 2.0 (dedicated bulk endpoint from the PSRAM ring) and a
   SoapySDR module (GNU Radio, gqrx), or ESP-SDR protocol emulation for SoapyESPSDR/ESP-WebSDR.
