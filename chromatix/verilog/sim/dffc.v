//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// Gowin DFFC (D flip-flop with asynchronous clear) model for simulation.

module DFFC (
    input  wire D,
    input  wire CLK,
    input  wire CLEAR,
    output reg  Q
);
    initial Q = 1'b0;
    always @(posedge CLK or posedge CLEAR)
        if (CLEAR)
            Q <= 1'b0;
        else
            Q <= D;
endmodule
