# Game Library Study (games stored on the console, selected from the menu)

Goal: store Game Boy games on the console and pick them from the regular Chromatic menu (the ESP32
menu), without a cartridge. This document records the hardware facts, then proposes an architecture,
the FPGA/ESP32 protocol and the options for the ESP32 side.

## Findings (2026-09-30, on hardware)

### No SD card
- **FPGA:** no SD/SDIO/MMC signal in the original ModRetro constraints (`evt1_x2.cst`), `top.v`,
  the ChromatiX platform or the litex-boards platform. `SDIO_LS` (N5) is an undocumented
  level-shifter enable, tied to 1 in every design.
- **ESP32 ↔ FPGA:** the ESP32 lines reaching the FPGA are QSPI (GPIO5/18/23/19/22/21), UART, I2S,
  EN/IO0 and two spares (GPIO9/10); none are the ESP32 SD card pins.
- **ESP32 firmware:** no SD card or FAT driver is linked (no `sdmmc`/`sdspi`/`vfs_fat`), only the
  UART and SPI master drivers.

Unless a socket is physically present (to be checked on the board), there is no SD card to use.

### ESP32 (ModRetro MCU)
- ESP32 rev 3, ESP32-MINI-1 (embedded **4MB** flash), read-only dump saved
  (`chromatic_esp32_flash_dump_2026-09-30.bin`, sha256 `1b83c7d4…`, two identical reads).
- Firmware: ModRetro `mcu_fw` **v0.13.2** (May 12 2025, ESP-IDF v5.3), 677KB, LVGL menu
  (`MenuSystem`, `MenuControls`, `MenuDisplay`, `MenuStatus`), settings in NVS (frame blend, color
  correction LCD/USB, player number, mute, backlight, transitions, D-pad diagonals, low battery
  indicator), `fpga_tx_task`/`fpga_rx_task` (UART + QSPI), power manager, console REPL on UART0.
- Partition table: `nvs` (24KB), `phy_init` (4KB), `factory` app (1MB at 0x10000). **No OTA slots,
  no filesystem partition.**
- **0x110000–0x400000 (~2.9MB) is unused**: it only holds leftovers of Espressif's factory ESP-AT
  firmware (Wi-Fi/HTTP/AT strings found there are not part of ModRetro's app).
- The ESP32 has Wi-Fi/Bluetooth, unused by the current firmware.
- The standard ChromatiX bitstream bridges USB CDC to the ESP32 UART with DTR/RTS: `esptool` works
  over the console's USB-C port (flash read at 460800 baud: 4MB in 99s).

## Architecture

Storage: a filesystem partition in the free ESP32 flash (~2.8MB: e.g. 3–10 typical games, 32KB–1MB
each). Games get there over USB (host tool through the CDC/ESP32 link) or Wi-Fi (upload page on the
ESP32). An SD card would only change the storage backend if one is found.

```
 Storage (ESP32 flash FS)  ──►  ESP32 menu "Games"  ──QSPI (1KB bursts)──►  PSRAM 0x400000 (ROM)
                                     │                                        │
                                     └──UART cmd: vcart config/hold/start──►  Virtual cartridge ──► Game Boy core
 <game>.sav  ◄──── save read-back (UART or QSPI read) ◄──── PSRAM 0x780000 (cartridge RAM)
```

The virtual cartridge (`chromatix/gateware/vcart.py`: MBC1/2/3/5, 4KB cache, cartridge RAM written
through to the PSRAM) is validated on hardware with ROMs loaded from the PC. Here the ESP32 takes
the role of the PC.

### FPGA changes (all in the standard bitstream)
1. **Virtual cartridge always built:** remove `with_vcart = with_debug_bridge` (`chromatix.py`); the
   control comes from the ESP32 instead of the debug bridge CSRs (both kept).
2. **UART commands** on free addresses (in use: 0x2, 0x4, 0x5, 0x6, 0x9, 0xB, 0xC, 0xD, see
   `SystemMonitorControl` in `chromatix/gateware/sysmon.py`):

   | Addr | Payload | Action |
   |------|---------|--------|
   | 0x3 | `enable[0] hold[1] flush[2] mbc[6:4] rom_mask[16:8] ram_mask[23:20]` | Same fields as `VirtualCartCSR.control` |
   | 0x7 | `offset[23:0]`, `length[15:0]` | Save RAM read-back request (answered on a new FPGA→ESP32 channel) |
   | 0x8 | none | Request the virtual cartridge status (enabled, loaded ROM size, save dirty flag) |

3. **Capability flag** in the version channel (payload 6, `SystemMonitorPayloads.VERSION`): one bit
   "virtual cartridge available", so the ESP32 firmware only shows the Games entry on a capable
   bitstream (and keeps working with the official one).
4. **Save read-back:** UART first (a new FPGA→ESP32 channel streaming cartridge RAM: an 8KB save
   takes ~0.8s at 115200 baud, 32KB ~3s); QSPI read later if needed (drive the QSPI data lines
   after the address phase, the command bit is ignored today).
5. **Menu without cartridge:** the menu button is ignored until cart-detect is stable high
   (`chromatix.py`, `cart_det_sr`), and a cart-detect change resets the core/PSRAM (`memrst`). Allow
   the menu when the virtual cartridge is enabled or no cartridge is present, and keep `memrst` for
   physical insert/remove only. A physical cartridge inserted takes priority (disables the virtual
   cartridge).
6. **Display while loading:** with `hold` the core stops and so does the LCD/OSD timing. Loading is
   short (256KB over QSPI at up to 40MHz in a few tens of ms, dominated by the ESP32 flash read:
   ~0.1–0.5s), so a brief blank screen is acceptable. Otherwise, reuse the `LCDTerminal` timing
   approach (`chromatix/gateware/terminal.py`) to keep the OSD visible while the core is held.

### Load / start / exit sequence (ESP32 side)
1. UART 0x3: `enable=0, hold=1`.
2. QSPI: write the ROM to PSRAM 0x400000+ in **1024-byte transactions** (fixed burst length, see
   `QSPIBurstWrite`, `memory.py`; the last one padded), and the `.sav` to 0x780000 if present.
3. UART 0x3: `enable=1, hold=1, flush=1` + `mbc`/`rom_mask`/`ram_mask` from the ROM header (same rules
   as `rom_config()` in `scripts/chromatic.py`).
4. UART 0x3: `enable=1, hold=0`: the game starts; close the menu.
5. Save: UART 0x7 read-back of `ram_size` bytes, written to `<game>.sav` (on menu open, on game
   exit and periodically when the save dirty flag is set).
6. Exit: UART 0x3 `enable=0, hold=1` then `hold=0` (back to the physical cartridge / menu).

## ESP32 side: options

| Option | What | Pros | Cons |
|--------|------|------|------|
| **A. ModRetro firmware** | ModRetro adds a Games menu, a flash FS partition and the protocol above to `mcu_fw` | Best integration, users keep official updates | Depends on ModRetro; their partition table changes (1MB app → app + FS) |
| **B. Open replacement firmware** | Open `mcu_fw` (ESP-IDF + LVGL) with the same menus (settings, palettes, battery, brightness, OSD over QSPI) + Games, installed with `esptool` over USB (backup/restore like the FPGA flasher) | Fully open, no dependency | Reimplement the whole menu; battery/power management and settings behavior must match; ModRetro updates would overwrite it |
| **C. No menu change** | Keep ModRetro's firmware; library selected from the PC (existing `chromatix-vcart` + `load-rom`) | Works today | Needs a PC, not "on the console" |

The FPGA side of the ESP32 protocol is fully known (it is in our gateware: UART packets in
`sysmon.py`, OSD/QSPI writes in `memory.py`/`video.py`), so option B is feasible without ModRetro
documentation. The work is mostly on the ESP32: menu UI, OSD rendering and power management.

## Recommendation
1. Implement the FPGA changes (they are small and useful for every option) and test them in
   simulation (extend `test/test_sysmon*.py`, `test/test_vcart.py`) and on hardware through the
   debug bridge, emulating the ESP32 sequence from the host.
2. Propose the protocol to ModRetro (option A) with this document and the reference gateware.
3. In parallel, prototype option B as a minimal firmware (Games list + load/save only, OSD over
   QSPI) installed as a **second app**: this needs an OTA partition table (factory 1MB + ota_0 +
   FS), validated first on the console with the ESP32 backup ready for restore.

## Open questions
- Physical check of the board for a microSD socket (not found in any source).
- `SDIO_LS` role (N5): unknown; a test build driving it low would show what it gates.
- ModRetro's interest in option A and their firmware update process (does the ModRetro updater
  rewrite the whole ESP32 flash, which would erase a game partition?).
