# Booley

Booley is a Python framework for agentic FPGA/ASIC development, including
simulation, synthesis, linting, and ticket-based workflows.

## Repository Rules

- Read `docs/CODING_PRINCIPLES.md` before writing Python code.
- Run `ruff check src/ tests/` before committing Python changes.
- Keep project-specific content in the directory resolved by `booley.runtime.project_dir`;
  framework code must not hardcode project paths or names.
