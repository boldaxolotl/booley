# Review Report

**Not Reviewed: 5 findings need you**

## Run

- discovery Review Run 20260101T000000-aaaaaa of `aaaaaaaaaaaa`
- Base `bbbbbbbbbbbb` (merge-base with local main)
- rules changed since the Base: docs/spec.md
- Commit message: Edit a

```
First body line.
Second body line.
```

## Needs you

- `f-major-open` rtl/a.sv:2 MAJOR blocking: f-major-open summary (rtl bugs, resolve major)
- `f-critical-fixed` rtl/a.sv:4 CRITICAL blocking: f-critical-fixed summary (rtl bugs, resolve major)
- `f-waived-ok` rtl/a.sv:5 MAJOR blocking: f-waived-ok summary (rtl bugs, resolve major)
- `f-waived-refused` rtl/a.sv:6 MAJOR blocking: f-waived-refused summary (rtl bugs, resolve major)
- `f-spec-minor` rtl/a.sv:8 MINOR blocking: f-spec-minor summary (rtl spec, resolve minor)

## Non-blocking: introduced

- `f-minor-open` rtl/a.sv:3 MINOR non-blocking: f-minor-open summary
- `f-advisory` rtl/a.sv:7 MAJOR non-blocking, advisory: f-advisory summary

## Non-blocking: pre-existing

- `f-old` rtl/a.sv:9 CRITICAL pre-existing: f-old summary

## Reviews not needed

- tb quality: no changed file
