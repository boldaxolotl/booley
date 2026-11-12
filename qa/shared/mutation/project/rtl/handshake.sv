// Handshake: both testbenches wait for `ack` before they check anything.
module handshake (
    input  logic req,
    output logic ack
);
  assign ack = req;
endmodule
