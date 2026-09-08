# Exercise the real GTKWave GUI's lazy waveform decoding, not only its loader.
set count [gtkwave::getNumFacs]
if {$count != 2} { puts stderr "Expected 2 signals, got $count"; exit 1 }
set signals {}
for {set i 0} {$i < $count} {incr i} {
    lappend signals [gtkwave::getFacName $i]
}
if {$signals ne {viewer.clk {viewer.counter[3:0]}}} {
    puts stderr "Unexpected hierarchy: $signals"; exit 1
}
if {[gtkwave::addSignalsFromList $signals] != 2} { exit 1 }
if {[gtkwave::getMinTime] != 0 || [gtkwave::getMaxTime] != 20} { exit 1 }
gtkwave::setMarker 5
set clk [gtkwave::getTraceValueAtMarkerFromIndex 0]
set counter [gtkwave::getTraceValueAtMarkerFromIndex 1]
if {$clk ne "1" || $counter ne "3"} {
    puts stderr "Unexpected decoded values: $clk, $counter"; exit 1
}
puts "PASS: GTKWave read 2 signals, time range 0..20, clk=1 and counter=3 at time 5"
exit 0
