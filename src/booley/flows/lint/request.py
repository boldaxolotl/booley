"""Structured input for the lint Flow."""

from dataclasses import dataclass

from booley.flows.request import FlowRequest


@dataclass(kw_only=True)
class LintRequest(FlowRequest):
    scope: str = ""
