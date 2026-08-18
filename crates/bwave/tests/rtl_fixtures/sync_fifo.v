`timescale 1ns/1ps

module sync_fifo (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       wr_en,
    input  wire       rd_en,
    input  wire [7:0] wr_data,
    output reg  [7:0] rd_data,
    output wire       full,
    output wire       empty,
    output reg  [2:0] count
);

    reg [7:0] mem [0:3];
    reg [1:0] wr_ptr;
    reg [1:0] rd_ptr;

    assign full  = (count == 3'd4);
    assign empty = (count == 3'd0);

    // Write logic
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            wr_ptr <= 2'd0;
        else if (wr_en && !full) begin
            mem[wr_ptr] <= wr_data;
            wr_ptr      <= wr_ptr + 2'd1;
        end
    end

    // Read logic
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rd_ptr  <= 2'd0;
            rd_data <= 8'd0;
        end else if (rd_en && !empty) begin
            rd_data <= mem[rd_ptr];
            rd_ptr  <= rd_ptr + 2'd1;
        end
    end

    // Count tracking
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= 3'd0;
        else begin
            case ({wr_en && !full, rd_en && !empty})
                2'b10:   count <= count + 3'd1; // write only
                2'b01:   count <= count - 3'd1; // read only
                default: count <= count;         // both or neither
            endcase
        end
    end

endmodule
