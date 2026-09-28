//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// eReg_SavestateV (MiSTer bus_savestates.vhd) in Verilog for simulation: instantiated from Verilog
// with per-instance generics (not kept by the GHDL VHDL -> Verilog conversion).

module eReg_SavestateV #(
    parameter integer index = 0,
    parameter integer Adr   = 0,
    parameter integer upper = 0,
    parameter integer lower = 0,
    parameter [63:0]  def   = 64'd0
) (
    input  wire               clk,
    input  wire [63:0]        BUS_Din,
    input  wire [9:0]         BUS_Adr,
    input  wire               BUS_wren,
    input  wire               BUS_rst,
    output wire [63:0]        BUS_Dout,
    input  wire [upper:lower] Din,
    output wire [upper:lower] Dout
);
    reg  [upper:lower] Dout_buffer = def[upper:lower];
    wire               hit         = (BUS_Adr == Adr + index);

    always @(posedge clk)
        if (BUS_rst)
            Dout_buffer <= def[upper:lower];
        else if (hit & BUS_wren)
            Dout_buffer <= BUS_Din[upper:lower];

    assign Dout     = Dout_buffer;
    assign BUS_Dout = hit ? ({64'd0, Din} << lower) : 64'd0;
endmodule
