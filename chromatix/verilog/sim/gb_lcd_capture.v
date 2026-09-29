//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Game Boy LCD capture for simulation: writes the 160x144 frames (RGB555, R in the LSBs) as binary
// PPM files (frame_NNNN.ppm, every EVERY frames) and ends the simulation after FRAMES frames (both
// can be overridden with the +frames=N/+every=N plusargs). Frames with less than 160x144 pixels (LCD
// off, first partial frame) are written black.

module gb_lcd_capture #(
    parameter integer FRAMES = 60,
    parameter integer EVERY  = 1
) (
    input wire        clk,
    input wire        clkena,
    input wire [14:0] data,
    input wire        vsync
);
    localparam integer PIXELS = 160*144;

    reg     [14:0] fb [0:PIXELS-1];
    reg            vsync_d = 1'b0;
    integer        n       = 0;
    integer        frame   = 0;
    integer        i;
    integer        f;
    reg [8*64-1:0] name;
    reg     [19:0] cycles  = 20'd0;
    integer        frames  = FRAMES;
    integer        every   = EVERY;
    reg     [14:0] mask;

    initial begin
        if ($value$plusargs("frames=%d", frames)) ;
        if ($value$plusargs("every=%d",  every))  ;
    end

    function [7:0] expand(input [4:0] c);
        expand = {c, c[4:2]};
    endfunction

    always @(posedge clk) begin
        // Progress.
        cycles <= cycles + 20'd1;
        if (cycles == 20'd0)
            $display("[gb_lcd_capture] frame %0d, %0d pixels", frame, n);
        vsync_d <= vsync;
        if (vsync & ~vsync_d) begin
            if (frame % every == 0) begin
                $sformat(name, "frame_%04d.ppm", frame);
                f = $fopen(name, "wb");
                $fwrite(f, "P6\n160 144\n255\n");
                // Incomplete frames black (masked at runtime: Verilator writes nothing for constant 0 %c).
                mask = (n == PIXELS) ? 15'h7fff : 15'h0000;
                for (i = 0; i < PIXELS; i = i + 1)
                    $fwrite(f, "%c%c%c", expand(fb[i][4:0] & mask[4:0]), expand(fb[i][9:5] & mask[9:5]),
                        expand(fb[i][14:10] & mask[14:10]));
                $fclose(f);
            end
            frame = frame + 1;
            n     = 0;
            if (frame >= frames)
                $finish;
        end else if (clkena) begin
            if (n < PIXELS)
                fb[n] <= data;
            n = n + 1;
        end
    end
endmodule
