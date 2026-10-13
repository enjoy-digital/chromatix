//
// This file is part of ChromatiX.
//
// Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
// SPDX-License-Identifier: BSD-2-Clause
//
// opll (YM2413 FM) stub for the --with-core sms build. The real FM is the VHDL VM2413 (vm2413/);
// it is a Japanese-Mark-III / SMS-FM-unit add-on that the vast majority of SMS games and all Game
// Gear games do not use. This stub ties the FM mix to silence so the core builds without the VHDL
// (saving area); drop in the real vm2413 opll later for FM-audio titles. Ports match the
// instantiation in system.v (`opll fm(.xin,.xena,.d,.a,.cs_n,.we_n,.ic_n,.mixout)`).
//
module opll(
    input         xin,
    input         xena,
    input  [7:0]  d,
    input         a,
    input         cs_n,
    input         we_n,
    input         ic_n,
    output [13:0] mixout
);
    assign mixout = 14'd0;
endmodule
