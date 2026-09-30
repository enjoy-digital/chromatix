//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Game Boy cartridge RAM for simulation (128KB): content loaded from "sim_cart_ram.init" ($readmemh,
// written at runtime from the save file) and written to "sim_cart_ram.out" at the end of the
// simulation (Verilog final block: window closed, frames reached), to update the save file.

module gb_cart_ram #(
    parameter integer SIZE = 131072
) (
    input  wire        clk,
    input  wire [16:0] adr,
    input  wire        we,
    input  wire  [7:0] dat_w,
    output wire  [7:0] dat_r
);
    reg [7:0] mem [0:SIZE-1];

    initial $readmemh("sim_cart_ram.init", mem);

    always @(posedge clk)
        if (we)
            mem[adr] <= dat_w;

    assign dat_r = mem[adr];

    final $writememh("sim_cart_ram.out", mem);
endmodule
