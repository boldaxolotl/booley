# B-Wave glossary

This is the canonical vocabulary for B-Wave. Shared Booley concepts such as
**Trace Artifact** and **EDA tool** are defined in the
[shared glossary](../../docs/CONTEXT.md); detailed commands and behavior belong
in the [B-Wave documentation](docs/public/intro.md).

**B-Wave**:
The agent-facing query surface for FST trace stores. It reads native FST output directly and ingests VCD output into FST before exposing signal queries, virtual signals, and text-mode waveforms.
_Avoid_: waveform viewer, VCD parser, `.bwave` format

**Virtual Signal**:
A named 1-bit boolean predicate over existing waveform signals. Virtual Signals use Verilog-subset expressions and may compose other Virtual Signals.
_Avoid_: computed signal, derived signal, expression

**Marker**:
A named time annotation associated with a waveform. A persisted Marker can be reused as a time reference; a native wave Marker annotates one rendered wave query.
_Avoid_: bookmark, event, signal

**Waveform Viewer**:
The human-facing GUI used to explore a Trace Artifact. B-Wave controls an external Waveform Viewer but does not render waveforms itself.
_Avoid_: waveform renderer, waveform GUI, wave window, B-Wave display
