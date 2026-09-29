//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Video pipeline output capture for simulation (gClk): the UVC copy of the ST7785 panel scan (RGB666,
// one pixel every 3 dot clocks while EN, lines started by the HSYNC low pulse, frames by the VSYNC
// low pulse) written as binary PPM files (uvc_NNNN.ppm, every EVERY panel frames, +every=N plusarg).

module uvc_capture #(
    parameter integer EVERY  = 1,
    parameter integer WIDTH  = 160,
    parameter integer HEIGHT = 144
) (
    input wire        clk,
    input wire        hsync,
    input wire        vsync,
    input wire        en,
    input wire [17:0] db
);
    reg     [17:0] fb [0:WIDTH*HEIGHT-1];
    reg            hsync_d = 1'b1;
    reg            vsync_d = 1'b1;
    reg            line_en = 1'b0;
    integer        x       = 0;
    integer        y       = -1;
    integer        phase   = 0;
    integer        frame   = 0;
    integer        lines   = 0;
    integer        every   = EVERY;
    integer        i;
    integer        f;
    reg [8*64-1:0] name;

    initial begin
        if ($value$plusargs("every=%d", every)) ;
        for (i = 0; i < WIDTH*HEIGHT; i = i + 1)
            fb[i] = 18'd0;
    end

    function [7:0] expand(input [5:0] c);
        expand = {c, c[5:4]};
    endfunction

    always @(posedge clk) begin
        hsync_d <= hsync;
        vsync_d <= vsync;
        // Frame start: previous frame written.
        if (~vsync & vsync_d) begin
            if ((lines > 0) && (frame % every == 0)) begin
                $sformat(name, "uvc_%04d.ppm", frame);
                f = $fopen(name, "wb");
                $fwrite(f, "P6\n%0d %0d\n255\n", WIDTH, HEIGHT);
                for (i = 0; i < WIDTH*HEIGHT; i = i + 1)
                    $fwrite(f, "%c%c%c", expand(fb[i][5:0]), expand(fb[i][11:6]), expand(fb[i][17:12]));
                $fclose(f);
            end
            if (lines > 0)
                frame = frame + 1;
            lines = 0;
            y     = -1;
        end
        // Line start.
        if (~hsync & hsync_d) begin
            x       = 0;
            phase   = 0;
            line_en = 1'b0;
        end
        // Pixels (sampled in the middle of the 3 dot clocks).
        if (en) begin
            if (~line_en) begin
                line_en = 1'b1;
                y       = y + 1;
                lines   = lines + 1;
            end
            if (phase == 1 && x < WIDTH && y >= 0 && y < HEIGHT)
                fb[y*WIDTH + x] <= db;
            phase = phase + 1;
            if (phase == 3) begin
                phase = 0;
                x     = x + 1;
            end
        end
    end
endmodule
