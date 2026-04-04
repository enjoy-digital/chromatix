# LiteX-ification of Chromatic FPGA Design

Progressive migration of the ModRetro Chromatic FPGA design from hand-crafted RTL + Gowin TCL build system to LiteX.

## Goal

Show the official developer the possibilities of the LiteX ecosystem by progressively simplifying the project. Each step produces a buildable, hardware-testable design -- small incremental changes to ease revalidation from a known-working baseline.

## Current Architecture

- **FPGA**: Gowin GW5A-25 (GW5A-EV25UG256CC1/I0)
- **Build**: TCL scripts → Gowin Synthesis + P&R
- **Clocks**: Main PLL (33.554 MHz → fClk/pClk/hClk/gClk/xClk), USB PLL (24 MHz)
- **Subsystems**: Video (LCD + HDMI), Audio (TLV320 codec), Memory (PSRAM + QSPI), Emulation (MiSTer GB core), USB (UVC + UART + UAC), System Monitor, ADC, Buttons, LEDs

## Migration Steps

### Step 1: LiteX Build Wrapper ✅
Replace TCL build system with `top.py` Python script. RTL stays 100% untouched -- LiteX is purely a build system wrapper.
- Created `top.py` with Platform definition, source file list, and constraint injection.
- `python3 top.py --build` produces equivalent bitstream to original TCL flow.

### Step 2: Replace Main PLL with LiteX CRG ✅
Replace `gowin_pll.v` (Gowin IP) with LiteX-managed `GW5APLL` clock generation.
- Created CRG class using `GW5APLL` producing fClk/pClk/hClk/gClk/xClk (same IDIV/FBDIV/MDIV/ODIV as original).
- LiteX is now the true top-level (`chromatic` module); original `top.v` is a submodule via `Instance()`.
- Switched to litex-boards platform (`modretro_chromatic.py`) for proper pin definitions.
- All platform resources requested and mapped to `top.v` Instance ports.
- LiteX auto-generates CST and base SDC; original SDC added for generated clock/timing constraints.

### Step 3: Absorb top.v Glue Logic into LiteX
Move all glue logic (resets, LED FSM, timers, ESP32 boot delay, etc.) from `top.v` into Python. Existing RTL subsystems become `Instance()` black boxes.

### Step 4: Replace UART with LiteX UART
Swap custom UART2 + VHDL rx/tx for LiteX RS232PHY (115200 baud, 8N1).

### Step 5: Replace I2C with LiteX I2C
Swap custom `i2c_master.sv` for LiteX I2CMaster.

### Step 6: Replace Button Debouncers with LiteX GPIOIn
Replace 8x `button_debouncer` instances with LiteX GPIO inputs.

### Step 7: Replace LED Control with LiteX GPIOOut
Move LED state machine to LiteX.

### Future Steps
- **Step 8**: LCD SPI Init → LiteX SPI master
- **Step 9**: Audio system → LiteX audio cores
- **Step 10**: Memory system → LiteX memory infrastructure
- **Step 11**: Video pipeline → LiteX video
- **Step 12**: USB subsystem (complex due to encrypted soft PHY, likely stays as-is)

The **emulation core** (MiSTer Game Boy) remains as Verilog permanently.

## Usage

```
python3 top.py --build                 # Full build (requires gw_sh in PATH)
python3 top.py --build --no-compile    # Generate build files only
```

## Verification Strategy

At each step:
1. `python3 top.py --build` completes without errors.
2. Compare resource utilization with previous step.
3. Flash and verify changed functionality on hardware.
4. Git tag each working step for easy rollback.
