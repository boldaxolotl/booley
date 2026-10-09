# Frozen standalone UART contract

Review the RTL against all the following Project-visible documents. Read the
linked files in this snapshot before deciding spec compliance; this index is
not a substitute for their full text. Retrieve no upstream implementation,
tests, generated registers or current documentation.

The corpus is frozen at OpenTitan
`615d3c74fadbbf674c8ca05a70f91094989849fb`; its exact file digests are in
[corpus-manifest.json](corpus-manifest.json). The normative sources are:

- [UART overview](corpus/hw/ip/uart/README.md)
- [Theory of operation](corpus/hw/ip/uart/doc/theory_of_operation.md)
- [Programmer's guide](corpus/hw/ip/uart/doc/programmers_guide.md)
- [Registers](corpus/hw/ip/uart/doc/registers.md)
- [Interfaces](corpus/hw/ip/uart/doc/interfaces.md)
- [Block diagram](corpus/hw/ip/uart/doc/block_diagram.svg)
- [MMIO addendum](mmio-addendum.md)
- [Timing addendum](timing-addendum.md)
- [Interface stub](qa_uart.sv)

Apply addenda first, register definitions and precise behavior text second,
and examples last. RX FIFO depth 64 and TX FIFO depth 32 are normative.
Report conflicting examples rather than silently adopting their behavior.
The allowed sources include the corpus's Apache-2.0 [LICENSE](corpus/LICENSE).
The independent evaluator and its private inputs are outside this contract
and must remain inaccessible to the implementing child and its Specialists.
