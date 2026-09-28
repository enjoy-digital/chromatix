//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Scripted Game Boy buttons for simulation: events loaded from "buttons.hex" (one per line, sorted,
// {frame[23:0], buttons[7:0]}, buttons = {right, left, down, up, start, select, b, a}), applied at the
// vsync starting the frame. A missing/empty file (or 0xffffffff) ends the events.

module gb_buttons (
    input  wire       clk,
    input  wire       vsync,
    output reg  [7:0] buttons = 8'd0
);
    localparam integer EVENTS = 1024;

    reg     [31:0] events [0:EVENTS-1];
    reg            vsync_d = 1'b0;
    integer        frame   = 0;
    integer        index   = 0;
    integer        i;

    initial begin
        for (i = 0; i < EVENTS; i = i + 1)
            events[i] = 32'hffffffff;
        $readmemh("buttons.hex", events);
    end

    always @(posedge clk) begin
        vsync_d <= vsync;
        if (vsync & ~vsync_d) begin
            frame = frame + 1;
            while ((index < EVENTS) && (events[index] != 32'hffffffff) && (events[index][31:8] == frame)) begin
                buttons <= events[index][7:0];
                index    = index + 1;
            end
        end
    end
endmodule
