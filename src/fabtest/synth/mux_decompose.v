// techmap: $_MUX_ (Y = S ? B : A) as NOT / AND / OR primitives.
(* techmap_celltype = "$_MUX_" *)
module _mux_decompose (input A, input B, input S, output Y);
    wire sn, a_sel, b_sel;
    \$_NOT_ inv (.A(S), .Y(sn));
    \$_AND_ and_a (.A(A), .B(sn), .Y(a_sel));
    \$_AND_ and_b (.A(B), .B(S), .Y(b_sel));
    \$_OR_ or_y (.A(a_sel), .B(b_sel), .Y(Y));
endmodule
