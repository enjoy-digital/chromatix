# ChromatiX Roadmap

## Context

Steps 1–15 moved the build, clocking, glue logic, I2C/I2S/UART, codec/LCD init and the system monitor
transport into LiteX. The design still depends on:

- **Gowin IP**, some of it encrypted: USB device controller, USB soft PHY, colour-space convertor,
  `fifo_video`, `fifo1k`, fixed-point divider, `Gowin_ADC` and a second `Gowin_PLL_UVC`.
- **Hand-written vendor RTL** (~6k lines, outside the GB core): PSRAM controller and arbiter, video
  pipeline, QSPI slave, `system_monitor`, USB glue.
- **A single 2235-line `top.py`** with the build at the repo root and RTL under `esp32t/src/`.

Goals:

1. Remove as much vendor IP and vendor RTL as possible and replace it with LiteX cores. Where a core
   is missing, write it generically and upstream it to LiteX.
2. Reorganize the repo like `litex_m2sdr`: a Python package, a target script, a local platform,
   pytest simulations, CI and an m2sdr-style README.
3. Use recent LiteX features: GW5A primitives and constraint APIs, `VideoFrameBuffer` reading from
   any Wishbone memory, APMemory PSRAM, `GowinJTAG` + JTAGBone, `SPILCD`, GW5APLL DPA.

Constraints that apply to every step:

- Keep the ESP32 firmware protocols unchanged: the QSPI framebuffer/OSD writes, the UART packet
  protocol, and the version field read by the ModRetro updater.
- Every step must build with `--no-compile`, then be checked on hardware before its legacy RTL is
  deleted. This is the same pattern as steps 1–15.
- The GW5A-25 has no Apicula support (LiteX rejects GW5 devices), so the Gowin IDE remains required.

---

## Progress (2026-09-25)

- Phase A: done (package/platform/tests/CI, top-level glue in LiteX modules).
- Phase B: SoCMini + CSRs + debug bridge done, over USB CDC (UARTBone) since the GWU2X JTAG
  cable is not usable from OpenOCD; LiteScope not yet added.
- Phase C: `fifo1k` (encrypted), PSRAM arbiter and burst writers done.
- Phase F1: USB controller/PHY updated to the Gowin V1.9.12.04 IP sources.
- Toolchain: Gowin V1.9.12.04 required (USB does not enumerate with V1.9.10 builds).

## Phase A — Repository restructure (no functional change)

Target layout, modelled on `/home/florent/dev/litex/litex_m2sdr`:

```
chromatix.py              # target: CRG + BaseSoC(SoCMini) + main()
chromatix_platform.py     # local platform (_io, Platform, create_programmer)
chromatix/
  __init__.py                 # re-exports Platform
  gateware/
    crg.py                    # CRG (GW5APLL), timers/enables
    buttons.py                # debouncer
    audio/{i2s.py,tlv320.py}  # I2S, TLV320 init, codec/PMIC polling
    lcd/{st7785.py,...}       # LCD init sequencer (later: timing/PHY, overlays)
    sysmon/{transport.py,payloads.py}
    constraints.py            # timing constraints now in install_toolchain_fixes()
  verilog/                    # remaining legacy RTL, moved from esp32t/src/rtl (emu/, usb/, bsp/)
  data/                       # regs.bin, tlv320regs.hex
scripts/                      # on-board test scripts (litex_cli/RemoteClient) once a bridge exists
test/test_<core>.py           # pytest + litex.gen.sim per ported core
.github/workflows/ci.yml      # litex_setup + elaborate (--no-compile) + pytest
setup.py  pytest.ini  .gitignore  README.md  CHANGELOG.md
```

Steps:

1. **Split `top.py` into `gateware/*.py`.** Move the code without changing it. The generated
   `build/gateware/chromatic.v` must stay the same, apart from name ordering.
2. **Local platform.** Create `chromatix_platform.py`, starting from the litex-boards
   `modretro_chromatic` platform. It is needed because we already override the device
   (`GW5A-EV25UG256CC1/I0` / `GW5A-25A`) and depend on board-specific constraints. Upstream the fixes
   to litex-boards later.
3. **Move `esp32t/src/rtl` to `chromatix/verilog`.** The `Gameboy_MiSTer` submodule path moves
   with it. Drop the now meaningless `esp32t/` name, and delete dead files (`mpmc.v`, `vid_tpg.v`, and
   the empty `gowin_pll_preevt/`, `board/`, `impl/`).
4. **Packaging and hygiene.** Add `setup.py`, `pytest.ini`, and a `.gitignore` covering `build/`,
   `__pycache__`, `*.egg-info` and `*.csv`.
5. **Tests and CI.** Add pytest simulations for the cores already ported (debounce, UART packet
   RX/TX + CRC, arbiter, ST7785/TLV320 sequencers, polling FSM). Add CI that runs `litex_setup`, the
   `--no-compile` elaboration, and pytest.
6. **README.** Rewrite in m2sdr style: banner, badges, TL;DR, architecture, getting started, and a
   migration status table.
7. **Licensing.** Decide and document the license split: new LiteX Python under BSD-2, and the GPLv3
   GB core and ModRetro RTL under their own license, with the repo `LICENSE` staying GPLv3.

## Phase B — Adopt LiteX infrastructure

1. **`BaseSoC(SoCMini)` with a CSR bus** (no CPU), replacing `ChromaticTop(Module)`. Debug access:
   - `add_jtagbone()` through `GowinJTAG` (supported with the Gowin toolchain), with a UARTBone
     fallback.
   - LiteScope probes (`--with-*-probe`).
   - CSRs for status and control (PSRAM BIST, battery, version), so `litex_cli` and `scripts/` can
     inspect the running board. This is the main bring-up tool for Phases C–E.
2. **Constraints.** Replace most of `install_toolchain_fixes()` with
   `platform.add_period_constraint`, `add_false_path_constraint` and `add_generated_clock_constraint`.
   Keep a small hook only for what still can't be expressed.
3. **Build variants and release.** Add argparse variants (`--with-usb`, `--with-jtagbone`,
   `--with-scope`), a `get_build_name()`, and a `release.py` like m2sdr's.

## Phase C — Remove the simple IP and RTL (low risk)

| Item | Replacement |
|---|---|
| `Gowin_PLL_UVC` (PLLA, 24 → 60 MHz) | Second `GW5APLL` in the CRG |
| `fifo1k` (encrypted) + `gb_burst_write`/`mm_burst_write` | `stream.AsyncFIFO` + Migen burst writers |
| `fifo_video` (encrypted) | `stream.AsyncFIFO` / `ClockDomainCrossing` |
| `Color_Space_Convertor_Top` (encrypted) | Migen RGB→YCbCr module (litevideo `csc` style) |
| `Fixed_Point_Divider_Top` (encrypted) + `uart.v`, `uart_rx/tx.vhd` | LiteX `RS232PHY` with a runtime-tunable `tuning_word` computed in Migen, or a fixed baud |
| `Gowin_ADC` + `adc_wrap.v` | LiteX module that instantiates the `ADC` hard primitive + `TLVDS_IBUF_ADC` directly. Extend `GowinAroraVTemperatureSensor`/`hwmon.py` upstream with a VBAT (vsen) mode. The hard ADC itself cannot be removed. |
| `system_monitor.sv` rest | Migen: backlight PWM (`litex.soc.cores.pwm`), battery averaging and thresholds, RX command decode, menu toggle |
| `emu_system_top.v` glue (cart, speedcontrol, audio filter/IIR) | Migen. Only the `gb` core stays a Verilog instance |

After Phase C, the only encrypted IP left is the USB device controller and the USB soft PHY.

## Phase D — PSRAM memory system in LiteX

This replaces `PSRAMController.vhd`, `MultiPortRamCtrl.vhd`, `PSRAMBIST_Burst.vhd`,
`mem_system_top.sv`, `mm_burst_read_to_stream.v` and `qspi_slave.v`.

The board's PSRAM is an AP Memory OPI **x8** part: DQ[7:0], a single DQS/RWDS, DDR, MR0/4/8 config and
latency 3–7.

1. **GW5A I/O primitives, upstreamed to `litex/build/gowin/common.py`.** Add wrappers for `OSER4`,
   `IDES4` and `IODELAY`. The legacy controller uses exactly these, and LiteX has none yet.
2. **x8 PSRAM PHY and core.**
   - Extend `litex/soc/cores/ram/apmemory.py` (currently x16 only) to x8, reusing its MR/latency
     logic.
   - Add a GW5A PHY using the primitives from step 1.
   - Use `GW5APLL.expose_dpa()` for read-capture phase calibration.
   - Upstream the result as a generic core.
3. **Port arbitration.** Put the 5 current clients behind a Wishbone `Arbiter`/`Crossbar`:
   BIST → CSR-driven test, QSPI write, framebuffer write, framebuffer read, OSD read.
   Use burst-capable Wishbone DMA from `litex/soc/cores/dma.py` for the streaming ports, and
   `wishbone.Cache` where it helps.
4. **QSPI slave.** Port it to Migen with the exact ESP32 protocol; `SPIBone` is not
   protocol-compatible.
5. **Validation.** Check on hardware with the BIST over JTAGBone and LiteScope on the PHY, before
   deleting the VHDL.

## Phase E — Video pipeline in LiteX

This replaces `vid_system_top.sv`, `ST7785_panel_master.v` and the `overlay*.vhd` files.

1. **Panel timing and PHY.** Use `VideoTimingGenerator` with custom ST7785 RGB666 timings, and drive
   the dotclk with a LiteX `DDROutput` in place of the raw `ODDR`. Add a small RGB666 PHY, or fix the
   litex-boards `VideoLCDPHY` mapping.
2. **Framebuffers.** Use `VideoFrameBuffer` reading from PSRAM over Wishbone (supported since
   2026-07). The previous frame (frame blend) and the OSD layer become two readers.
3. **Processing blocks.** Port frame blend, menu slide-out, colour correction (multiply-add, with LCD
   and UVC outputs) and the overlays (battery, timer, debug) as stream blocks in `gateware/video/`.
4. **LCD SPI.** Evaluate the new `SPILCD` core for the SPI control pins, next to our ST7785 init.

## Phase F — USB (highest risk; research track)

The Gowin device controller and soft PHY are encrypted and provide **high-speed** composite
UVC + UAC + CDC. LiteX only has full-speed CDC-ACM (LUNA, through Amaranth) and ValentyUSB.

- **F1 (do now).** Keep only `USB_Device_Controller_Top` and `USB2_0_SoftPHY` as black boxes. Move
  everything else to LiteX: the PLL (Phase C), FIFOs, CSC, CDC UART bridge, and
  `usb_descriptor_video` and the class handlers as Migen descriptor ROMs and FSMs. This leaves the
  smallest possible encrypted surface.
- **F2 (research).** Evaluate a LUNA-based composite device (UVC + UAC + CDC).
  - Full speed works with a LiteX raw-D+/D− PHY. Its limit: isochronous FS gives about 1 MB/s,
    which limits UVC to roughly 160×144 at about 20–25 fps in YUY2, or requires compression or a
    lower frame rate.
  - High speed needs a UTMI/ULPI PHY. No open-source GW5A soft PHY exists, so this is a long-term
    effort.
  - Decide after measuring what users actually rely on (UVC capture vs CDC).
- **F3 (done).** The Gowin USB 2.0 SoftPHY is replaced by LiteX's USB2PHY (HS + FS UTMI),
  validated on hardware (480M enumeration, UVC/UAC/CDC).
- **F4 (in progress).** Replace the Gowin USB Device Controller: LUNA's USB 2.0 device (Amaranth,
  HS UTMI, high-bandwidth isochronous IN) integrated through LiteX's Amaranth2VConverter on our
  USB2PHY, then a native Migen port of the same layers (reset/chirp, packet, transaction, control,
  endpoints) checked against LUNA in co-simulation.

## Phase G — Optional / long term

- A small soft CPU (VexRiscv-lite / FemtoRV / SERV) running firmware for the menu, system monitor,
  battery and overlays. It would replace fixed-function Migen with C, and expose everything through
  CSRs.
- A Verilator `litex_sim` target (GB core + video sim module) for testing without hardware.
- Upstream the board target to litex-boards: `modretro_chromatic` with PSRAM, LCD, codec and battery.

## What remains as non-LiteX, by design

- The MiSTer Game Boy core (`gb.v`, T80, `gbc_snd`, ...): third-party GPL IP, instantiated from the
  submodule.
- The GW5A hard ADC primitive.
- The USB device controller, until it is replaced (F4: LUNA first, then a native Migen port). The
  Gowin SoftPHY has been replaced by LiteX's USB2PHY (F3).

## Verification (every step)

1. `python3 chromatix.py --build --no-compile` elaborates.
2. `pytest` passes, with a new sim test for each ported core.
3. A full Gowin build meets timing (compare the post-P&R utilisation and Fmax report with the previous
   step).
4. On the board: boot a cartridge, check LCD, audio, buttons, menu/OSD, battery LED, ESP32 update
   path, and USB UVC/CDC. Use JTAGBone/LiteScope from Phase B on.
5. Remove the legacy files only after the hardware check, and update the README migration table.
