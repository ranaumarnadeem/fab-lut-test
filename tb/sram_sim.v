// SRAM macro model for the Icarus replay: rtl/bram/sram_model.v with xgen driving X
// (the synthesis flow keeps xgen a black box: an X source of the fault simulation).
module xgen (output wire y);
    assign y = 1'bx;
endmodule

module sram_1rw1r_32_256_8_sky130 #(
    parameter integer NUM_WMASKS = 4,
    parameter integer DATA_WIDTH = 32,
    parameter integer ADDR_WIDTH = 8,
    parameter integer RAM_DEPTH  = 1 << ADDR_WIDTH,
    parameter integer DELAY      = 3
) (
    input  wire                  clk0,
    input  wire                  csb0,
    input  wire                  web0,
    input  wire [NUM_WMASKS-1:0] wmask0,
    input  wire [ADDR_WIDTH-1:0] addr0,
    input  wire [DATA_WIDTH-1:0] din0,
    output wire [DATA_WIDTH-1:0] dout0,
    input  wire                  clk1,
    input  wire                  csb1,
    input  wire [ADDR_WIDTH-1:0] addr1,
    output wire [DATA_WIDTH-1:0] dout1
);
    reg [DATA_WIDTH-1:0] mem [0:RAM_DEPTH-1];
    reg [ADDR_WIDTH-1:0] a1;
    reg collide;
    wire we = !csb0 && !web0;
    wire x;
    xgen Inst_xgen (.y(x));
    always @(posedge clk0) begin
        if (we && wmask0[0]) mem[addr0][7:0]   <= din0[7:0];
        if (we && wmask0[1]) mem[addr0][15:8]  <= din0[15:8];
        if (we && wmask0[2]) mem[addr0][23:16] <= din0[23:16];
        if (we && wmask0[3]) mem[addr0][31:24] <= din0[31:24];
    end
    always @(posedge clk1) begin
        a1 <= addr1;
        collide <= we && (addr0 == addr1);
    end
    assign dout1 = collide ? {DATA_WIDTH{x}} : mem[a1];
    assign dout0 = {DATA_WIDTH{x}};
endmodule
