//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// NES core debug trace (simulation only, --core nes): logs the CPU PRG-fetch bus and, periodically,
// the raster position and the last CPU PC, to nes_trace.log. Finds where a hosted NES core gets
// stuck (a tight loop shows as the CPU address cycling a small range; the raster log shows whether
// the PPU scanline advances toward vblank at scanline 241). Self-terminates after +nes_ticks NES
// cycles so debug runs are short and deterministic, independent of the display's vsync.

module nes_trace(
    input         clk,       // NES clock.
    input         reset,
    input  [21:0] cpu_addr,  // PRG fetch/read address (cpumem).
    input         cpu_read,
    input  [7:0]  cpu_din,   // PRG byte returned.
    input  [5:0]  color,     // PPU pixel (palette index).
    input  [8:0]  cycle,
    input  [8:0]  scanline
);
    integer    f;
    integer    n_reads  = 0;
    reg [31:0] ticks    = 0;
    reg [8:0]  max_scan = 0;
    reg [21:0] last_pc  = 0;   // Last PRG fetch address (approximates the CPU PC).
    reg [5:0]  prev_color = 0;
    reg        varied   = 0;   // Pixel varied within a frame (rendering active).

    integer    nes_ticks = 800000;
    initial begin
        f = $fopen("nes_trace.log", "w");
        if (!$value$plusargs("nes_ticks=%d", nes_ticks)) nes_ticks = 800000;
    end

    always @(posedge clk) begin
        ticks = ticks + 1;
        if (cpu_read) last_pc <= cpu_addr;
        if (scanline > max_scan) max_scan <= scanline;
        if (scanline < 240 && cycle < 256) begin
            if (color != prev_color) varied <= 1;
            prev_color <= color;
        end

        // First CPU PRG reads (the init path; a tight loop shows as repeated addresses).
        if (cpu_read && n_reads < 4000) begin
            $fwrite(f, "r %0d %05x %02x\n", n_reads, cpu_addr, cpu_din);
            n_reads = n_reads + 1;
        end

        // Periodic raster + PC snapshot.
        if (ticks[13:0] == 0)
            $fwrite(f, "raster t=%0d scan=%0d cyc=%0d pc=%05x color=%0d max_scan=%0d varied=%0d\n",
                ticks, scanline, cycle, last_pc, color, max_scan, varied);

        // Self-terminate.
        if (ticks > nes_ticks) begin
            $fwrite(f, "done t=%0d max_scan=%0d varied=%0d\n", ticks, max_scan, varied);
            $fclose(f);
            $finish;
        end
    end
endmodule
