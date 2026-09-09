# B-Wave glossary

This is the canonical vocabulary for B-Wave. Shared Booley concepts such as
**Trace Artifact** and **EDA tool** are defined in the
[shared glossary](../../docs/CONTEXT.md); detailed commands and behavior belong
in the [B-Wave documentation](docs/public/intro.md).

## Language

**B-Wave**:
The agent-facing query surface for RTL waveform stores.
_Avoid_: waveform viewer, VCD parser, `.bwave` format

**Virtual Signal**:
A named boolean predicate over existing waveform signals.
_Avoid_: computed signal, derived signal, expression

**Marker**:
A named time annotation associated with a waveform.
_Avoid_: bookmark, event, signal

**Waveform Viewer**:
The human-facing GUI for visually exploring a Trace Artifact, distinct from B-Wave's agent-facing query surface.
_Avoid_: waveform renderer, waveform GUI, wave window, B-Wave display
