# Ticket Creation Defaults

This Project-owned file is intentionally inactive after `booley init`. To enable it,
uncomment every YAML mapping below and replace the examples with this Project's complete
defaults. An active file must define **On success** and all four Ticket types. The mappings
use exactly the same `on_success` and `criteria` syntax as Ticket frontmatter.

Only `/booley-ticket-create` consumes these defaults. Text outside the YAML blocks is
explanatory and cannot change Ticket creation.

## On success

```yaml
# on_success:
#   destination: review
#   merge: true
#   cleanup: true
#   triage_report: true
```

## Feature

```yaml
# criteria:
#   mandatory:
#     lint_clean: [lint_default]
#     sim_pass:
#       - tb/example_tb.sv @ sim_default @ all @ pass -> pass
#     review_rtl_bugs: true
#     review_tb_quality: true
#   optional: {}
```

## Bugfix

```yaml
# criteria:
#   mandatory:
#     sim_pass:
#       - tb/example_tb.sv @ sim_default @ all @ pass -> pass
#   optional: {}
```

## Refactor

```yaml
# criteria:
#   mandatory:
#     lint_clean: [lint_default]
#     sim_pass:
#       - tb/example_tb.sv @ sim_default @ all @ pass -> pass
#     review_rtl_bugs: true
#   optional: {}
```

## Verification

```yaml
# criteria:
#   mandatory:
#     sim_pass:
#       - tb/example_tb.sv @ sim_default @ all @ pass -> pass
#     review_tb_quality: true
#   optional: {}
```
