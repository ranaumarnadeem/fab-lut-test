// Testbench template, filled in by sim/run.py.
// Applies one stimulus word per 10 ns step (see configs/configs.py for the
// format), samples O/Co at 9 ns into flagged steps. Config latches are only
// ever written through FrameData/FrameStrobe.
// +STIM=<file> +NSTEPS=<n> [+FAULT=<id>]; faults.vh holds the force cases,
// applied at t = 1 ns and held for the whole run.
`timescale 1ns / 1ps
`default_nettype none

module tb;
    reg [63:0] stim [0:4095];
    reg [31:0] FD;
    reg [19:0] FS;
    reg [3:0] I;
    reg Ci, SR, EN, CLK;
    wire O, Co;

    lut_cell_top dut (
        .FrameData(FD[@FD_HI@:@FD_LO@]),
        .FrameStrobe(FS[@FS_HI@:@FS_LO@]),
        .I(I),
        .Ci(Ci),
        .SR(SR),
        .EN(EN),
        .UserCLK(CLK),
        .O(O),
        .Co(Co)
    );

    integer fault_id, nsteps, k;
    reg [8*512-1:0] stimfile;
    reg [63:0] w;

    initial begin
        FD = 0; FS = 0; I = 0; Ci = 0; SR = 0; EN = 0; CLK = 0;
        if (!$value$plusargs("STIM=%s", stimfile)) begin
            $display("ERROR missing +STIM");
            $finish;
        end
        if (!$value$plusargs("NSTEPS=%d", nsteps)) begin
            $display("ERROR missing +NSTEPS");
            $finish;
        end
        if (!$value$plusargs("FAULT=%d", fault_id)) fault_id = -1;
        $readmemh(stimfile, stim, 0, nsteps - 1);
        // Inject after the initial input values settle (EN = 0, UserCLK = 0)
        // and before the first step. Forcing at t = 0 would turn x -> 1 on a
        // clock net into a posedge while D is still x (sky130 dff UDP -> Q = x),
        // which a real stuck-at-1 clock never sees.
        #1;
`include "faults.vh"
        for (k = 0; k < nsteps; k = k + 1) begin
            w = stim[k];
            FD = w[31:0];
            FS = w[51:32];
            I = w[55:52];
            Ci = w[56];
            SR = w[57];
            EN = w[58];
            CLK = w[59];
            #9;
            if (w[60]) $display("S %b%b", O, Co);
            #1;
        end
        $display("END");
        $finish;
    end
endmodule
