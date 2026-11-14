- Fixed the fpga Flow choosing Vivado sources by file name: it now uses each
  file's declared `file_type`, so SystemVerilog in a `.v` file is read as
  SystemVerilog and sources with other suffixes (such as `.SV`) are no longer
  dropped. Sources keep their `.core` order.
