# ChromatiX

LiteX-based rebuild of the ModRetro Chromatic FPGA design (Gowin GW5A-25).

ChromatiX is a progressive migration of the [ModRetro Chromatic](https://modretro.com/products/chromatic) FPGA design from hand-crafted RTL + Gowin TCL build system to [LiteX](https://github.com/enjoy-digital/litex).

## Overview

The Chromatic is a Game Boy / Game Boy Color handheld built around a Gowin GW5A-25 FPGA. The original design uses ~80 Verilog/VHDL source files, a TCL-based build system, and Gowin-generated IP for clocks, FIFO, and ADC.

ChromatiX demonstrates how LiteX can progressively simplify and modernize an existing FPGA design:
- Replace the build system with a single Python script.
- Replace low-level IP (PLL, UART, I2C, button debouncers) with LiteX cores.
- Move glue logic from Verilog to Python/Migen for easier maintenance.
- Keep complex subsystems as Verilog `Instance()` black boxes for safe, incremental migration.

## Architecture

```
                            ┌──────────────────────────────────┐
                            │    LiteX (chromatix.py)      │
                            │                                  │
                            │  ┌────────┐   ┌──────────────┐  │
  33.55MHz ──────────────►  │  │  CRG   │   │ Timer/Enable │  │
                            │  │ GW5APLL│   │  gClk/xClk   │  │
                            │  └─┬──┬──┬┘   └──────────────┘  │
                            │    │  │  │                       │
                            │  fClk pClk hClk gClk xClk       │
                            │    │  │  │   │    │              │
                            │    ▼  ▼  ▼   ▼    ▼              │
                ┌───────────┼──────────────────────────────────┼───────────┐
                │           │  Verilog Subsystem Instances     │           │
                │           │                                  │           │
  Cartridge ◄──►│  emu_system_top (MiSTer GB Core)             │◄──► IR    │
                │           │                                  │           │
  PSRAM/QSPI◄──►│  mem_system_top (Memory Arbiter + PSRAM)    │           │
                │           │                                  │           │
  LCD ◄─────────│  vid_system_top + LiteX LCD init (Video)     │           │
                │           │                                  │           │
  USB ◄─────────│  usbuvcuart_top (UVC + UART + UAC)          │──► ESP32  │
                │           │                                  │           │
                │           │  system_monitor (Menu/UI/OSD)    │           │
                │           │  adc_wrap (Battery Voltage)      │           │
                │           │  LiteI2C init/polling FSMs       │           │
                └───────────┼──────────────────────────────────┼───────────┘
                            │                                  │
                            │  ┌──────────────┐ ┌──────────┐  │
  I2C (SCL/SDA)◄────────── │  │ LiteI2C PHY  │ │ RS232PHY │  │──► ESP32 UART
                            │  └──────────────┘ └──────────┘  │
                            │                                  │
                            │  ┌──────────────┐ ┌──────────┐  │
  Audio Codec ◄──────────── │  │ I2S (Migen)  │ │ Buttons  │  │◄── D-Pad/A/B
                            │  └──────────────┘ │ (Migen)  │  │
                            │                   └──────────┘  │
                            └──────────────────────────────────┘
```

### Clock Domains

| Domain | Frequency   | Source     | Usage                          |
|--------|-------------|------------|--------------------------------|
| fClk   | ~134.22 MHz | GW5APLL    | Memory system (PSRAM)          |
| pClk   | ~33.55 MHz  | GW5APLL    | Emulation core, LCD SPI init   |
| hClk   | ~16.78 MHz  | GW5APLL    | Video, I2C, emulation          |
| gClk   | ~8.39 MHz   | GW5APLL    | Audio I2S, timers, UART, USB   |
| xClk   | ~67.11 MHz  | GW5APLL    | Cart detect, LED control       |
| phy    | ~60 MHz     | USB PLL    | USB UART resync, ESP32 boot    |
| sys    | = gClk      | alias      | LiteX CSR bus, debug bridge    |

### What's in LiteX (Python/Migen)

- **CRG**: GW5APLL clock generation (5 domains).
- **Glue logic**: Timers, cart-detect reset, LED FSM, LCD init, LCD enable sync, USB init delay, ESP32 boot delay, UART resync, HDMI debug routing.
- **I2S**: Audio serialization with mute, mono/stereo mixing, headphone routing.
- **I2C**: LiteI2C PHY with LiteX/Migen codec-init and codec/PMIC polling FSMs.
- **UART**: LiteX RS232PHY (115200 baud, replaces custom UART2).
- **System monitor transport**: LiteX/Migen UART packet RX/TX framing, CRC, and channel arbiter.
- **System monitor payloads**: LiteX/Migen channel-valid generation and payload byte packing.
- **System monitor control**: ESP32 packet decode (palettes, MCU buttons, brightness, system control), menu toggle, backlight PWM, battery AA/Li-ion detection/averaging, low battery/LED status.
- **Battery ADC**: GW5A hard ADC (voltage mode) + request/ready FSM.
- **Video pipeline**: frame buffer addressing, frame blending, OSD/overlays (battery, timer, debug), GBC color correction (LCD/UVC), line buffer and ST7785 RGB666 panel timing, dot clock.
- **Buttons**: 8-channel debouncer (3-stage sampling + 15-bit counter).
- **Memory system**: AP Memory OPI x8 PSRAM controller with GW5A OSER4/IDES4/IODELAY PHY, startup BIST, ESP32 QSPI slave (GW5A DFFC for the CS asynchronous reset), multi-port round-robin arbiter, Game Boy framebuffer and ESP32 QSPI burst writers (LiteX async FIFOs), framebuffer/OSD line readers.
- **USB 2.0 PHY**: LiteX USB2PHY (UTMI, High-Speed 480Mbps + Full-Speed, GW5A SerDes).
- **USB 2.0 device core**: [LUNA](https://github.com/greatscottgadgets/luna)'s USB 2.0 device (Amaranth, BSD-3, converted to Verilog at build time with LiteX's Amaranth2VConverter): High-Speed reset/chirp, packets, CRC, data toggles/handshakes, standard requests, control/isochronous (high-bandwidth)/bulk endpoints, with a request bridge to the Migen EP0 handlers.
- **USB composite device** (UVC + UAC + CDC-ACM): USB PLL, descriptors, class requests, UVC YUYV packing/packetizer (color space convertor + video FIFO), UAC endpoint, CDC UART at the host baudrate.
- **UVC 320x288**: the UVC stream is offered at **320x288** (default, 2x2 integer upscale: each YUY2 chroma pair is a single source pixel, so colors are exact) and at native 160x144, selected by the host (bFrameIndex). Lines are replayed at 60MHz from line buffers; 320x288 uses high-bandwidth isochronous transfers (2048 bytes per micro-frame, DATA1 -> DATA0, alternate setting 2) and runs at 60 fps. Sizes are selected with `--uvc-sizes` (ex: `--uvc-sizes 160x144` for the original single size).
- **SoC**: LiteX SoCMini (CSR bus in the sys = gClk domain) with debug/automation registers (virtual buttons, status) and an optional UARTBone debug bridge over the USB CDC port.

### What's in Verilog (Instance black boxes)

- **emu_system_top**: MiSTer Game Boy core (Z80 CPU, graphics, sound, cartridge).

No vendor (Gowin) IP is left: the only hard primitives used are the GW5A PLLs, SerDes/IO primitives and ADC.

## Migration Steps

| Step | Description | RTL Eliminated |
|------|-------------|----------------|
| 1  | LiteX build wrapper (TCL replacement) | legacy Gowin project files |
| 2  | PLL → LiteX GW5APLL CRG | `gowin_pll.v` |
| 3  | top.v glue → Migen | `top.v` |
| 4  | UART2 → LiteX RS232PHY | `uart.v`, `usb_uart_config.v` |
| 5  | I2C → LiteI2C PHY | `i2c_master.sv` |
| 6  | Buttons → Migen debounce | `button_debounce.v` |
| 7  | LEDs → Migen | (done in Step 3) |
| 8  | LCD SPI init extracted to top level | (moved from `vid_system_top.sv`) |
| 9  | I2S → Migen | (moved out of `aud_system_top.v`) |
| 10 | Audio system wrapper eliminated | `aud_system_top.v` |
| 11 | TLV320 init → LiteX/Migen | `tlv320_init.v` |
| 12 | Codec/PMIC polling → LiteX/Migen | `polling_master.v` |
| 13 | LCD init sequencer → LiteX/Migen | `ST7785_init.v` |
| 14 | System monitor transport → LiteX/Migen | `system_monitor_arbiter.sv`, `uart_packet_wrapper_rx.sv`, `uart_packet_wrapper_tx.sv` |
| 15 | System monitor payload packing → LiteX/Migen | (moved out of `system_monitor.sv`) |
| 16 | Repository restructured as a LiteX project (package, platform, tests, CI) | `top.py`, `mpmc.v`, `vid_tpg.v` |
| 17 | Top-level glue extracted into LiteX modules, audio CDC fix | |
| 18 | PSRAM arbiter + burst writers → LiteX/Migen | `mem_system_top.sv`, `MultiPortRamCtrl.vhd`, `gb_burst_write.v`, `mm_burst_write.v`, `fifo1k.v` (encrypted IP) |
| 19 | Gowin USB IPs updated to V1.9.12.04 sources | pre-synthesized V1.9.9 USB controller netlist |
| 20 | LiteX SoCMini + debug bridge (UARTBone over USB CDC), virtual buttons | |
| 21 | USB PLL, color space convertor, video FIFO and CDC baudrate divider → LiteX/Migen; cart data sampled in hclk (timing fix) | `Gowin_PLL_UVC.v`, `color_space_convertor.v` (encrypted), `fifo_video.v` (encrypted), `fixed_point_divider.v` (encrypted) |
| 22 | System monitor control + battery ADC → LiteX/Migen | `system_monitor.sv`, `adc_wrap.v`, `gowin_adc.v` |
| 23 | Video pipeline → LiteX/Migen | `vid_system_top.sv`, `ST7785_panel_master.v`, 6 `overlay*.vhd` |
| 24 | PSRAM controller/PHY, BIST and line readers → LiteX/Migen | `PSRAMController.vhd`, `PSRAMBIST_Burst.vhd`, `mm_burst_read_to_stream.v` |
| 25 | QSPI slave → LiteX/Migen | `qspi_slave.v` |
| 26 | USB class/device logic → LiteX/Migen (formally checked against the originals) | `usbuvcuart_top.v`, `usb_descriptor_video.v` + defs, `usb_fifo.v`, `sync_rx/tx_pkt_fifo.v`, `uart.v`, `uart_rx.vhd`, `uart_tx.vhd` |
| 27 | USB 2.0 PHY (HS + FS) → LiteX USB2PHY | `usb2_0_softphy*.v` (encrypted IP) |
| 28 | UVC 320x288 (2x2 upscale, high-bandwidth isochronous) alongside 160x144 | |
| 29 | Gowin USB 2.0 Device Controller → LUNA USB 2.0 device core (Amaranth, converted at build time) + Migen EP0 bridge | `usb_device_controller*` (encrypted IP / pre-synthesized netlist) |

**Remaining non-LiteX logic**: the MiSTer Game Boy emulation core (`chromatix/verilog/emu`, kept by design) and the LUNA USB 2.0 device core (Amaranth, open source; a native Migen port is planned).

Ports are checked with simulations, formal equivalence checks against the original Verilog (`test/eqcheck.py`, Yosys) and on hardware (automated through the debug bridge + UVC capture).

### Future Steps

See [doc/ROADMAP.md](doc/ROADMAP.md).

## Repository Layout

```
chromatix.py              # Target: BaseSoC + build/load/flash.
chromatix_platform.py     # Chromatic platform (IOs, Gowin options, programmer).
chromatix/
  gateware/                   # LiteX/Migen cores (CRG, LCD, codec, system monitor, sources).
  data/                       # ST7785 / TLV320 register images.
  verilog/                    # Remaining non-LiteX RTL: emu/ (Game Boy core) + Gameboy_MiSTer submodule.
scripts/                      # Host tools (chromatic.py: debug bridge control, buttons, UVC capture).
test/                         # Simulation/elaboration tests (pytest).
doc/                          # Roadmap.
```

## Usage

```bash
# Get the MiSTer Game Boy core submodule.
git submodule update --init --recursive

# Generate build files only (no Gowin toolchain required).
./chromatix.py --build --no-compile

# Full build.
./chromatix.py --gowin-path ~/tools/gowin_1.9.12.04/IDE --build

# Run simulation tests.
python3 -m pytest -v test
```

Output bitstream: `build/gateware/chromatic.fs` (CSR map: `scripts/csr.csv`).

**Gowin version**: use Gowin V1.9.12.04 (`--gowin-path ~/tools/gowin_1.9.12.04/IDE` or `GOWIN_PATH`).
Bitstreams built with V1.9.10 run the game but the USB (UVC/UAC/CDC) device does not enumerate (also
when building the original ModRetro sources); V1.9.9 fails timing in the Game Boy core.

## Programming

The LiteX board definition uses `openFPGALoader` with the Chromatic's built-in USB/JTAG bridge (`--cable gwu2x`).

```bash
# Detect the powered-on board.
openFPGALoader --detect --cable gwu2x

# Load the bitstream temporarily (SRAM).
./chromatix.py --load

# Program flash and reboot.
./chromatix.py --flash
```

Notes:
- The console must be powered on for the FPGA to enumerate.
- `openFPGALoader` must be built with GWU2X support.
- USB only enumerates when the FPGA configures from flash (also true for the official image): use
  `--flash` when USB is needed.
- Back up the official image before flashing (`openFPGALoader --cable gwu2x --dump-flash --file-size 1048576 official.bin`);
  it can be restored with `openFPGALoader --cable gwu2x --write-flash --file-type bin --reset official.bin`
  or with the ModRetro updater.

## Debug / Automation

A `--with-debug-bridge` build replaces the USB CDC <-> ESP32 UART passthrough with a LiteX UARTBone:
the CSR bus is then accessible from the host over the same USB cable, with virtual buttons (OR'ed with
the physical ones) and status registers. Combined with the UVC video stream, this allows fully
automated tests (press buttons, capture screens). The ESP32 keeps running normally, only its USB
flashing path is unavailable in this variant.

```bash
# Build/flash the debug variant.
./chromatix.py --gowin-path ~/tools/gowin_1.9.12.04/IDE --with-debug-bridge --build --flash

# Start the LiteX server on the USB CDC port.
litex_server --uart --uart-port /dev/ttyACM0 --uart-baudrate 115200

# Control/test.
./scripts/chromatic.py ident
./scripts/chromatic.py status
./scripts/chromatic.py press start --duration 0.2
./scripts/chromatic.py capture frame.png --frames 4 --scale 2
./scripts/chromatic.py capture frame320.png --size 320x288
./scripts/chromatic.py sequence "press:start wait:1.5 press:a wait:1.5 capture:menu.png"
```

The debug variant also has USB debug registers: UVC status/counters (`debug_ctrl_uvc_*`: selected
frame, high-bandwidth micro-frames, frames sent, FIFO level, dropped lines) and a UTMI packet monitor
(`usb_utmi_monitor_*`: PID/length/idle clocks of the transmitted data packets and received SOFs after
the first video packet), to check the USB traffic without a protocol analyzer.

## Dependencies

- [LiteX](https://github.com/enjoy-digital/litex) (with Gowin backend)
- [LiteI2C](https://github.com/enjoy-digital/litei2c)
- [Migen](https://github.com/m-labs/migen)
- [Amaranth](https://github.com/amaranth-lang/amaranth) 0.5.8 + [LUNA](https://github.com/greatscottgadgets/luna) 0.2.3 + usb-protocol 0.9.2 (USB 2.0 device core, installed by `pip3 install -e .`)
- Gowin EDA (for synthesis/P&R; Apicula does not support the GW5A yet)
