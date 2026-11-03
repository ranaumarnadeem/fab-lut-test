// Simulation model of the branch buffers inserted by synth/insbuf_branches.py
// (netlist S only; netlist G gets $_BUF_ from simcells.v).
module \$_BUF_ (A, Y);
    input A;
    output Y;
    assign Y = A;
endmodule
