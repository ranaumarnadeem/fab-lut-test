// $_XOR_ (DSP adders) as AND / OR / NOT, as synth/mux_decompose.v does for $_MUX_:
// the fast kernel and the X models know AND, OR, NOT, BUF, latch, flop only.
(* techmap_celltype = "$_XOR_" *)
module _xor_decompose (input A, input B, output Y);
    assign Y = (A & ~B) | (~A & B);
endmodule
