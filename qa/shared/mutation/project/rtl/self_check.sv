// Build-time self check: a constant function verified during elaboration. Any
// Mutant of `bump` makes `$error` fire, so the build fails and the Mutant is invalid.
module self_check;
  function automatic logic [3:0] bump(input logic [3:0] x);
    return x + 4'd1;
  endfunction

  localparam logic OK = (bump(4'd3) == 4'd4);

  if (!OK) begin : g_bump_broken
    $error("self_check: bump(3) must be 4");
  end
endmodule
