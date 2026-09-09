### Interface and MMIO contract

The interface-only `qa_uart` stub declares clock, active-low reset, request valid/ready, write flag, 32-bit byte address, 32-bit write data, four byte strobes, response valid/ready, 32-bit read data, error, serial RX/TX, and nine IRQ outputs ordered as INTR_STATE bits 0–8. It contains no UART implementation or tests.

The scenario-owned replacement bus follows these rules:

- Handshakes occur on rising edges. One accepted request may be outstanding. Request fields remain stable until acceptance; response data/error/valid remain stable under backpressure. No new request is accepted while a response is pending.
- An idle released interface accepts a continuously asserted request within four clocks and presents the response within four clocks of acceptance; no zero-cycle throughput requirement.
- Read snapshots and register side effects occur once at request acceptance, including RDATA pop and WDATA enqueue, never repeatedly during response stalls.
- Write strobes mask byte lanes, including W1C effects. WDATA enqueues only with its low byte enabled. Zero-strobe writes succeed without effects. Reads ignore strobes. RO writes are ignored, WO reads return zero, and reserved bits read zero/ignore writes.
- Misaligned or unmapped addresses return error and zero data without side effects; address truncation must not create aliases. Empty FIFO reads must not underflow; values explicitly unspecified by documentation are not scored.
- Reset cancels pending transactions and restores defined register/FIFO/UART state. Hold reset at least four clocks with RX high and release on a clock boundary. Check defined values, not fields documented as unspecified.
- Retain every register offset. **ALERT_TEST at 0x0c reads zero and acknowledges writes without effect**, an explicit exception for excluded alert infrastructure.

TL-UL, alert/integrity, low-power and SoC integration remain excluded. No OpenTitan implementation equivalence is claimed. Reserved control encodings or undocumented combinations are not assigned guessed RTL behavior.
