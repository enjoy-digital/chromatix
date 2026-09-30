# Game Library Study (SD card games in the Chromatic menu)

Goal: play Game Boy games stored on the SD card, selected from the regular Chromatic (ESP32) menu,
with ChromatiX as the FPGA design. Reference: [ChroMagic](https://github.com/cursedtoast2/ChroMagic)
(open custom firmware for the Chromatic, GPL, v1.0.1 September 2026), which already does this on
top of ModRetro's firmware and FPGA v18.8.

## Hardware and firmware facts

- **SD card: on the ESP32**, SDMMC slot 1 (IOMUX: CLK GPIO14, CMD GPIO15, D0 GPIO2), used in 1-bit
  mode at 20MHz (ChroMagic `mcu/main/sd_card.c`). Not visible from the FPGA sources, which is why
  no SD pin appears in the constraints; stock ModRetro firmware doesn't use it.
- **FPGA dependency:** ChroMagic's SD init fix (v1.0.1) makes the FPGA's `ESP32_IO0` pin open-drain
  with a strong pull-up instead of push-pull `DRIVE=8`. ChromatiX drives it push-pull like stock
  v18.8 (`chromatix_platform.py` `esp32_ctrl`/`io0`, `ESP32Control` in `chromatix/gateware/misc.py`):
  to change for SD support. `SDIO_LS` (N5) is left at 1 by everyone (role still undocumented).
- **ESP32 firmware is open source (GPL):**
  [ModRetro/oss-chromatic-console-mcu](https://github.com/ModRetro/oss-chromatic-console-mcu)
  (ESP-IDF + LVGL, ~1.2k lines in `main/`; tags v0.13.2..v4.2). The console tested here runs
  v0.13.2 (4MB flash dump saved: `chromatic_esp32_flash_dump_2026-09-30.bin`, factory 1MB app, no
  OTA/filesystem partition). It installs over USB-C with `esptool` through the ChromatiX
  CDC ↔ ESP32 bridge (DTR/RTS), validated.

## How ChroMagic does it

### ESP32 side (~8.2k new lines on top of ModRetro's firmware)
- **Menu:** a **BACKUPS** tab (folder browser of `/sdcard/CHROMAGIC/BACKUPS`, `.gb`/`.gbc`, A to
  load, B up) and **SYSTEM → CART BACKUP** (dumps the inserted cartridge + save to the SD card),
  plus a **C. MAGICIAN** toggle for the PC companion app (ChroMagician: SD file manager, cartridge
  dump/save restore/flash cart programming over USB).
- **Load:** header checks (logo, header and global checksums, exact size), mapper/masks decoded on
  the ESP32, then PREPARE (core held), ROM uploaded 1KB at a time over QSPI (each block
  acknowledged), `.sav` uploaded, RTC restored, START, then the boot is followed through a status
  lifecycle (reset → boot ROM → first frame) with retries. The menu is closed with an emulated MENU
  press.
- **Saves:** the ESP32 polls an FPGA "save dirty" flag every 250ms and pulls cartridge RAM back in
  1KB blocks (FPGA snapshot buffer read over QSPI, read twice and compared), written to the SD card
  atomically (`.tmp` → rename, `.old` recovery). RTC saved every 60s. QUIESCE/RESUME around game
  switches.
- **Limits:** ROM ≤ 4MB, no menu entry to return to the physical cartridge, SD mounted per
  operation, catalog read in the UI task.

### FPGA side (~3.1k new RTL lines + ~1.5k modified, Gowin 1.9.9)
- **Virtual cartridge:** MiSTer mappers in the FPGA (ROM-only, MBC1 incl. MBC1M, MBC2, MBC3 incl.
  MBC30 + RTC, MBC5 incl. rumble, HuC1), a 2-way set-associative cache (64B lines, 4KB ROM + 4KB
  cartridge RAM, write-back, early restart) on the PSRAM arbiter's highest priority port.
- **Misses stall the CPU only** (T80 `WAIT_n`, OAM DMA/HDMA frozen), the PPU/APU/timers keep
  running.
- **PSRAM map:** OSD 0x000000, framebuffer 0x010000, ROM 0x020000 (≤ 4MB), cartridge RAM 0x420000
  (≤ 128KB).
- **Protocol:**
  - UART cart request 0x0E (op/tag/address/value/aux, 8 bytes); virtual cartridge commands on
    op 7 with magic address 0x5643 ("VC"): STOP, START, STATUS, PREPARE, SAVE_BLOCK,
    RTC_RESTORE_LOW/HIGH, RTC_SNAPSHOT, QUIESCE, RESUME. Responses on channel 0x0A, stream events
    0x0D/acks 0x10.
  - QSPI: PSRAM writes with an upload sequence number in `addr[31:24]`; a new **quad read-back**
    (command 0, length 0x155, `addr[31:16]` = "CB") returning a 12-byte header (status, sequence,
    CRC32) + 1KB: upload status, save snapshot block or cartridge block.
- **Cartridge maintenance engine** (backup path): takes the physical cartridge bus, reads 1KB
  blocks (~1MB/s, CRC32), writes (MBC registers, save restore, flash cart programming), streams to
  USB for the PC app.
- **Menu without cartridge, black LCD timing while the core is held** (OSD stays visible), no
  memory reset on cart-detect changes during a virtual session.
- `rtl/virtual_cart_rebuild/` is an unshipped "never stall" redesign (patched T80, retention and
  admission logic, MBC1/2/5 only).

## Comparison with ChromatiX

| | ChroMagic (FPGA 18.38) | ChromatiX today |
|---|---|---|
| Mappers | ROM, MBC1/1M, MBC2, MBC3/30 + RTC, MBC5, HuC1 | ROM, MBC1, MBC2, MBC3 (no RTC), MBC5 |
| Cache | 2-way, 64B lines, 4KB ROM + 4KB RAM, write-back | Direct-mapped 4KB (16B lines), RAM write-through |
| Miss stall | CPU only (`WAIT_n`), DMA frozen | Whole core (speedcontrol `cart_wait`) |
| ROM/RAM in PSRAM | 0x020000 / 0x420000 | 0x400000 / 0x780000 |
| Control | ESP32 (UART "VC" + QSPI read-back) | Host debug bridge (CSRs) |
| ROM source | SD card via the ESP32 menu | PC (`load-rom`), ~0.1s/256KB |
| Cartridge backup | Yes (maintenance engine) | No |
| Menu without cartridge | Yes | No (menu gated by cart-detect) |

## Recommendation: be ChroMagic-compatible

Implement ChroMagic's FPGA-side protocol in ChromatiX, so that the **ChroMagic ESP32 firmware (GPL,
based on ModRetro's) runs unchanged with the ChromatiX bitstream**: SD playback and cartridge
backup inside the regular menu, no new ESP32 firmware to write or maintain, and the same PC app.

Steps (each checked on hardware with the ChroMagic MCU firmware installed via `esptool`, ESP32
backup ready for restore):
1. **SD enable:** `ESP32_IO0` open-drain with pull-up (platform/`ESP32Control`), then check the
   BACKUPS tab mounts the card (stock FPGA protocol is enough for browsing).
2. **Protocol:** UART 0x0E/0x0A/0x0D/0x10 in `sysmon.py` (new channels and priority), QSPI upload
   sequence + quad read-back in `memory.py` (`QSPISlave`: drive the data lines after the address
   phase), "VC" command decoder + status/lifecycle word.
3. **Virtual cartridge:** PSRAM map configurable (ChroMagic map in this mode), PREPARE/START/STOP
   lifecycle and status bits, save dirty flag + 1KB snapshot buffer, QUIESCE/RESUME; add MBC1M,
   MBC30, HuC1 and MBC3 RTC (restore/snapshot); consider 2-way + write-back cache and CPU-only
   stall (`WAIT_n`) as ChroMagic, or keep speedcontrol if audio/video pacing tolerates it (measure
   stall statistics with the existing counters).
4. **Menu without cartridge** and black LCD timing while the core is held (the `LCDTerminal` timing
   approach), no `memrst` on cart-detect during a virtual session.
5. **Cartridge maintenance engine** (backup to SD, save restore, flash carts) and USB streaming for
   ChroMagician: second phase.

Tests: Migen simulations of the protocol (UART packets, QSPI read-back) and of the virtual cartridge
lifecycle, ChroMagic's host tests as a reference; hardware with the ChroMagic MCU firmware.

Alternatives: our own protocol (simpler, but needs our own ESP32 firmware changes), or proposing a
documented protocol to ModRetro/ChroMagic. Compatibility gets the feature to users fastest and
keeps ChromatiX usable with the existing community firmware.

## Status (branch `chromagic`)

Implemented (simulation tests replaying the ESP32 sequences, bitstream timing met, no regression with
the stock ModRetro ESP32 firmware on hardware):
- `ESP32_IO0` open-drain with pull-up (SD card init).
- QSPI: command/length decode, read-back (upload status, save snapshot), upload sequence numbers.
- Cartridge link packets (0x0e requests, 0x0a responses, 14 channels, priority/guard as ChroMagic).
- Virtual cartridge: ChroMagic PSRAM map and mapper selection (ROM only, MBC1/1M, MBC2, MBC3/30,
  MBC5, HuC1), save snapshots and dirty flag, quiesce, "VC" commands and boot lifecycle, black
  frames while held, menu without cartridge, no memory reset during a session.

Not implemented yet: MBC3 RTC (capability bit reported as 0: the ESP32 firmware loads games without
RTC), cartridge maintenance engine (SYSTEM → CART BACKUP, ChroMagician PC mode: answered as
unsupported), audio snapshots.

Hardware test with the ChroMagic ESP32 firmware (restorable from the ESP32 flash backup):

```bash
esptool.py --port /dev/ttyACM0 --baud 460800 write_flash 0x10000 mcu.bin   # ChroMagic 1.0.1 release.
# SD card (FAT32): /CHROMAGIC/BACKUPS/<game>.gb|.gbc (+ .sav), then BACKUPS tab in the menu.
esptool.py --port /dev/ttyACM0 --baud 460800 write_flash 0 chromatic_esp32_flash_dump_2026-09-30.bin  # Restore.
```

## Open questions
- ChroMagic's protocol is defined by its code (no spec): versions may change; pin a ChroMagic
  release (1.0.1) and track changes.
- License: ChroMagic is GPL (as ModRetro's sources); ChromatiX's ports of ModRetro's design are
  GPL-3.0 already, a reimplementation of the protocol in Migen is our own code.
- `SDIO_LS` role and whether other ESP32 strap pins driven by the FPGA affect the SD card.
