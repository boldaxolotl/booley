# Copyright edalize contributors
# Licensed under the 2-Clause BSD License, see LICENSE for details.
# SPDX-License-Identifier: BSD-2-Clause
#
# Flow-API EDA-tool node for Verible lint (verible-verilog-lint).
#
# Authored in Booley (ADR 0033) for the Edalize `lint` flow and intended for
# upstreaming to https://github.com/olofk/edalize — the EDA-tool option names
# (`ruleset`, `rules`, `verible_lint_args`) and the `veribleLintRules` /
# `veribleLintWaiver` file types match the legacy `edalize/veriblelint.py`
# backend so existing cores keep working. Unlike the legacy backend this node
# emits a Makefile target (one `verible-verilog-lint` invocation over the
# whole fileset) and passes `--parse_fatal --lint_fatal=false` (both flags
# default to TRUE in the binary, so lint_fatal must be disabled explicitly):
# a syntax error exits non-zero (a hard EDA-tool error), while lint findings exit
# zero and are reported on stdout for the caller to parse.

from edalize.tools.edatool import Edatool
from edalize.utils import EdaCommands


class Verible(Edatool):
    description = "Verible lint (verible-verilog-lint)"

    TOOL_OPTIONS = {
        "ruleset": {
            "type": "str",
            "desc": "Ruleset: [default|all|none]",
        },
        "rules": {
            "type": "str",
            "desc": 'What rules to use. Prefix a rule name with "-" to disable it.',
            "list": True,
        },
        "verible_lint_args": {
            "type": "str",
            "desc": "Extra command line arguments passed to the Verible EDA tool",
            "list": True,
        },
    }

    def setup(self, edam):
        super().setup(edam)

        src_files = []
        rules_files = []
        waiver_files = []
        unused_files = []
        for f in self.files:
            file_type = f.get("file_type", "")
            if file_type.startswith("verilogSource") or file_type.startswith(
                "systemVerilogSource"
            ):
                # verible-verilog-lint operates on single unpreprocessed
                # files: include files are reached from their includers and
                # are not linted standalone.
                if f.get("is_include_file"):
                    unused_files.append(f)
                else:
                    src_files.append(f["name"])
            elif file_type == "veribleLintRules":
                rules_files.append(f["name"])
            elif file_type == "veribleLintWaiver":
                waiver_files.append(f["name"])
            else:
                unused_files.append(f)

        if not src_files:
            raise RuntimeError("verible needs at least one (System)Verilog source file to lint")
        if len(rules_files) > 1:
            raise RuntimeError(
                "Verible lint only supports a single rules file (type veribleLintRules)"
            )

        self.edam = edam.copy()
        self.edam["files"] = unused_files

        # --parse_fatal --lint_fatal=false: a parse error is a hard EDA-tool
        # error (non-zero exit); findings are data on stdout (zero exit).
        # Both flags default to true in verible-verilog-lint, so merely
        # omitting --lint_fatal does NOT disable it — findings would exit 1
        # and be misread as a hard EDA-tool error downstream.
        # No --ruleset/--rules defaults are injected here: unless the EDAM
        # says otherwise, Verible's own `default` ruleset applies.
        args = ["--parse_fatal", "--lint_fatal=false"]
        if "rules" in self.tool_options:
            args.append("--rules=" + ",".join(self.tool_options["rules"]))
        if "ruleset" in self.tool_options:
            args.append("--ruleset=" + self.tool_options["ruleset"])
        args += self.tool_options.get("verible_lint_args", [])
        if rules_files:
            args.append("--rules_config=" + rules_files[0])
        if waiver_files:
            args.append("--waiver_files=" + ",".join(waiver_files))

        # Note: vlogdefine/vlogparam are not forwarded — verible-verilog-lint
        # lints unpreprocessed sources and has no define/parameter flags.
        commands = EdaCommands()
        # The `lint` target names no produced file, so every `make` re-runs
        # the linter (lint output is the product, not an artifact on disk).
        commands.add(
            ["verible-verilog-lint"] + args + src_files,
            ["lint"],
            src_files + rules_files + waiver_files,
        )
        commands.set_default_target("lint")
        self.commands = commands
