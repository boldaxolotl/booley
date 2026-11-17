# Verilog literals

The value arguments of `find`, `sample`, `distance` and `stuck`, and the
values inside `--virtual` definitions, are Verilog literals. This page
lists the accepted forms and the edge keywords that can replace a value.

## Accepted forms

| Form | Meaning |
|---|---|
| `'d255` | decimal 255 |
| `'hFF`, `'hff` | hex FF (255); digits are case-insensitive |
| `'b1010` | binary 1010 (10) |
| `8'd255`, `4'hF`, `1'b1` | the same, with a width prefix |
| `255` | bare integer: decimal (not inside `--virtual`, where a bare number is a signal name: write `'d255`) |
| `'hx`, `'h1x` | hex with X or Z digits (`x` and `z`, any case, mixed with `0-9a-f`) |
| `'bx`, `'b1x` | binary with X or Z digits (`0 1 x z`, mixed freely) |
| `'dx`, `'dz` | decimal X or Z: a single `x` or `z`, not mixed with digits |
| `'hF_F`, `8'b1111_0000` | `_` after the first digit is ignored |

The base is `d`, `h` or `b`; any other base exits 2. Write the literal
without spaces. `'d255`, `255`, `'hFF` and `8'd255` all match the same
stored value.

A character that is not a digit of the base exits 2, and the error names
the literal: `'hG`, `'d1x`, `'h F`, `'hF'd15` and `'h_F` are all rejected.
`stuck`, `find`, `sample`, `distance` and `--virtual` apply the same rules.

A lone `x` or `z` literal (`'hx`, `'bx`, `'dz`) matches any value with an
X or Z bit. A mixed binary literal matches the value shown with those
digits: `'b1x` matches `1x`.
`stuck` also takes a bare `x` or `z` filter.

Bare hex such as `FF` is rejected with exit 2, because it cannot be told
apart from a decimal number or a name: add the base, `'hFF`.

## Edge keywords

`find`, `sample` and `distance` (both events) also take an edge keyword in
place of the value. `stuck` and `--virtual` definitions do not.

| Keyword | Matches |
|---|---|
| `rising` | a low-to-high transition |
| `falling` | a high-to-low transition |
| `change` | any transition |

A value and an edge ask different questions: `find sig 'h1` matches every
cycle where `sig` *holds* 1, while `find sig rising` matches only the
cycles where it *became* 1.

## Display radix is separate

The `%h`/`%d`/`%b` suffix on an `-s` pattern only changes how text output
renders values (`reference/patterns`). It never changes which values a
literal matches.

## Examples

```bash
bwave find sim.fst "*config" 32'hDEADBEEF --first
bwave sample sim.fst "*clk_div" rising -s "*data*"
bwave find sim.fst --virtual "magic = *header == 32'hDEADBEEF" magic 'h1 --first
```
