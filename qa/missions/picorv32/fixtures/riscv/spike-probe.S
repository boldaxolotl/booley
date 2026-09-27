# Bare-metal Spike probe: exit through HTIF (no OS or proxy kernel).
# Writing (exit_code << 1) | 1 to tohost ends the run; 1 means exit 0.
.section .text
.globl _start
_start:
  la t0, tohost
  li t1, 1
  sw t1, 0(t0)
1:
  j 1b

# Spike finds these symbols by name; 64-byte aligned as HTIF expects.
.section .tohost, "aw", @progbits
.align 6
.globl tohost
tohost: .dword 0
.align 6
.globl fromhost
fromhost: .dword 0
