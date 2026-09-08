module top;
  int unsigned value;
  initial begin
    value = $urandom;
    $display("RANDOM=%0d", value);
    if ($test$plusargs("fail")) $fatal(1, "intentional failure");
    if ($test$plusargs("hang")) forever #1;
    #1;
    $finish;
  end
endmodule
