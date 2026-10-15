# Voxel game on the Chromatic

`--with-voxel` builds the CPU application SoC (`--with-app`: VexRiscv, main RAM in the PSRAM, PSRAM
frame buffers, PCM audio, buttons) with a **VoxelRenderer**: a hardware raycaster that draws the 3D
view of a Minecraft-like block world into the LCD frame buffers (so also over UVC). The game
(`firmware/voxel`) runs on the CPU: world generation, player physics, block picking/editing, hotbar,
day/night cycle, sound effects. Creative mode.

Status: see [Performance](#performance) and [Known issues / next](#known-issues--next).

## Architecture

```
CPU firmware (VexRiscv, 67MHz)                  VoxelRenderer gateware (sys = xClk)
- world 256x256x32 (1 byte/cell, 2MB, PSRAM)    - world window RAM: 32x32x32 cells x 4 bits (8 BSRAM),
  value-noise terrain, water, beaches, snow,      toroidal on X/Z: the CPU only writes the columns
  trees, ores; X/Z wrap around                    entering the window when the player moves/turns
- player: AABB collisions, gravity, jump, fly,  - textures: 16 x 16x16 x 8-bit texels (2 BSRAM)
  swim; block picking (1/16 cell ray march)     - per pixel: ray = fwd + (x-80)*right + (y-64)*down,
- camera vectors (Q14), window origin ahead       delta = 2^26/|d| (3 restoring dividers, 2 bits/
  of the camera, palette (day/night, fog)         cycle), 3D DDA (2 cycles/step, <= 64 steps), hit:
- hotbar (CPU-written HUD rows 128-143)           texel (t*d products, 3 shared DSPs), shade = face +
- sounds (noise bursts/sweep, PCM IRQ)            fog, sky gradient, selection edges, crosshair
                                                - 160-pixel lines written into the LCDPSRAMFramebuffer
                                                  line buffers (hardware port), copied to the PSRAM
                                                  back buffer as the CPU lines (palette lookup)
```

The 3D view is 160x128 (rows 0-127), the hotbar uses the bottom 16 rows.

## Bit exact reference model

`firmware/voxel/vr.c` is the reference model of the gateware and also the software renderer (PC
build, `--with-app` builds without the renderer, `--args sw`). `test/test_voxel.py` renders rows of
random scenes/cameras with both (Migen simulation vs `vr.c` through ctypes) and checks every pixel:
terrain, axis-aligned rays (zero direction components), selection, window across the world wrap,
sky. Any change of the rendering must be done in both.

Formats (`vr.h`):

| | |
|---|---|
| World window | 32x32x32 cells, 4-bit blocks (0: air), word `(y << 7) \| (z << 2) \| (x >> 3)`, nibble `x & 7`; X/Z toroidal (`cell & 31`) |
| Textures | 16 textures of 16x16 texels, 8-bit (bits 4:0: color, 0: transparent), word `(t << 6) \| (v << 2) \| (u >> 2)`, byte `u & 3` |
| Blocks | 16 entries: textures of the top (3:0), sides (7:4), bottom (11:8) |
| Camera | cell (X/Z mod 256, Y 0-31) + fraction (Q8), fwd/right/down vectors (Q14, fwd unit and orthogonal to right/down: t is the view depth) |
| t | Q12, 24-bit, saturated (`0xffffff`: never) |
| Pixel | `(color << 3) \| shade`, shade = min(7, face + max(0, t - fog_start) >> fog_shift), faces: top 0, Z 1, X 2, bottom 3 |
| Sky | `sky_base \| clamp(4 - (dy >> 12), 0, 7)` |
| Rays end | after t_far, out of the window `[origin, origin + 31]` (X/Z), out of the world height, after 64 steps: sky |

Palette (`art.c`): 32 colors x 8 shades (shades 0-3: darker faces, 4-7: fog toward the horizon
color), color 31: sky gradient (zenith to horizon), 28-30: UI. Textures are generated (no assets).

## Build and run

```bash
./chromatix.py --gowin-path ~/tools/gowin_1.9.12.04/IDE --with-voxel --build --output-dir build_voxel
./scripts/chromatix_flash.py flash build_voxel/gateware/chromatic.fs
make -C firmware/voxel BUILD_DIR=../../build_voxel
./scripts/chromatic.py --serial /dev/ttyACM0 run firmware/voxel/voxel.bin   # --args "seed=42", "sw"
./scripts/chromatic.py capture voxel.png
```

PC build (SDL, software renderer, same game code): `make -C firmware/voxel -f Makefile.host &&
firmware/voxel/voxel_host` (keys: arrows, X: A, Z: B, Enter: Start, Backspace: Select).
`VOXEL_SCRIPT`/`VOXEL_SNAP`/`VOXEL_FRAMES` script the buttons and save frames (tests, see
`hal_sdl.c`).

## Controls

| Button | Action |
|---|---|
| D-pad up/down | walk forward/back |
| D-pad left/right | turn |
| Select + D-pad | look up/down, strafe |
| A | place the hotbar block |
| B | break the targeted block |
| Start | jump (swim up in water; fly: up) |
| Start twice | fly on/off |
| Select + Start | fly: down |
| Select + A / B | next/previous hotbar block |

## Performance

| Step | fps | Render | Notes |
|---|---|---|---|
| Software renderer (CPU, `vr.c`) | | | |
| VoxelRenderer | | | 62.5 cycles/pixel in simulation (terrain view) |

## Resources

| | Logic | BSRAM | DSP |
|---|---|---|---|
| `--with-app` (synthesis) | 16751 | 44 | 4 + 21 small |
| VoxelRenderer alone (synthesis) | 2719 | 10 | 3 + 1 small |

The first renderer version used 12 multipliers (setup, hit, row base): with the CPU/shell DSPs the
GW5A-25's 28 DSP blocks overflowed and the multipliers went to logic (25.2K logic, did not fit).
The products are now shared (setup and hit never at the same time) and the row base uses shifts.

## Known issues / next
