`timescale 1ns/1ps

module lfsr_8bit (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       enable,
    output reg  [7:0] lfsr_out,
    output wire       bit_out
);

    // Feedback: x^8 + x^6 + x^5 + x^4 + 1
    // Taps at bits 7, 5, 4, 3 (0-indexed)
    wire feedback;
    assign feedback = lfsr_out[7] ^ lfsr_out[5] ^ lfsr_out[4] ^ lfsr_out[3];

    assign bit_out = lfsr_out[7];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            lfsr_out <= 8'hA5; // Seed value
        else if (enable)
            lfsr_out <= {lfsr_out[6:0], feedback};
    end

endmodule
