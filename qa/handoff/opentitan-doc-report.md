# Draft OpenTitan UART documentation report

**Draft only — not submitted.** Proposed title: **Earl Grey 1.0.0 UART documentation: RX FIFO depth and initialization-example inconsistencies**.

This report concerns the frozen documentation at `lowRISC/opentitan@615d3c74fadbbf674c8ca05a70f91094989849fb` (`earlgrey_1.0.0`). It does not claim these issues persist in current upstream, and it makes no RTL defect claim. Verification used only that revision's public UART README, theory, programmer's guide and register Markdown; no implementation or tests were inspected.

## 1. Reception prose gives a different RX FIFO depth

The [Reception paragraph, line 70](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/theory_of_operation.md#L62-L71) describes a 32-byte RX FIFO, while the [feature list, lines 23–24](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/README.md#L18-L27) specifies 64-byte RX and 32-byte TX. The [RX watermark encodings](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/registers.md#fifo_ctrl--rxilvl) include levels 32 and 62, supporting the feature-list interpretation.

Suggested correction: change the Reception paragraph's RX FIFO depth to **64 bytes**, preserving the stated **32-byte TX FIFO**. This resolves the internal documentation inconsistency; it is not an implementation validation claim.

## 2. Initialization example enables an undocumented TX-overflow interrupt

The [initialization example, line 47](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/programmers_guide.md#L44-L50) includes `UART_INTR_ENABLE_TX_OVERFLOW_MASK`. The [INTR_ENABLE register](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/registers.md#intr_enable) defines nine interrupt bits: tx_watermark, rx_watermark, tx_done, rx_overflow, rx_frame_err, rx_break_err, rx_timeout, rx_parity_err and tx_empty. There is no TX-overflow bit in this pinned register contract. The example's intended set already includes RX overflow separately.

Suggested correction: remove the TX-overflow-mask term from the initialization example, unless a separately documented supported interrupt is intended. Do not add a new hardware interrupt merely to make the example compile. This report establishes a mismatch against the public register documentation; it does not claim to have compiled generated headers or examined their definitions.

## 3. Initialization example shifts RX watermark encoding by the wrong bit offset

The [FIFO initialization expression, line 41](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/programmers_guide.md#L37-L42) shifts the RX watermark encoding by three. [FIFO_CTRL.RXILVL](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/registers.md#fifo_ctrl) occupies bits **4:2**, so an unshifted field encoding must be shifted by **two**. TXILVL occupies bits 7:5, and its neighboring shift by five is consistent.

Suggested correction: use the documented RXILVL field offset (2), ideally its generated field-offset constant, instead of the literal shift by three. The shown one-character RX threshold has encoding zero, so the current example happens to write the same bits; that masks the error. A nonzero example exposes it: encoding 1 shifted by 2 selects the two-character RX threshold, whereas shifted by 3 it becomes encoding 2 and selects four characters.

## Requested scope

Please reconcile these three documentation inconsistencies in the appropriate maintained documentation revision or errata. No changes to RTL behavior, no implementation equivalence claim and no external submission are part of this draft. Pinned links above make the observations reproducible even if current documentation differs.
