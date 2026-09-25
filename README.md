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

### What's in LiteX (Python/Migen)

- **CRG**: GW5APLL clock generation (5 domains).
- **Glue logic**: Timers, cart-detect reset, LED FSM, LCD init, LCD enable sync, USB init delay, ESP32 boot delay, UART resync, HDMI debug routing.
- **I2S**: Audio serialization with mute, mono/stereo mixing, headphone routing.
- **I2C**: LiteI2C PHY with LiteX/Migen codec-init and codec/PMIC polling FSMs.
- **UART**: LiteX RS232PHY (115200 baud, replaces custom UART2).
- **System monitor transport**: LiteX/Migen UART packet RX/TX framing, CRC, and channel arbiter.
- **System monitor payloads**: LiteX/Migen channel-valid generation and payload byte packing.
- **Buttons**: 8-channel debouncer (3-stage sampling + 15-bit counter).

### What's in Verilog (Instance black boxes)

- **vid_system_top**: LCD panel master, frame buffering, OSD overlays, color correction.
- **mem_system_top**: PSRAM controller, multi-port arbiter, QSPI slave.
- **emu_system_top**: MiSTer Game Boy core (Z80 CPU, graphics, sound, cartridge).
- **usbuvcuart_top**: USB 2.0 soft PHY + UVC video + UART + UAC audio.
- **system_monitor**: Menu UI, palette control, battery monitoring, and request generation.
- **adc_wrap**: Gowin ADC for battery voltage measurement.

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

**Current cleanup: 14 legacy RTL files removed, 4 legacy Gowin project files removed, 2 RTL files adapted** (`vid_system_top.sv`, `system_monitor.sv`).

Note: `uart_rx.vhd`, `uart_tx.vhd`, and `fixed_point_divider.v` are still required by the USB CDC/UART block.

### Future Steps

See [doc/ROADMAP.md](doc/ROADMAP.md).

## Repository Layout

```
chromatix.py              # Target: BaseSoC + build/load/flash.
chromatix_platform.py     # Chromatic platform (IOs, Gowin options, programmer).
chromatix/
  gateware/                   # LiteX/Migen cores (CRG, LCD, codec, system monitor, sources).
  data/                       # ST7785 / TLV320 register images.
  verilog/                    # Remaining legacy RTL (bsp/, emu/, usb/, ip/) + Gameboy_MiSTer submodule.
test/                         # Simulation tests (pytest).
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

Output bitstream: `build/chromatic.fs`

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

## Dependencies

- [LiteX](https://github.com/enjoy-digital/litex) (with Gowin backend)
- [LiteI2C](https://github.com/enjoy-digital/litei2c)
- [Migen](https://github.com/m-labs/migen)
- Gowin EDA (for synthesis/P&R; Apicula does not support the GW5A yet)
