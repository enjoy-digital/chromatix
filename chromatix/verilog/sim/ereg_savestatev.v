//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: GPL-3.0-only
// Derived from the MiSTer Game Boy core (bus_savestates.vhd, GPL).
//
// eReg_SavestateV (MiSTer bus_savestates.vhd) in Verilog for simulation: instantiated from Verilog
// with per-instance generics (not kept by the GHDL VHDL -> Verilog conversion). Savestates are not
// used in simulation: the register holds its default value and the savestate bus is idle (folded
// by Verilator, faster simulation).

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
    assign Dout     = def[upper:lower];
    assign BUS_Dout = 64'd0;
endmodule
