// FABulous clk_buf (assign X = A) as a kept buffer cell: each tile's clock segment
// (UserCLK -> UserCLKo) stays a gate, so its faults are in the fault list.
module clk_buf (input A, output X);
    (* keep *) \$_BUF_ _TECHMAP_REPLACE_ (.A(A), .Y(X));
endmodule
