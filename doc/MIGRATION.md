# ChromatiX Migration

How the original ModRetro design (hand-written RTL, Gowin TCL project and encrypted Gowin IP) was moved
to LiteX, one block at a time, with the design working on hardware after every step.

## Process

Each block went through the same loop:

1. **Isolate.** Cut the block out of the legacy RTL (or split it into smaller sub-blocks) and keep
   the original as the reference.
2. **Port** it to LiteX/Migen, reusing LiteX cores where they exist (PLL, RS232PHY, LiteI2C, stream
   FIFOs, UARTBone...), or integrating an open core (LUNA for the USB device).
3. **Prove it equivalent** to the original: formal equivalence checks with Yosys
   ([`test/eqcheck.py`](../test/eqcheck.py): bounded checks, plus unbounded PDR proofs, against the
   original Verilog read from the git history), plus behavioural simulations
   (pytest: Migen simulations, Amaranth simulation of the LUNA integration; a Verilator co-simulation
   was used for the endpoint FIFOs of the former Gowin controller path).
4. **Check on hardware** with the automated loop: build/flash, then drive the console from the host
   through the debug bridge (UARTBone over USB CDC: virtual buttons, status/debug registers, UTMI
   packet monitor) and check the output through the UVC video capture and UAC audio.
5. **Delete** the legacy RTL/IP only once all of the above pass, and commit.

## Phases

| Phase | What | Status |
|-------|------|--------|
| A | Build: Gowin TCL project → one Python script; repo as a LiteX project (package, platform, tests, CI) | done |
| B | LiteX infrastructure: SoCMini, CSRs, UARTBone debug bridge over USB CDC, virtual buttons | done |
| C | Glue, clocking, I2C/I2S/UART, codec/LCD init, system monitor, battery ADC; encrypted FIFOs, CSC, divider | done |
| D | Memory: x8 OPI PSRAM controller/PHY, BIST, arbiter, QSPI slave, burst writers/readers | done |
| E | Video pipeline: frame blend, OSD/overlays, color correction, ST7785 panel timing | done |
| F | USB: class logic, UTMI PHY (LiteX USB2PHY), device controller (LUNA); UVC 320x288 | done |

## Clock Domains

| Domain | Frequency   | Source     | Usage                          |
|--------|-------------|------------|--------------------------------|
| fClk   | ~134.22 MHz | GW5APLL    | Memory system (PSRAM)          |
| pClk   | ~33.55 MHz  | GW5APLL    | Emulation core, LCD SPI init   |
| hClk   | ~16.78 MHz  | GW5APLL    | Video, I2C, emulation          |
| gClk   | ~8.39 MHz   | GW5APLL    | Audio I2S, timers, UART, USB   |
| xClk   | ~67.11 MHz  | GW5APLL    | Cart detect, LED control       |
| phy    | ~60 MHz     | USB PLL    | USB UART resync, ESP32 boot    |
| sys    | = gClk      | alias      | LiteX CSR bus, debug bridge    |

## What's in LiteX (Python/Migen)

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

Next steps: [ROADMAP.md](ROADMAP.md).
