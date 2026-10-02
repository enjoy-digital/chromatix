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
  HWAGC IQ8`, gain 0-72, rates 80/40/16 MS/s, 8/10-bit I/Q, tuning 100-6000 MHz accepted (but
  only the Wi-Fi channel frequencies really tune, see the verification section), snapshot spectra
  256-2048 bins (`SPECINFO?`). `CAP16 4096 1` (40 MS/s): `DATA 4096 <crc> 104` (104us of
  capture), 8KB payload.
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
- **Controls**: Left/Right: tune (Wi-Fi channels), Up/Down: reference level (5dB), A: span
  (16/40MHz), B: gain (AGC, manual 20-70), Start: peak hold, Select: auto reference level, Menu:
  RSSI tone (PCM audio, pitch following the peak level above the noise floor in the center
  quarter of the span: tune to an emitter and hunt it down; measured over the USB audio:
  520-1120Hz following Wi-Fi bursts).
- **Measured**: 9.5 updates/s (capture 67ms, DSP 30ms, draw 6ms, present 2ms), no capture errors
  after the startup resync. 80MS/s gives no wider view (the ESP32 RX filter is ~40MHz wide, its CAPS
  only list RX40/RX16), so the spans are 16 and 40MHz.

<img src="images/sdr_console.png" width="320" alt="Chromatic SDR: spectrum and waterfall around Wi-Fi channel 6">

Wi-Fi channel 6 band (2437MHz, AGC) on the console: the ~36MHz RX filter passband and the regular
narrow lines (internal spurs, also seen in phase 0) are visible in the spectrum and waterfall.

## Phase 2b: I/Q over QSPI (done)

`firmware/esp32-sdr`: ESP-SDR (GPL-3.0, fetched and patched at build time) with a Chromatic
transport. `QSPI <address>` makes the captures go to the FPGA PSRAM over the ESP32 -> FPGA QSPI
link (the ModRetro menu/OSD link: VSPI 40MHz quad, 1KB transfers written as PSRAM bursts by the
existing gateware), only the `DATA` header on the UART. No gateware change: the CPU invalidates its
caches and reads the payload from its main RAM (CRC checked).

- `CAP16 16380` round trip: 7.3ms (vs ~170ms over the UART).
- Console app: 4096-sample captures in 10ms, **25.6 -> 27 updates/s** (DSP: int16 block scaled FFT,
  21ms per update), no CRC errors.

## Phase 4: I/Q streaming to the PC, SoapySDR (done)

The console relays the ESP-SDR protocol to the PC at the USB rate:

- **Gateware** (`chromatix/gateware/cdc_link.py`, `--with-app`): the USB CDC byte stream is
  switched by the application from the UARTBone debug bridge to an application UART (commands,
  replies) + a DMA reader (bulk data from the main RAM). A host "1200 baud touch" switches it back
  to the debug bridge (`scripts/chromatic.py` does it) for the debug session: back to the
  application 1s after the port is closed (DTR released). Build: logic 74%, BSRAM 50/56, timing
  met.
- **Firmware** (`firmware/sdr`): host command lines forwarded to the ESP32 (`BAUD`/`QSPI` answered
  locally), replies relayed; after a capture `DATA` header, the payload (already in the main RAM
  through QSPI) is sent by DMA. The relayed captures are displayed (header: `USB`), local captures
  resume 2s after the last host command.
- **Host**: `scripts/chromatic_sdr.py` (info, bench, spectrum, record) and a SoapySDR module
  (`software/SoapyChromatic`: GNU Radio, gqrx, SoapySDR Python..., CS8/CS16/CF32, 16/40MS/s, AGC
  or manual gain). The official SoapyESPSDR is ESP32-S31/Ethernet only.
- **Measured**: 73 captures/s of 16380 samples, **2.4MB/s** (1.2MS/s delivered, ~3% duty cycle at
  40MS/s), 0 errors over 833 relayed captures (27MB); stock ESP-SDR through the USB bridge: 31KB/s.
  SoapySDR: 1.0MS/s (Python), GNU Radio soapy source: 1.12MS/s.

<img src="images/sdr_host_spectrum.png" width="800" alt="Host spectrum over USB: Wi-Fi channel 1">

<img src="images/sdr_console_usb.png" width="320" alt="Console display of the relayed captures">

Host spectrum (400 relayed captures, `chromatic_sdr.py spectrum --freq 2412`): Wi-Fi channel 1
(20MHz, max hold) and the console showing the same captures (`USB`).

## Verification: tuning, spectrum orientation, console vs host

Reference signals: the console 24MHz crystal harmonics (2400/2424/2448/2472/2496MHz, narrow lines
seen at every tuning) and the Wi-Fi access point on channel 1.

- **Inverted spectrum**: with ESP-SDR's I/Q order, the crystal harmonics showed at 2*LO - f (ex:
  2424MHz at 2420/2430/2440MHz for LO = 2422/2427/2432MHz). The ESP32 spectrum is inverted: the
  samples are conjugated by the console DSP, SoapyChromatic and `chromatic_sdr.py` (the ESP-SDR
  protocol/payload is unchanged).
- **Tuning range** (stock ESP-SDR, see the extended tuning section for the fix): ESP-SDR accepts
  100-6000MHz, but on the original ESP32 only the Wi-Fi channel frequencies (2412-2472MHz in 5MHz
  steps, 2484MHz) move the LO: out of channel requests (calibration on 2412MHz then direct PLL
  offset) leave it in place, except a few MHz around 2412MHz (comb positions unchanged from 2405MHz
  down to 2300MHz, from 2490MHz up to 2600MHz, and for most frequencies between channels). ESP-SDR
  documents its extended range as not RF-validated. With stock ESP-SDR, the console steps through
  the channels and SoapyChromatic tunes the nearest channel + digital mixer (2410-2486MHz).
- **Checks after the fixes**:
  - Host (SoapyChromatic) at 2410/2412/2414.7/2425.3/2437/2439.9/2451/2463.6/2478/2484/2486MHz:
    every crystal harmonic in the span at its true frequency (2400.02, 2424.00, 2448.00, 2472.00,
    2496.00MHz).
  - Console standalone display (UVC frames, spectrum trace read per column) at 2412/2437/2462MHz:
    harmonics at 2400.1/2424.1, 2424.1/2448.1, 2448.1/2472.1MHz (0.25MHz columns).
  - Console display of relayed captures vs host computation of the console DSP on the same
    captures: same lines within one column, trace correlation 0.82.
  - Access point bursts (waterfall/burst spectra) at LO 2422MHz: 2414.8MHz center on the console,
    2415.1MHz on the host, 2414.5MHz through the host NCO (LO 2419.5MHz, ESP32 on 2417MHz): same
    absolute frequency, on the AP side (an inverted spectrum would show them at ~2429MHz).
  - gqrx 2.15.8 (Xvfb, remote control) at 2436MHz (hardware frequency 2433.12MHz): lines at
    2424.0/2424.3, 2441.1 and 2448.0MHz on its axis.

## Extended tuning: 2386-2504MHz in 1kHz steps (ESP32 fork)

ESP-SDR's out of channel tuning (2412MHz calibration + direct PLL divider) leaves the VCO capacitor
bank calibrated for 2412MHz: the LO doesn't follow. The ESP32 PHY library has a software channel
calibration (`set_chan_freq_sw_start(index = MHz - 2400, offset, ctrl)`, used for the Wi-Fi
channels by `set_channel_rfpll_freq` and by the Bluetooth PHY for its 1MHz channels), found by
disassembling `libphy.a`. `firmware/esp32-sdr` (`chromatic_tune.c`) uses it:

- **Calibration on the requested MHz** (index 0-84: 2400-2484MHz; no lock above 85) + an offset
  for the fraction. The offset is in 1/1024 MHz units but the LO moves by **1.0546x** the offset
  (measured, interpolated comb peaks).
- **Beyond, offsets from the 2400/2484MHz calibrations**, same 1.0546 scale, with a -0.243MHz step
  past a 15MHz move above 2484MHz (overlapping regions: switched at 14.9MHz). PLL lock from 2400 -
  14.7MHz to 2484 + 22MHz: 2386-2504MHz used.
- **Result**: `FREQK <kHz>` (and `FREQ`): LO within **+-2.6kHz** (~1ppm, the console/ESP32 crystals
  difference is -1.2kHz) at 18 arbitrary frequencies over 2386.3-2503.8MHz, both sides of the
  14.9MHz switch. The comb line at 2400MHz is not a reference: another source sits ~16kHz above it
  (at LO 2412MHz: -17kHz from it, -1.3kHz from 2424MHz).
- **Clients**: the console tunes 2386-2504MHz (5MHz steps, kHz from the host), SoapyChromatic and
  `chromatic_sdr.py` use `FREQK` (range from `RANGEK?`, NCO only for the sub-kHz rest); stock
  ESP-SDR: channel frequencies + NCO. Checked: SoapySDR (comb lines at their true frequencies at
  2386.7-2503.5MHz), console standalone at 2387.0/2388.5/2453.5/2502.0/2503.5MHz (UVC trace),
  gqrx at 2390.5MHz (hardware 2387.62MHz: lines at 2376.0/2400.0MHz on its axis). The console
  reference level now follows the level changes across the band (auto until Up/Down).
- **1090MHz/ADS-B**: not reachable with the ESP32 radio (LO range above, 2.4GHz antenna path).
  The ESP32 ADS-B projects use an RTL-SDR dongle on an ESP32-P4 USB host or a dedicated 1090MHz
  module; a video of an ESP32 SDR app "at 1575MHz" shows a 2.4GHz spectrum with the hardware at
  2446.5MHz (the app warns: outside its verified ranges). An external front-end (cartridge slot)
  would be needed.

## Next steps

1. **USB link**: one 512-byte USB packet was lost once in a capture payload in ~13000 relayed
   captures (5000-capture stress test with a slow consumer: no error); the clients resynchronize
   and drop the capture. Root cause (USB IN handshake/host side) still to be found.
2. **Throughput**: per capture, the ESP32 fills/checks its 64KB capture memory and packs it before
   the QSPI writes; continuous ring captures (ESP-SDR ring mode on other chips) would raise the
   duty cycle.
3. **ESP32 duties**: the SDR ESP32 firmware has no menu/OSD/power management: merging the
   transport into the ModRetro MCU firmware (GPL) would keep them (SDR as a mode).
4. **App extras**: channel occupancy view, recording to the PC from the console buttons.
