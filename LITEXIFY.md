# LiteX-ification of Chromatic FPGA Design

Progressive migration of the [ModRetro Chromatic](https://modretro.com/products/chromatic) FPGA design from hand-crafted RTL + Gowin TCL build system to [LiteX](https://github.com/enjoy-digital/litex).

## Overview

The Chromatic is a Game Boy / Game Boy Color handheld built around a Gowin GW5A-25 FPGA. The original design uses ~80 Verilog/VHDL source files, a TCL-based build system, and Gowin-generated IP for clocks, FIFO, and ADC.

This project demonstrates how LiteX can progressively simplify and modernize an existing FPGA design:
- Replace the build system with a single Python script.
- Replace low-level IP (PLL, UART, I2C, button debouncers) with LiteX cores.
- Move glue logic from Verilog to Python/Migen for easier maintenance.
- Keep complex subsystems as Verilog `Instance()` black boxes for safe, incremental migration.

## Architecture

```
                            ┌──────────────────────────────────┐
                            │         LiteX (top.py)           │
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
  LCD ◄─────────│  vid_system_top + ST7785_init (Video)        │           │
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
- **Glue logic**: Timers, cart-detect reset, LED FSM, LCD enable sync, USB init delay, ESP32 boot delay, UART resync, HDMI debug routing.
- **I2S**: Audio serialization with mute, mono/stereo mixing, headphone routing.
- **I2C**: LiteI2C PHY with LiteX/Migen codec-init and codec/PMIC polling FSMs.
- **UART**: LiteX RS232PHY (115200 baud, replaces custom UART2).
- **Buttons**: 8-channel debouncer (3-stage sampling + 15-bit counter).

### What's in Verilog (Instance black boxes)

- **vid_system_top**: LCD panel master, frame buffering, OSD overlays, color correction.
- **ST7785_init**: LCD SPI initialization sequence (9-bit protocol).
- **mem_system_top**: PSRAM controller, multi-port arbiter, QSPI slave.
- **emu_system_top**: MiSTer Game Boy core (Z80 CPU, graphics, sound, cartridge).
- **usbuvcuart_top**: USB 2.0 soft PHY + UVC video + UART + UAC audio.
- **system_monitor**: Menu UI, palette control, battery monitoring.
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

**Current cleanup: 8 legacy RTL files removed, 4 legacy Gowin project files removed, 2 RTL files adapted** (`vid_system_top.sv`, `system_monitor.sv`).

Note: `tlv320regs.hex` remains as the codec register image, and `uart_rx.vhd`, `uart_tx.vhd`, and `fixed_point_divider.v` are still required by the USB CDC/UART block.

### Future Steps

- **Memory system**: Replace PSRAM controller + multi-port arbiter with LiteX memory infrastructure.
- **Video pipeline**: Progressive LiteX-ification of frame buffering, color correction, OSD.
- **USB subsystem**: Complex due to encrypted Gowin soft PHY; likely stays as Verilog.
- **Emulation core**: MiSTer Game Boy core stays as Verilog permanently (well-tested third-party IP).

## Usage

```bash
# Generate build files only (no Gowin toolchain required).
python3 top.py --build --no-compile

# Full build (requires gw_sh in PATH).
python3 top.py --build

# Use open-source Apicula toolchain.
python3 top.py --build --toolchain apicula
```

Output bitstream: `build/chromatic.fs`

## Programming

The LiteX board definition uses `openFPGALoader` with the Chromatic's built-in USB/JTAG bridge (`--cable gwu2x`).

```bash
# Detect the powered-on board.
openFPGALoader --detect --cable gwu2x

# Load the bitstream temporarily.
openFPGALoader --cable gwu2x --bitstream build/chromatic.fs

# Program internal flash and reset.
openFPGALoader --write-flash --cable gwu2x --reset build/chromatic.fs
```

Notes:
- The console must be powered on for the FPGA to enumerate.
- `openFPGALoader` must be built with GWU2X support.
- The LiteX flow emits `build/chromatic.fs`, not the legacy `esp32t/impl/pnr/evt1_x2.fs`.

## Dependencies

- [LiteX](https://github.com/enjoy-digital/litex) (with Gowin backend)
- [LiteX-Boards](https://github.com/litex-hub/litex-boards) (modretro_chromatic platform)
- [LiteI2C](https://github.com/enjoy-digital/litei2c)
- [Migen](https://github.com/m-labs/migen)
- Gowin EDA (for synthesis/P&R) or [Apicula](https://github.com/YosysHQ/apicula) (open-source)
