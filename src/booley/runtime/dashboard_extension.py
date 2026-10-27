"""Build the bundled workspace extension without an npm or Marketplace dependency."""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.paths import package_data_dir

VSIX_PATH = "/usr/local/libexec/booley/dashboard.vsix"


def _vsix_manifest(manifest: dict) -> bytes:
    root = ElementTree.Element(
        "PackageManifest",
        Version="2.0.0",
        xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011",
    )
    metadata = ElementTree.SubElement(root, "Metadata")
    ElementTree.SubElement(
        metadata,
        "Identity",
        Language="en-US",
        Id=manifest["name"],
        Version=manifest["version"],
        Publisher=manifest["publisher"],
    )
    ElementTree.SubElement(metadata, "DisplayName").text = manifest["displayName"]
    ElementTree.SubElement(metadata, "Description").text = manifest["description"]
    properties = ElementTree.SubElement(metadata, "Properties")
    for key, value in (
        ("Microsoft.VisualStudio.Code.Engine", manifest["engines"]["vscode"]),
        ("Microsoft.VisualStudio.Code.ExtensionKind", "workspace"),
    ):
        ElementTree.SubElement(properties, "Property", Id=key, Value=value)
    installation = ElementTree.SubElement(root, "Installation")
    ElementTree.SubElement(installation, "InstallationTarget", Id="Microsoft.VisualStudio.Code")
    ElementTree.SubElement(root, "Dependencies")
    assets = ElementTree.SubElement(root, "Assets")
    ElementTree.SubElement(
        assets,
        "Asset",
        Type="Microsoft.VisualStudio.Code.Manifest",
        Path="extension/package.json",
        Addressable="true",
    )
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def render_vsix() -> bytes:
    """Return a deterministic VSIX assembled from wheel-owned extension sources."""
    source = package_data_dir() / "vscode/dashboard"
    manifest = json.loads((source / "package.json").read_bytes())
    files = {
        f"extension/{name}": (source / name).read_bytes()
        for name in ("package.json", "extension.js", "vaporview_bridge.js", "LICENSE.txt")
    }
    files["extension.vsixmanifest"] = _vsix_manifest(manifest)
    files["[Content_Types].xml"] = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="json" ContentType="application/json"/>'
        b'<Default Extension="js" ContentType="application/javascript"/>'
        b'<Default Extension="txt" ContentType="text/plain"/>'
        b'<Default Extension="vsixmanifest" ContentType="text/xml"/></Types>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()


def write_vsix(path: Path) -> None:
    """Publish the image-owned extension at its fixed installation path."""
    atomic_replace_bytes(path, render_vsix(), mode=0o644)


def installed_error(path: Path = Path(VSIX_PATH)) -> str | None:
    """Explain a missing or stale image artifact without requiring an attached editor."""
    try:
        if path.read_bytes() != render_vsix():
            return "bundled Dashboard extension differs from the installed Booley code"
    except (OSError, ValueError) as exc:
        return f"bundled Dashboard extension unavailable: {exc}"
    return None


if __name__ == "__main__":
    write_vsix(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(VSIX_PATH))
