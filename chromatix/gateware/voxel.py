#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""
VoxelRenderer: hardware 3D view of a voxel world (raycasting, 1 ray per pixel), written as 8-bit
indexed lines to the LCDPSRAMFramebuffer line buffers (hardware port).

Bit exact with the reference model (firmware/voxel/vr.c, see vr.h for the formats), per pixel:
- Ray: d = fwd + (x - 80)*right + (y - 64)*down (clamped to +-32767), ray setup: delta = 2^26/|d|
  (restoring division, div_bits per cycle, saturated to TINF = 2^24 - 1), first boundaries
  tmax = dist*delta >> 8.
- 3D DDA (Amanatides & Woo), 2 cycles per step (world RAM read), up to 64 steps: the ray ends
  (sky) after t_far, out of the window (X/Z) or of the world height (Y).
- Hit: position on the face (t*d >> 18 + fraction), texel (texture RAM read), transparent texels
  continue the ray, shade = face level + fog, selected block edges/crosshair.

Memories (bus, write only, 32-bit words): world window (4096 words: 32x32x32 voxels, 4-bit),
textures (1024 words: 16 textures of 16x16 8-bit texels), block textures (16 words: top/side/
bottom textures).
"""

from migen import *

from litex.gen import *

from litex.soc.interconnect import wishbone
from litex.soc.interconnect.csr import *

# Constants (vr.h) ---------------------------------------------------------------------------------

VR_WIDTH     = 160
VR_HEIGHT    = 128
VR_CX        = 80
VR_CY        = 64
VR_WINDOW    = 32
VR_MAX_STEPS = 64
VR_TINF      = 0xffffff

FACE_TOP, FACE_Z, FACE_X, FACE_BOTTOM = 0, 1, 2, 3

WORLD_WORDS   = VR_WINDOW**3//8
TEXTURE_WORDS = 16*16*16//4

# Voxel Renderer -----------------------------------------------------------------------------------

class VoxelRenderer(LiteXModule):
    """
    Renders rows [rows.first, rows.first + rows.count) of the 3D view in the `control.buffer` frame
    buffer when `control.start` is written (status.busy until done, status cycles: render time).

    - bus: memories (word addresses: world 0x0000-0x0fff, textures 0x1000-0x13ff, blocks 0x1800-
      0x180f), region size 0x8000 bytes.
    - fb_wr/fb_line/fb_busy: LCDPSRAMFramebuffer hardware port (hw_wr, hw_line, busy).
    """
    def __init__(self, nslots=4, line_words=40, div_bits=2, world_init=None, textures_init=None):
        assert 24 % div_bits == 0
        self.bus = bus = wishbone.Interface(data_width=32, address_width=32, addressing="word")

        # Frame buffer hardware port.
        self.fb_wr   = Record([("we", 1), ("adr", bits_for(nslots*line_words - 1)), ("dat", 32)])
        self.fb_line = Record([("valid", 1), ("line", 8), ("buffer", 2), ("slot", 2)])
        self.fb_busy = Signal(nslots)

        # CSRs.
        self.control = CSRStorage(fields=[
            CSRField("start",  size=1, pulse=True, description="Start a render."),
            CSRField("buffer", size=2, description="Frame buffer."),
            CSRField("slot",   size=2, description="First line buffer (the line buffers are copied in order: continues the CPU's sequence)."),
        ])
        self.rows = CSRStorage(fields=[
            CSRField("first", size=8, reset=0,         description="First row."),
            CSRField("count", size=8, reset=VR_HEIGHT, description="Rows."),
        ])
        self.cell = CSRStorage(fields=[
            CSRField("x", size=8), CSRField("y", size=8), CSRField("z", size=8),
        ], description="Camera cell (X/Z: world cell mod 256, Y: 0-31).")
        self.frac = CSRStorage(fields=[
            CSRField("x", size=8), CSRField("y", size=8), CSRField("z", size=8),
        ], description="Camera position in the cell (Q8).")
        vectors = {}
        for name in ["fwd", "right", "down"]:
            xy = CSRStorage(fields=[CSRField("x", size=16), CSRField("y", size=16)], name=f"{name}_xy",
                description=f"{name} vector X/Y (Q14).")
            z  = CSRStorage(fields=[CSRField("z", size=16)], name=f"{name}_z",
                description=f"{name} vector Z (Q14).")
            setattr(self, f"_{name}_xy", xy)
            setattr(self, f"_{name}_z",  z)
            vectors[name] = [xy.fields.x, xy.fields.y, z.fields.z]
        self.window = CSRStorage(fields=[
            CSRField("x", size=8), CSRField("z", size=8),
        ], description="Window origin (world cells).")
        self.t_far     = CSRStorage(24, description="Rays end (Q12).")
        self.fog_start = CSRStorage(24, description="Fog start (Q12).")
        self.fog = CSRStorage(fields=[
            CSRField("shift",    size=5, description="+1 shade every 2^shift (Q12)."),
            CSRField("sky_base", size=8, description="Sky palette base (8 levels)."),
        ])
        self.select = CSRStorage(fields=[
            CSRField("enable", size=1), CSRField("x", size=8), CSRField("y", size=8), CSRField("z", size=8),
        ], description="Selected block (edges highlighted).")
        self.colors = CSRStorage(fields=[
            CSRField("select",       size=8, description="Selected block edges color."),
            CSRField("cross_enable", size=1, description="Crosshair enable."),
            CSRField("cross",        size=8, description="Crosshair color."),
        ])
        self.status = CSRStatus(fields=[
            CSRField("busy", size=1, description="Render in progress."),
        ])
        self.cycles = CSRStatus(32, description="Last render duration (sys cycles).")

        # # #

        # Memories ---------------------------------------------------------------------------------
        world    = Memory(32, WORLD_WORDS,   init=world_init)
        world_wr = world.get_port(write_capable=True)
        world_rd = world.get_port()
        tex      = Memory(32, TEXTURE_WORDS, init=textures_init)
        tex_wr   = tex.get_port(write_capable=True)
        tex_rd   = tex.get_port()
        self.specials += world, world_wr, world_rd, tex, tex_wr, tex_rd
        blocks = Array(Signal(12, name=f"block{i}") for i in range(16))

        # Wishbone (writes only, reads return 0).
        is_world  = Signal()
        is_tex    = Signal()
        is_blocks = Signal()
        write     = Signal()
        self.comb += [
            write.eq(bus.cyc & bus.stb & bus.we & ~bus.ack),
            is_world.eq(~bus.adr[12]),
            is_tex.eq(bus.adr[12] & ~bus.adr[11]),
            is_blocks.eq(bus.adr[12] & bus.adr[11]),
            world_wr.adr.eq(bus.adr[:12]),
            world_wr.dat_w.eq(bus.dat_w),
            world_wr.we.eq(write & is_world),
            tex_wr.adr.eq(bus.adr[:10]),
            tex_wr.dat_w.eq(bus.dat_w),
            tex_wr.we.eq(write & is_tex),
        ]
        self.sync += [
            If(write & is_blocks, blocks[bus.adr[:4]].eq(bus.dat_w[:12])),
            bus.ack.eq(bus.cyc & bus.stb & ~bus.ack),
        ]

        # Parameters -------------------------------------------------------------------------------
        fwd   = [Signal((16, True), name=f"fwd{i}")   for i in range(3)]
        right = [Signal((16, True), name=f"right{i}") for i in range(3)]
        down  = [Signal((16, True), name=f"down{i}")  for i in range(3)]
        for i in range(3):
            self.comb += [
                fwd[i].eq(vectors["fwd"][i]),
                right[i].eq(vectors["right"][i]),
                down[i].eq(vectors["down"][i]),
            ]
        cam_cell = [self.cell.fields.x, self.cell.fields.y, self.cell.fields.z]
        cam_frac = [self.frac.fields.x, self.frac.fields.y, self.frac.fields.z]

        # State --------------------------------------------------------------------------------------
        x       = Signal(8)
        y       = Signal(8)
        y_last  = Signal(8)
        slot    = Signal(2)
        rowbase = [Signal((27, True), name=f"rowbase{i}") for i in range(3)]
        dacc    = [Signal((27, True), name=f"dacc{i}")    for i in range(3)]
        d       = [Signal((16, True), name=f"d{i}")       for i in range(3)]
        m       = [Signal(15, name=f"m{i}")               for i in range(3)]
        rem     = [Signal(17, name=f"rem{i}")             for i in range(3)]
        quo     = [Signal(24, name=f"quo{i}")             for i in range(3)]
        div_cnt = Signal(max=24//div_bits + 1)
        delta   = [Signal(24, name=f"delta{i}")           for i in range(3)]
        tmax    = [Signal(24, name=f"tmax{i}")            for i in range(3)]
        prod    = [Signal((41, True), name=f"prod{i}")    for i in range(3)]
        c       = [Signal(8, name="c0"), Signal((10, True), name="c1"), Signal(8, name="c2")]
        n       = Signal(max=VR_MAX_STEPS + 1)
        hit_t   = Signal(24)
        hit_a   = Signal(2)
        hit_v   = Signal(4)
        nibble  = Signal(3)
        pixel   = Signal(8)
        cycles  = Signal(32)
        busy    = Signal()

        # Texel stage registers.
        texel_byte = Signal(2)
        highlight  = Signal()
        level      = Signal(2)

        self.comb += self.status.fields.busy.eq(busy)
        self.sync += If(busy, cycles.eq(cycles + 1))

        # Helpers ----------------------------------------------------------------------------------
        def sat(v):
            return Mux(v > VR_TINF, VR_TINF, v)

        def step_of(i):
            # +1/-1/0 (d sign).
            return Mux(d[i] > 0, 1, Mux(d[i] < 0, -1, 0))

        # DDA step (select the nearest boundary, step, checks), from STEP and CHECK/TEXEL (continue).
        sel   = Signal(2)
        t_sel = Signal(24)
        nc    = [Signal(8, name="nc0"), Signal((10, True), name="nc1"), Signal(8, name="nc2")]
        out   = Signal()
        ntmax = Signal(25)
        self.comb += [
            If((tmax[0] <= tmax[1]) & (tmax[0] <= tmax[2]),
                sel.eq(0),
            ).Elif(tmax[1] <= tmax[2],
                sel.eq(1),
            ).Else(
                sel.eq(2),
            ),
            t_sel.eq(Array(tmax)[sel]),
            ntmax.eq(Array(tmax)[sel] + Array(delta)[sel]),
            nc[0].eq(c[0] + Mux(sel == 0, step_of(0), 0)),
            nc[1].eq(c[1] + Mux(sel == 1, step_of(1), 0)),
            nc[2].eq(c[2] + Mux(sel == 2, step_of(2), 0)),
            out.eq((nc[1] < 0) | (nc[1] >= VR_WINDOW) |
                   ((nc[0] - self.window.fields.x)[5:8] != 0) |
                   ((nc[2] - self.window.fields.z)[5:8] != 0)),
        ]

        def dda_step():
            return [
                If((n == VR_MAX_STEPS) | (t_sel > self.t_far.storage),
                    NextState("SKY"),
                ).Else(
                    NextValue(n, n + 1),
                    NextValue(hit_t, t_sel),
                    NextValue(hit_a, sel),
                    NextValue(c[0], nc[0]),
                    NextValue(c[1], nc[1]),
                    NextValue(c[2], nc[2]),
                    *[If(sel == i, NextValue(tmax[i], sat(ntmax))) for i in range(3)],
                    NextValue(world_rd.adr, Cat(nc[0][3:5], nc[2][:5], nc[1][:5])),
                    NextValue(nibble, nc[0][:3]),
                    If(out,
                        NextState("SKY"),
                    ).Else(
                        NextState("READ"),
                    )
                )
            ]

        # Pixel/line output (4 pixels per word, little endian).
        pixels = Signal(24)
        self.comb += [
            self.fb_line.line.eq(y),
            self.fb_line.buffer.eq(self.control.fields.buffer),
            self.fb_line.slot.eq(slot),
            self.fb_wr.adr.eq(slot*line_words + x[2:]),
            self.fb_wr.dat.eq(Cat(pixels, pixel)),
        ]

        # Hit: position on the face, texture coordinates, texture.
        pos  = [Signal(8, name=f"pos{i}") for i in range(3)]
        u    = Signal(4)
        w    = Signal(4)
        tsel = Signal(4)
        lvl  = Signal(2)
        btex = Signal(12)
        self.comb += [
            [pos[i].eq(cam_frac[i] + prod[i][18:26]) for i in range(3)],
            btex.eq(blocks[hit_v]),
            Case(hit_a, {
                0: [u.eq(pos[2][4:]), w.eq(15 - pos[1][4:]), lvl.eq(FACE_X), tsel.eq(btex[4:8])],
                2: [u.eq(pos[0][4:]), w.eq(15 - pos[1][4:]), lvl.eq(FACE_Z), tsel.eq(btex[4:8])],
                "default": If(d[1] < 0,
                    u.eq(pos[0][4:]), w.eq(pos[2][4:]), lvl.eq(FACE_TOP), tsel.eq(btex[0:4]),
                ).Else(
                    u.eq(pos[0][4:]), w.eq(pos[2][4:]), lvl.eq(FACE_BOTTOM), tsel.eq(btex[8:12]),
                )
            }),
        ]

        # Shade: face level + fog.
        texel = Signal(8)
        fog   = Signal(24)
        shade = Signal(4)
        self.comb += [
            texel.eq(Array(tex_rd.dat_r[8*i:8*(i + 1)] for i in range(4))[texel_byte]),
            If(hit_t > self.fog_start.storage,
                fog.eq((hit_t - self.fog_start.storage) >> self.fog.fields.shift),
            ),
            If((fog > 7) | ((level + fog[:3]) > 7),
                shade.eq(7),
            ).Else(
                shade.eq(level + fog[:3]),
            ),
        ]

        # Sky.
        sky = Signal((5, True))
        self.comb += sky.eq(4 - (d[1] >> 12))

        # Crosshair.
        cross = Signal()
        self.comb += cross.eq(self.colors.fields.cross_enable & (
            ((y == VR_CY) & (x >= VR_CX - 2) & (x <= VR_CX + 2)) |
            ((x == VR_CX) & (y >= VR_CY - 2) & (y <= VR_CY + 2))))

        # FSM ----------------------------------------------------------------------------------------
        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.control.fields.start,
                NextValue(busy, 1),
                NextValue(cycles, 0),
                NextValue(y, self.rows.fields.first),
                NextValue(slot, self.control.fields.slot),
                NextValue(y_last, self.rows.fields.first + self.rows.fields.count - 1),
                NextState("INIT"),
            )
        )
        # Row base: fwd - 64*down - 80*right (shifts), then + first*down (1 row per cycle).
        assert VR_CY == 64 and VR_CX == 80
        row_k = Signal(8)
        fsm.act("INIT",
            *[NextValue(rowbase[i], fwd[i] - (down[i] << 6) - (right[i] << 6) - (right[i] << 4))
                for i in range(3)],
            NextValue(row_k, 0),
            NextState("INIT_ROWS"),
        )
        fsm.act("INIT_ROWS",
            If(row_k == y,
                NextState("ROW"),
            ).Else(
                NextValue(row_k, row_k + 1),
                *[NextValue(rowbase[i], rowbase[i] + down[i]) for i in range(3)],
            )
        )
        fsm.act("ROW",
            *[NextValue(dacc[i], rowbase[i]) for i in range(3)],
            NextValue(x, 0),
            If(~Array(self.fb_busy[i] for i in range(nslots))[slot],
                NextState("PIXEL"),
            )
        )
        # Ray direction (clamped), |d|.
        dclamp = [Signal((16, True), name=f"dclamp{i}") for i in range(3)]
        for i in range(3):
            self.comb += If(dacc[i] > 32767,
                dclamp[i].eq(32767),
            ).Elif(dacc[i] < -32767,
                dclamp[i].eq(-32767),
            ).Else(
                dclamp[i].eq(dacc[i]),
            )
        fsm.act("PIXEL",
            *[NextValue(d[i], dclamp[i]) for i in range(3)],
            *[NextValue(m[i], Mux(dclamp[i] < 0, -dclamp[i], dclamp[i])) for i in range(3)],
            *[NextValue(rem[i], 4) for i in range(3)],
            *[NextValue(quo[i], 0) for i in range(3)],
            NextValue(div_cnt, 0),
            If(cross,
                NextValue(pixel, self.colors.fields.cross),
                NextState("OUT"),
            ).Else(
                NextState("DIV"),
            )
        )
        # delta = 2^26/m (m >= 5, else TINF): restoring division of the remaining 24 zero bits.
        for i in range(3):
            r = rem[i]
            q = quo[i]
            stages = []
            for b in range(div_bits):
                r2 = Signal(18, name=f"div{i}_r{b}")
                rn = Signal(17, name=f"div{i}_rn{b}")
                qb = Signal(name=f"div{i}_q{b}")
                self.comb += [
                    r2.eq(Cat(Constant(0, 1), r)),
                    If(r2 >= m[i],
                        rn.eq(r2 - m[i]),
                        qb.eq(1),
                    ).Else(
                        rn.eq(r2),
                    )
                ]
                stages.append(qb)
                r = rn
            fsm.act("DIV",
                NextValue(rem[i], r),
                NextValue(quo[i], Cat(*reversed(stages), quo[i])),
            )
        fsm.act("DIV",
            NextValue(div_cnt, div_cnt + 1),
            If(div_cnt == 24//div_bits - 1,
                NextState("DELTA"),
            )
        )
        fsm.act("DELTA",
            *[NextValue(delta[i], Mux(m[i] <= 4, VR_TINF, quo[i])) for i in range(3)],
            NextState("TMAX0"),
        )
        # tmax = dist*delta >> 8 (dist: to the first boundary, Q8).
        dist = [Signal(9, name=f"dist{i}") for i in range(3)]
        for i in range(3):
            self.comb += dist[i].eq(Mux(d[i] > 0, 256 - cam_frac[i], cam_frac[i]))
        # Multipliers (3, shared): dist*delta (TMAX0), hit_t*d (HIT0).
        mul_a = [Signal(24, name=f"mul_a{i}")         for i in range(3)]
        mul_b = [Signal((17, True), name=f"mul_b{i}") for i in range(3)]
        for i in range(3):
            self.comb += If(fsm.ongoing("TMAX0"),
                mul_a[i].eq(delta[i]),
                mul_b[i].eq(dist[i]),
            ).Else(
                mul_a[i].eq(hit_t),
                mul_b[i].eq(d[i]),
            )
            self.sync += prod[i].eq(mul_a[i]*mul_b[i])
        fsm.act("TMAX0",
            NextState("TMAX1"),
        )
        fsm.act("TMAX1",
            *[NextValue(tmax[i], Mux(d[i] == 0, VR_TINF, sat(prod[i][8:33]))) for i in range(3)],
            NextValue(c[0], cam_cell[0]),
            NextValue(c[1], cam_cell[1]),
            NextValue(c[2], cam_cell[2]),
            NextValue(n, 0),
            NextState("STEP"),
        )
        fsm.act("STEP", dda_step())
        fsm.act("READ", NextState("CHECK"))
        voxel = Signal(4)
        self.comb += voxel.eq(Array(world_rd.dat_r[4*i:4*(i + 1)] for i in range(8))[nibble])
        fsm.act("CHECK",
            If(voxel == 0,
                dda_step(),
            ).Else(
                NextValue(hit_v, voxel),
                NextState("HIT0"),
            )
        )
        # Hit: products t*d (position on the face).
        fsm.act("HIT0",
            NextState("HIT1"),
        )
        # Texture coordinates, selection, texture read.
        fsm.act("HIT1",
            NextValue(tex_rd.adr, Cat(u[2:], w, tsel)),
            NextValue(texel_byte, u[:2]),
            NextValue(level, lvl),
            NextValue(highlight, self.select.fields.enable &
                (c[0] == self.select.fields.x) & (c[1] == self.select.fields.y) &
                (c[2] == self.select.fields.z) &
                ((u == 0) | (u == 15) | (w == 0) | (w == 15))),
            NextState("HIT2"),
        )
        fsm.act("HIT2", NextState("TEXEL"))
        fsm.act("TEXEL",
            If(highlight,
                NextValue(pixel, self.colors.fields.select),
                NextState("OUT"),
            ).Elif(texel[:5] == 0,
                dda_step(), # Transparent: the ray continues.
            ).Else(
                NextValue(pixel, Cat(shade[:3], texel[:5])),
                NextState("OUT"),
            )
        )
        fsm.act("SKY",
            If(sky < 0,
                NextValue(pixel, self.fog.fields.sky_base),
            ).Elif(sky > 7,
                NextValue(pixel, self.fog.fields.sky_base | 7),
            ).Else(
                NextValue(pixel, self.fog.fields.sky_base | sky[:3]),
            ),
            NextState("OUT"),
        )
        fsm.act("OUT",
            self.fb_wr.we.eq(x[:2] == 3),
            NextValue(pixels, Cat(pixels[8:], pixel)),
            *[NextValue(dacc[i], dacc[i] + right[i]) for i in range(3)],
            NextValue(x, x + 1),
            If(x == VR_WIDTH - 1,
                NextState("LINE"),
            ).Else(
                NextState("PIXEL"),
            )
        )
        fsm.act("LINE",
            self.fb_line.valid.eq(1),
            NextValue(slot, slot + 1),
            NextValue(y, y + 1),
            *[NextValue(rowbase[i], rowbase[i] + down[i]) for i in range(3)],
            If(y == y_last,
                NextValue(busy, 0),
                NextValue(self.cycles.status, cycles),
                NextState("IDLE"),
            ).Else(
                NextState("ROW"),
            )
        )
