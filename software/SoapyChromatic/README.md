# SoapyChromatic

[SoapySDR](https://github.com/pothosware/SoapySDR) module for the Chromatic SDR: GNU Radio, gqrx,
CubicSDR, SoapySDR Python... receive 2.4 GHz I/Q from the Chromatic ESP32 (ESP-SDR).

The module talks the ESP-SDR protocol over the Chromatic USB CDC port:

- `--with-app` bitstream + `firmware/sdr` (ESP32: `firmware/esp32-sdr`): the console relays the
  protocol, captures go ESP32 -> QSPI -> PSRAM -> DMA -> USB (~73 captures/s of 16380 samples,
  2.4MB/s) and are also displayed on the console.
- Standard bitstream (USB <-> ESP32 UART bridge, stock or Chromatic ESP-SDR): 2 Mbaud UART, slower.

## Build/Use

```sh
mkdir build && cd build && cmake .. && make
export SOAPY_SDR_PLUGIN_PATH=$PWD     # Or: sudo make install.
SoapySDRUtil --probe="driver=chromatic"
gqrx                                  # Device string: soapy=0,driver=chromatic
```

The console must run `firmware/sdr` (`scripts/chromatic.py --serial /dev/ttyACM0 run
firmware/sdr/sdr.bin`): it relays the protocol on the USB CDC port. Debug tools
(`scripts/chromatic.py --serial`) take the port back with a 1200 baud touch; the relay resumes 1s
after they close it.

Device arguments: `serial=<port>` (default: the Chromatic CDC port,
`/dev/serial/by-id/*Chromatic*if02`), `samples=<burst samples>` (256-16380, default 16380).

- Sample rates: 16/40 MS/s (ESP32 hardware rates), frequency: 100-6000 MHz (1 MHz steps, the
  ESP32 RF front-end is for 2.4 GHz), gain: AGC or 0-72 (manual, `LNA` element).
- Formats: CS8 (native), CS16, CF32.

## Bursts

The ESP32 captures bursts (16380 samples: 410us at 40 MS/s) with gaps (capture, transfer):
~1.1 MS/s are delivered, not a continuous stream. Each burst starts with a time
(`SOAPY_SDR_HAS_TIME`, host time at reception) and ends with `SOAPY_SDR_END_BURST`. Applications
assuming a continuous stream (gqrx...) run on the bursts as if they were contiguous: fine for
spectrum/waterfall views, not for demodulation across bursts.

The protocol is also usable directly: `scripts/chromatic_sdr.py` (info, bench, spectrum, record).
