"""Publish compact producing-build declaration evidence using Campaign artifacts."""

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from .campaign_durability import durable_create
from .campaign_reports import write_campaign_json
from .coverage_campaign import CoverageArtifact, CoverageCampaign
from .coverage_provenance import content_digest
from .verilator_coverage import CoverageCollectionResult
from .verilator_declarations import (
    DECLARATION_CONTRACT,
    DeclarationDiagnostic,
    DeclarationInventory,
)


def publish_declaration_evidence(
    root: Path, result: CoverageCollectionResult
) -> CoverageCollectionResult:
    """Persist successful build evidence before publishing its Campaign manifest."""
    inventory = result.build.declarations
    # Failed builds have no producing declaration inventory and no advisory.
    if not result.build.successful:
        return result
    if inventory is None:
        inventory = DeclarationInventory(
            (result.collector.tag, result.collector.commit),
            "unavailable",
            diagnostics=(
                DeclarationDiagnostic(
                    "missing_build_evidence", "Successful build supplied no declaration evidence"
                ),
            ),
        )
    document = inventory.document()
    document["native_source_presence"] = {
        "paths": list(result.native_sources),
        "unresolved": list(result.native_source_diagnostics),
    }
    path = root / "declarations" / "inventory.json"
    write_campaign_json(path, document)
    status = inventory.status if not result.native_source_diagnostics else "incomplete"
    artifacts = [
        _artifact(
            root,
            path,
            "declaration_inventory",
            {
                "contract_version": DECLARATION_CONTRACT,
                "discovery_status": status,
            },
        )
    ]
    for name, data in inventory.raw_evidence:
        raw = root / "build-evidence" / name
        durable_create(raw, data)
        artifacts.append(_artifact(root, raw, "declaration_raw", {}))
    return replace(result, artifacts=(*result.artifacts, *artifacts))


def _artifact(root: Path, path: Path, kind: str, attributes: dict) -> CoverageArtifact:
    data = path.read_bytes()
    return CoverageArtifact(
        id=f"artifact:{kind}:{path.name}",
        kind=kind,
        path=path.relative_to(root).as_posix(),
        sha256=content_digest(data),
        bytes=len(data),
        state="fresh_queryable",
        owner_run=None,
        attributes=MappingProxyType(attributes),
    )


def validate_declaration_publication(root: Path, campaign: CoverageCampaign) -> None:
    """Reject malformed or mismatched newly published advisory evidence."""
    from .coverage_source_gaps import GAP_CODE, INCOMPLETE_CODE, source_gap_findings

    inventory = load_declaration_artifact(root, campaign)
    if inventory is None:
        return
    document, decoded = inventory
    presence = document["native_source_presence"]
    expected = ()
    if (
        campaign.collection["status"] == "complete"
        and campaign.collector.native_format["compatibility"] == "compatible"
    ):
        expected = source_gap_findings(
            decoded,
            campaign.source_closure,
            tuple(presence["paths"]),
            tuple(presence["unresolved"]),
        )
    actual = tuple(f for f in campaign.findings if f.code in {GAP_CODE, INCOMPLETE_CODE})
    if actual != expected:
        raise ValueError("Source-gap findings differ from producing declaration evidence")


def load_declaration_artifact(
    root: Path, campaign: CoverageCampaign
) -> tuple[dict, DeclarationInventory] | None:
    """Authenticate compact evidence; raw diagnostic retention is independent."""
    from .verilator_declarations import _bounded_json, _read_evidence_bytes

    matches = [a for a in campaign.artifacts if a.kind == "declaration_inventory"]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Ambiguous declaration inventory artifact")
    artifact = matches[0]
    path = root / artifact.path
    if (
        artifact.path != "declarations/inventory.json"
        or path.is_symlink()
        or not path.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError("Unsafe declaration inventory path")
    data = _read_evidence_bytes(path)
    if len(data) != artifact.bytes or content_digest(data) != artifact.sha256:
        raise ValueError("Declaration inventory digest mismatch")
    document = _bounded_json(data)
    inventory = _decode_inventory(document)
    version = campaign.collector.version
    if inventory.compiler != (version["tag"], version["commit"]):
        raise ValueError("Declaration inventory compiler differs from Campaign")
    presence = document["native_source_presence"]
    status = inventory.status if not presence["unresolved"] else "incomplete"
    if (
        artifact.attributes.get("contract_version") != DECLARATION_CONTRACT
        or artifact.attributes.get("discovery_status") != status
    ):
        raise ValueError("Declaration inventory attributes differ from evidence")
    return document, inventory


def _decode_inventory(value: object) -> DeclarationInventory:
    from booley.core.boundary import require_dict, require_list, require_str

    from .verilator_declarations import MAX_DECLARATIONS

    document = require_dict(value, field="inventory")
    if document.get("$schema") != DECLARATION_CONTRACT or document.get("status") not in {
        "complete",
        "incomplete",
    }:
        raise ValueError("Unsupported declaration inventory contract")
    compiler = require_dict(document.get("compiler"), field="compiler")
    sources = tuple(
        _decode_source(s) for s in require_list(document.get("sources"), field="sources")
    )
    if len(sources) > MAX_DECLARATIONS or len({s.path for s in sources}) != len(sources):
        raise ValueError("Invalid inventory source count or duplicate paths")
    paths = {s.path for s in sources}
    declarations = [
        _decode_declaration(record, paths)
        for record in require_list(document.get("declarations"), field="declarations")
    ]
    if len(declarations) > MAX_DECLARATIONS or declarations != sorted(set(declarations)):
        raise ValueError("Invalid inventory declaration ordering or count")
    diagnostics = tuple(
        _decode_diagnostic(d)
        for d in require_list(document.get("diagnostics"), field="diagnostics")
    )
    inventory = DeclarationInventory(
        (require_str(compiler, "tag"), require_str(compiler, "commit")),
        require_str(document, "build_identity"),
        sources,
        tuple(declarations),
        diagnostics,
    )
    if inventory.status != document["status"]:
        raise ValueError("Invalid inventory completeness")
    _validate_presence(document.get("native_source_presence"), paths)
    _validate_raw_references(document.get("raw_evidence"))
    return inventory


def _decode_declaration(value: object, sources: set[str]):
    from booley.core.boundary import require_dict, require_str

    from .verilator_declarations import Declaration

    record = require_dict(value, field="declaration")
    if (
        record.get("kind") not in {"MODULE", "IFACE", "PACKAGE"}
        or record.get("source") not in sources
    ):
        raise ValueError("Invalid inventory declaration")
    return Declaration(
        require_str(record, "kind"),
        require_str(record, "name"),
        require_str(record, "source"),
        require_str(record, "compiler_location"),
        _location_number(record, "line"),
        _location_number(record, "column"),
    )


def _location_number(value: dict, key: str) -> int:
    from booley.core.boundary import require_int

    number = require_int(value.get(key), field=key)
    if number < 1:
        raise ValueError("Invalid declaration location")
    return number


def _decode_source(value: object):
    import re

    from booley.core.boundary import require_dict, require_str

    from .coverage_source_gaps import _canonical_path
    from .verilator_declarations import DeclarationSource

    source = require_dict(value, field="source")
    path, digest = require_str(source, "path"), require_str(source, "sha256")
    aliases = source.get("aliases")
    if _canonical_path(path) is None or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError("Invalid inventory source identity")
    if (
        not isinstance(aliases, list)
        or not aliases
        or any(not isinstance(a, str) or not a for a in aliases)
    ):
        raise ValueError("Invalid inventory source aliases")
    if not isinstance(source.get("testbench"), bool) or not isinstance(
        source.get("include"), bool
    ):
        raise ValueError("Invalid inventory source roles")
    return DeclarationSource(
        path,
        tuple(aliases),
        digest,
        require_str(source, "file_type"),
        source["testbench"],
        source["include"],
    )


def _decode_diagnostic(value: object) -> DeclarationDiagnostic:
    from booley.core.boundary import require_dict, require_str

    diagnostic = require_dict(value, field="diagnostic")
    return DeclarationDiagnostic(
        require_str(diagnostic, "code"), require_str(diagnostic, "message")
    )


def _validate_presence(value: object, sources: set[str]) -> None:
    from booley.core.boundary import require_dict, require_list

    presence = require_dict(value, field="native_source_presence")
    paths = require_list(presence.get("paths"), field="paths")
    unresolved = require_list(presence.get("unresolved"), field="unresolved")
    if any(not isinstance(p, str) or p not in sources for p in paths) or paths != sorted(
        set(paths)
    ):
        raise ValueError("Invalid native source presence")
    if any(not isinstance(p, str) for p in unresolved):
        raise ValueError("Invalid unresolved native source presence")


def _validate_raw_references(value: object) -> None:
    import re

    from booley.core.boundary import require_dict, require_list, require_str

    records = require_list(value, field="raw_evidence")
    if len(records) > 2:
        raise ValueError("Too many raw declaration references")
    paths = set()
    for item in records:
        record = require_dict(item, field="raw reference")
        path, digest = require_str(record, "path"), require_str(record, "sha256")
        if (
            path not in {"build-evidence/cells.tree.json", "build-evidence/tree.meta.json"}
            or path in paths
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise ValueError("Invalid raw declaration reference")
        paths.add(path)
