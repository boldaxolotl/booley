module authored_fst_top (
    input  logic       clk,
    input  logic       rst_n,
    output logic [3:0] count
);
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            count <= '0;
        end else begin
            count <= count + 1'b1;
        end
    end
endmodule
