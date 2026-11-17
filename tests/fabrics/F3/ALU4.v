// F3 validation BEL: a 4-bit ALU fabtest has no plugin for (opaque).
// Same pins as RegFile_32x4, so the CUST tile reuses the RegFile switch-matrix list.
// ConfigBits[0]: AD = D + A_ADR[3:0] (1) or D ^ A_ADR[3:0] (0)
// ConfigBits[1]: BD registered (1) or combinational (0); BD_comb = B_ADR[3:0] & W_ADR[3:0]
`default_nettype none

(* FABulous, BelMap,
    ADD=0,
    BD_reg=1
*)
module ALU4 #(
    parameter integer NoConfigBits = 2
) (
    input  wire [3:0] D    ,
    input  wire [4:0] W_ADR,
    input  wire       W_en ,
    output wire [3:0] AD   ,
    input  wire [4:0] A_ADR,
    output wire [3:0] BD   ,
    input  wire [4:0] B_ADR,
    (* FABulous, EXTERNAL, SHARED_PORT *) input wire UserCLK,
    (* FABulous, GLOBAL *) input wire [NoConfigBits-1:0] ConfigBits
);

    wire [3:0] sum = D + A_ADR[3:0];
    wire [3:0] bd_comb = (B_ADR[3:0] & W_ADR[3:0]) ^ {4{W_en & A_ADR[4]}};
    reg  [3:0] bd_reg;

    always @(posedge UserCLK) begin
        if (B_ADR[4] == 1'b0)
            bd_reg <= bd_comb;
    end

    assign AD = ConfigBits[0] ? sum : (D ^ A_ADR[3:0]);
    assign BD = ConfigBits[1] ? bd_reg : bd_comb;

endmodule
`resetall
