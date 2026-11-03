// Reg-based, X-pessimistic models of every latch and flop type in the
// netlists. sim/state.py removes the original definitions from copies of
// synth/sky130_cells.v and synth/simcells.v and compiles these instead.
//
// Why: the state must be depositable with force/release (a sequential UDP
// keeps its state where no testbench can reach it), and an X enable or X
// clock edge must give X unless D already equals Q (the yosys simcells treat
// an X enable as "not enabled", which is optimistic).
//
// The sky130 modules reproduce the UDP tables of
// sky130_fd_sc_hd__udp_dlatch$P and sky130_fd_sc_hd__udp_dff$P row by row,
// including the "reduce pessimism" rows. Port order matches the UDPs.

// Q keeps its value when D == Q is a defined value, else becomes x.
`define SEQ_KEEP_OR_X(Q, D) if (!(((D) === 1'b0 && (Q) === 1'b0) || ((D) === 1'b1 && (Q) === 1'b1))) Q = 1'bx

module sky130_fd_sc_hd__udp_dlatch$P (Q, D, GATE);
    output Q;
    input D, GATE;
    reg Q;
    initial Q = 1'bx;
    always @(D or GATE) begin
        if (GATE === 1'b1) Q = D;            // 0 1 : ? : 0 / 1 1 : ? : 1, x -> x
        else if (GATE !== 1'b0) `SEQ_KEEP_OR_X(Q, D);  // 0 x : 0 : 0 / 1 x : 1 : 1
        // GATE = 0: hold
    end
endmodule

module sky130_fd_sc_hd__udp_dff$P (Q, D, CLK);
    output Q;
    input D, CLK;
    reg Q, clk_q;
    initial begin
        Q = 1'bx;
        clk_q = 1'bx;
    end
    always @(CLK) begin
        if (clk_q === 1'b0 && CLK === 1'b1) Q = D;        // (01): D, x -> x
        else if ((clk_q !== 1'b0 && clk_q !== 1'b1 && CLK === 1'b1)    // (x1)
                 || (clk_q === 1'b0 && CLK !== 1'b0 && CLK !== 1'b1))  // (0x)
            `SEQ_KEEP_OR_X(Q, D);
        // (1x), (?0): hold. Data edges never change Q.
        clk_q = CLK;
    end
endmodule

module \$_DLATCH_P_ (E, D, Q);
    input E, D;
    output Q;
    reg Q;
    initial Q = 1'bx;
    always @(D or E) begin
        if (E === 1'b1) Q = D;
        else if (E !== 1'b0) `SEQ_KEEP_OR_X(Q, D);
    end
endmodule

module \$_DFFE_PP_ (D, C, E, Q);
    input D, C, E;
    output Q;
    reg Q, c_q;
    initial begin
        Q = 1'bx;
        c_q = 1'bx;
    end
    always @(C) begin
        if (c_q === 1'b0 && C === 1'b1) begin                 // definite posedge
            if (E === 1'b1) Q = D;
            else if (E !== 1'b0) `SEQ_KEEP_OR_X(Q, D);
        end else if ((c_q !== 1'b0 && c_q !== 1'b1 && C === 1'b1)
                     || (c_q === 1'b0 && C !== 1'b0 && C !== 1'b1)) begin  // possible posedge
            if (E !== 1'b0) `SEQ_KEEP_OR_X(Q, D);
        end
        c_q = C;
    end
endmodule
