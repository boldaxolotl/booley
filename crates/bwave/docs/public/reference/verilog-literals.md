# Verilog literals

Every B-Wave subcommand that accepts a *value*
(`find <PAT> <VAL>`, `sample <PAT> <VAL>`,
`distance <PAT> <VAL> [--to <PAT> <VAL>]`,
`stuck [VAL]`, virtual-signal expressions) uses the
same Verilog literal syntax. This page lists the
forms and explains why some look-alikes are rejected.

## Accepted forms

### Base-prefixed (no width)

| Form | Meaning |
|---|---|
| `'d255` | decimal 255 |
| `'h FF` | hex FF (255) |
| `'hFF` | same |
| `'hff` | same (case-insensitive) |
| `'b1010` | binary 1010 (10) |
| `'o17` | octal 17 (15) |

The leading apostrophe is mandatory. Letter is the base:
`d` decimal, `h` hex, `b` binary, `o` octal. The literal
takes the natural width of the value.

### Width-prefixed

| Form | Meaning |
|---|---|
| `8'd255` | 8-bit decimal 255 |
| `8'hFF` | 8-bit hex FF |
| `1'b1` | 1-bit 1 |
| `4'hF` | 4-bit hex F |
| `32'h DEADBEEF` | 32-bit hex DEADBEEF |
| `13'h0C1C` | 13-bit hex 0C1C |

Width prefix locks the width. If you use a width-prefixed
literal in a signal-to-literal comparison, the store
value must match the locked width or the comparison
errors.

### Bare integers

`0`, `1`, `42`, `255`: accepted as decimal in
contexts where unambiguous. In virtual-signal
expressions they behave like `'d0`, `'d1`, etc.

### Edge keywords (find / sample / distance only)

| Keyword | Match condition |
|---|---|
| `rising` | low-to-high transition |
| `falling` | high-to-low transition |
| `change` | any transition |

Edge keywords replace the VALUE positional argument in
`find`, `sample`, and `distance`. They cannot be used
inside virtual-signal expressions.

## Rejected forms

### Bare hex

```
bwave find sim.fst "*error*" FF        # ERROR
bwave find sim.fst "*error*" DEADBEEF  # ERROR
```

`FF` and `DEADBEEF` look like hex to a human but are
syntactically ambiguous (signal names can also be plain
identifiers). B-Wave rejects them with:

```
ERROR: cannot parse value 'FF' — use Verilog literal
       (e.g. 'hFF, 8'hFF) or bare decimal
```

The fix is always to add a base prefix:

```bash
bwave find sim.fst "*error*" 'hFF
bwave find sim.fst "*error*" 32'hDEADBEEF
```

### Stray spaces

`8 'd 255`: the parser is whitespace-tolerant inside
the literal itself (`'h FF` works) but not around the
width prefix. Stick to `8'd255`, no spaces.

### Mixed bases

`'h FF'd255`: only one literal per value argument.

## Display vs. comparison

There are *two* radix systems and they should not be
confused:

- **Display radix** is the `%d` / `%b` / `%h` suffix
  attached to `-s` patterns: `-s "data_out%d"` makes
  text-mode output render `data_out` in decimal. Default
  is `%h`.
- **Comparison literal** is the value you pass to
  `find` / `sample` / `distance` / `stuck`: `'d255`,
  `'hFF`. This is what the store value is compared
  against.

Common confusion: you set the display radix to decimal
and then try to find with `255` thinking it'll match.
That works (`255` is a bare decimal), but the value the
store holds is *always* binary internally; the radix
only affects display. `'d255`, `255`, `'hFF`, and
`8'd255` all match the same store value (assuming
widths allow).

See `troubleshooting/radix-pitfalls` for worked
examples.

## Edge keywords vs. value matches

`find sig 'h1` matches every cycle where `sig` *holds*
`1`. `find sig rising` matches every cycle where `sig`
*transitioned to* `1`. These differ: a signal can hold
1 for many cycles after a single rising edge. Use
`rising` for event semantics, `'h1` for state semantics.

## Where each form is accepted

| Context | Bare int | `'dN`/`'hN`/etc | Width-prefixed | Edge keyword |
|---|---|---|---|---|
| `find` VALUE | yes | yes | yes | yes |
| `sample` VAL | yes | yes | yes | yes |
| `distance` VALUE (and `--to` VALUE) | yes | yes | yes | yes |
| `stuck` VALUE | yes | yes | yes | no |
| Virtual signal expression | yes (as decimal) | yes | yes | no |
| `-s PATTERN%RADIX` display suffix | n/a | n/a | n/a | n/a |

## Recipes

Find a 32-bit constant:

```bash
bwave find sim.fst "*config" 32'hDEADBEEF --first
```

Find a small decimal state code:

```bash
bwave find sim.fst "*state*" 'd5 --first
```

Bare-decimal shortcut (only safe for small integers):

```bash
bwave find sim.fst "*counter*" 100
```

Edge-triggered sample:

```bash
bwave sample sim.fst "*clk_div" rising -s "*data*"
```

Width-locked comparison inside a virtual signal:

```bash
bwave find sim.fst \
    --virtual "magic_match = *header == 32'hDEADBEEF" \
    magic_match 'h1 --first
```
