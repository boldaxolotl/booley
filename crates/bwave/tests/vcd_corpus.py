#!/usr/bin/env python3
"""Capture semantic VCD excerpts and replay them into benchmark corpora."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

U64_MAX = (1 << 64) - 1
DEFAULT_MAX_EXCERPT_BYTES = 64 * 1024 * 1024
PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
LINE_CLASS_BY_PREFIX = {
    **{bytes([value]): "scalar" for value in b"01xXzZ"},
    **{bytes([value]): "vector" for value in b"bB"},
    **{bytes([value]): "real" for value in b"rR"},
    b"#": "timestamp",
    b"$": "directive",
}


class CorpusError(ValueError):
    """An invalid source VCD, excerpt, or command-line boundary."""


@dataclass(frozen=True)
class Signal:
    identifier: bytes
    width: int
    is_real: bool


@dataclass
class BodyState:
    values: dict[bytes, bytes] = field(default_factory=dict)
    in_dumpoff: bool = False


@dataclass
class CaptureStats:
    line_classes: Counter[str] = field(default_factory=Counter)
    timestamp_count: int = 0
    event_count: int = 0
    max_events_per_timestamp: int = 0
    events_at_timestamp: int = 0
    selected_event_count: int = 0
    declaration_count: int = 0
    first_timestamp: int | None = None
    last_timestamp: int | None = None

    def timestamp(self, tick: int) -> None:
        self.max_events_per_timestamp = max(
            self.max_events_per_timestamp, self.events_at_timestamp
        )
        self.events_at_timestamp = 0
        self.timestamp_count += 1
        self.first_timestamp = tick if self.first_timestamp is None else self.first_timestamp
        self.last_timestamp = tick

    def event(self, *, selected: bool) -> None:
        self.event_count += 1
        self.events_at_timestamp += 1
        if selected:
            self.selected_event_count += 1

    def finish(self) -> None:
        self.max_events_per_timestamp = max(
            self.max_events_per_timestamp, self.events_at_timestamp
        )


@dataclass(frozen=True)
class ExcerptBody:
    header: bytes
    initialization: list[bytes]
    activity: list[bytes]
    start_tick: int
    end_tick: int
    newline: bytes


def _open_input(path: Path) -> AbstractContextManager[BinaryIO]:
    if path.name.endswith(".gz"):
        return gzip.open(path, "rb")
    return path.open("rb")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    with _open_input(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
    return byte_count, digest.hexdigest()


def _read_header(stream: BinaryIO) -> bytes:
    lines: list[bytes] = []
    saw_enddefinitions = False
    for line in stream:
        lines.append(line)
        for token in line.split():
            if saw_enddefinitions and token == b"$end":
                return b"".join(lines)
            if token == b"$enddefinitions":
                saw_enddefinitions = True
    raise CorpusError("VCD header has no complete $enddefinitions ... $end")


def _directive_end(tokens: list[bytes], start: int) -> int:
    for pos in range(start + 1, len(tokens)):
        if tokens[pos] == b"$end":
            return pos + 1
    raise CorpusError(f"unterminated VCD header directive {tokens[start]!r}")


def _signals_from_header(header: bytes) -> tuple[list[Signal], int]:
    tokens = header.split()
    signals: list[Signal] = []
    seen: set[bytes] = set()
    declaration_count = 0
    pos = 0
    while pos < len(tokens):
        if tokens[pos] != b"$var":
            pos = _directive_end(tokens, pos) if tokens[pos].startswith(b"$") else pos + 1
            continue
        if pos + 4 >= len(tokens):
            raise CorpusError("truncated $var declaration in VCD header")
        kind, width_text, identifier = tokens[pos + 1 : pos + 4]
        try:
            width = int(width_text)
        except ValueError as error:
            raise CorpusError(f"invalid $var width {width_text!r}") from error
        if width <= 0:
            raise CorpusError(f"invalid non-positive $var width {width}")
        if identifier not in seen:
            signals.append(Signal(identifier, width, kind in {b"real", b"realtime"}))
            seen.add(identifier)
        declaration_count += 1
        pos = _directive_end(tokens, pos)
    if not signals:
        raise CorpusError("VCD declares no signals")
    return signals, declaration_count


def _newline_for(header: bytes) -> bytes:
    crlf = header.count(b"\r\n")
    return b"\r\n" if crlf > header.count(b"\n") - crlf else b"\n"


def _parse_timestamp(line: bytes) -> int:
    text = line.rstrip(b"\r\n")
    token = text[1:].strip(b" \t")
    if not token or not token.isdigit():
        raise CorpusError(f"invalid timestamp line {text!r}")
    tick = int(token)
    if tick > U64_MAX:
        raise CorpusError(f"timestamp exceeds u64: {tick}")
    return tick


def _value_identifier(content: bytes) -> bytes | None:
    if not content:
        return None
    if content[:1] in b"01xXzZ":
        return content[1:].strip(b" \t")
    if content[:1] not in b"bBrR":
        return None
    fields = content[1:].split(None, 1)
    return fields[1].strip(b" \t") if len(fields) == 2 else None


def _update_state(state: BodyState, content: bytes, known_ids: set[bytes]) -> bool:
    if content.startswith(b"$dumpoff"):
        state.in_dumpoff = True
        return False
    if content.startswith(b"$dumpon"):
        state.in_dumpoff = False
        return False
    if state.in_dumpoff:
        return False
    identifier = _value_identifier(content)
    if identifier is None or identifier not in known_ids:
        return False
    state.values[identifier] = content
    return True


def _unknown_value(signal: Signal) -> bytes:
    if signal.width == 1 and not signal.is_real:
        return b"x" + signal.identifier
    return b"bx " + signal.identifier


def _write_initial_state(
    output: BinaryIO, tick: int, signals: list[Signal], state: BodyState, newline: bytes
) -> None:
    output.write(b"#" + str(tick).encode("ascii") + newline)
    output.write(b"$dumpvars" + newline)
    for signal in signals:
        output.write(state.values.get(signal.identifier, _unknown_value(signal)) + newline)
    output.write(b"$end" + newline)
    if state.in_dumpoff:
        output.write(b"$dumpoff" + newline + b"$end" + newline)


def _line_class(content: bytes) -> str:
    if not content:
        return "blank"
    return LINE_CLASS_BY_PREFIX.get(content[:1], "other")


def _scan_capture(
    stream: BinaryIO,
    output: BinaryIO,
    signals: list[Signal],
    start: int,
    end: int,
    newline: bytes,
) -> tuple[CaptureStats, int, int]:
    known_ids = {signal.identifier for signal in signals}
    state = BodyState()
    stats = CaptureStats()
    current_tick: int | None = None
    selected_start: int | None = None
    selected_end: int | None = None
    for line in stream:
        content = line.rstrip(b"\r\n")
        stats.line_classes[_line_class(content)] += 1
        if content.startswith(b"#"):
            tick = _parse_timestamp(line)
            if current_tick is not None and tick < current_tick:
                raise CorpusError(f"decreasing timestamp {tick} after {current_tick}")
            current_tick = tick
            stats.timestamp(tick)
            if selected_start is None and start <= tick <= end:
                selected_start = tick
                _write_initial_state(output, tick, signals, state, newline)
            elif selected_start is not None and tick <= end:
                output.write(line)
            if selected_start is not None and tick <= end:
                selected_end = tick
            continue
        selected = selected_start is not None and current_tick is not None and current_tick <= end
        if selected:
            output.write(line)
        if _update_state(state, content, known_ids):
            stats.event(selected=selected)
    stats.finish()
    if selected_start is None or selected_end is None:
        raise CorpusError(f"no timestamp in requested range {start}:{end}")
    return stats, selected_start, selected_end


def _width_distribution(signals: list[Signal]) -> dict[str, int]:
    counts = Counter(signal.width for signal in signals)
    return {str(width): counts[width] for width in sorted(counts)}


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".manifest.json")


def _validate_output(path: Path, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for candidate in (path, _sidecar(path)):
        if candidate.exists() and not force:
            raise CorpusError(f"output exists (pass --force to replace): {candidate}")


def _compress_deterministically(raw_path: Path, output: Path) -> None:
    with (
        raw_path.open("rb") as source,
        output.open("wb") as raw_output,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=0) as compressed,
    ):
        shutil.copyfileobj(source, compressed, length=1024 * 1024)


def capture(args: argparse.Namespace) -> dict[str, object]:
    source: Path = args.source
    output: Path = args.output
    if not source.is_file():
        raise CorpusError(f"source VCD not found: {source}")
    if args.start > args.end:
        raise CorpusError("--start must not exceed --end")
    if not output.name.endswith(".vcd.gz"):
        raise CorpusError("capture output must end in .vcd.gz")
    _validate_output(output, force=args.force)
    temp_path: Path | None = None
    try:
        with _open_input(source) as stream:
            header = _read_header(stream)
            signals, declaration_count = _signals_from_header(header)
            newline = _newline_for(header)
            with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as raw_output:
                temp_path = Path(raw_output.name)
                raw_output.write(header)
                if header and not header.endswith((b"\n", b"\r")):
                    raw_output.write(newline)
                stats, selected_start, selected_end = _scan_capture(
                    stream, raw_output, signals, args.start, args.end, newline
                )
                stats.declaration_count = declaration_count
                if raw_output.tell() > args.max_excerpt_bytes:
                    raise CorpusError("excerpt exceeds --max-excerpt-bytes")
        _compress_deterministically(temp_path, output)
        manifest = _capture_manifest(
            args, source, output, temp_path, signals, stats, selected_start, selected_end
        )
        _write_json(_sidecar(output), manifest)
        return manifest
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _capture_manifest(
    args: argparse.Namespace,
    source: Path,
    output: Path,
    raw_path: Path,
    signals: list[Signal],
    stats: CaptureStats,
    selected_start: int,
    selected_end: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "vcd_semantic_excerpt",
        "profile": args.profile,
        "source": _source_manifest(source, args.source_label),
        "selection": {
            "requested_start": args.start,
            "requested_end": args.end,
            "actual_start": selected_start,
            "actual_end": selected_end,
        },
        "signals": {
            "declarations": stats.declaration_count,
            "unique_ids": len(signals),
            "width_distribution": _width_distribution(signals),
        },
        "activity": _activity_manifest(stats),
        "excerpt": {
            "file": output.name,
            "raw_bytes": raw_path.stat().st_size,
            "compressed_bytes": output.stat().st_size,
            "raw_sha256": _sha256(raw_path),
            "compressed_sha256": _sha256(output),
        },
    }


def _source_manifest(source: Path, label: str) -> dict[str, object]:
    raw_bytes, raw_sha256 = _input_fingerprint(source)
    return {
        "label": label,
        "compression": "gzip" if source.name.endswith(".gz") else "none",
        "stored_bytes": source.stat().st_size,
        "stored_sha256": _sha256(source),
        "raw_bytes": raw_bytes,
        "raw_sha256": raw_sha256,
    }


def _activity_manifest(stats: CaptureStats) -> dict[str, object]:
    return {
        "line_classes": dict(sorted(stats.line_classes.items())),
        "timestamp_count": stats.timestamp_count,
        "event_count": stats.event_count,
        "selected_event_count": stats.selected_event_count,
        "max_events_per_timestamp": stats.max_events_per_timestamp,
        "first_timestamp": stats.first_timestamp,
        "last_timestamp": stats.last_timestamp,
    }


def _load_excerpt(path: Path) -> ExcerptBody:
    with _open_input(path) as stream:
        data = stream.read(DEFAULT_MAX_EXCERPT_BYTES + 1)
    if len(data) > DEFAULT_MAX_EXCERPT_BYTES:
        raise CorpusError(f"excerpt expands beyond {DEFAULT_MAX_EXCERPT_BYTES} bytes")
    stream = memoryview(data)
    header_end = _header_end(data)
    header = bytes(stream[:header_end])
    lines = bytes(stream[header_end:]).splitlines(keepends=True)
    if not lines or not lines[0].startswith(b"#"):
        raise CorpusError("excerpt body must begin with a timestamp")
    start_tick = _parse_timestamp(lines[0])
    init_end = _initialization_end(lines)
    initialization = lines[:init_end]
    activity = lines[init_end:]
    if not activity:
        raise CorpusError("excerpt contains no activity to replay")
    end_tick = _validate_activity(activity, start_tick)
    return ExcerptBody(
        header, initialization, activity, start_tick, end_tick, _newline_for(header)
    )


def _header_end(data: bytes) -> int:
    saw_enddefinitions = False
    offset = 0
    for line in data.splitlines(keepends=True):
        offset += len(line)
        for token in line.split():
            if saw_enddefinitions and token == b"$end":
                return offset
            if token == b"$enddefinitions":
                saw_enddefinitions = True
    raise CorpusError("excerpt has no complete VCD header")


def _initialization_end(lines: list[bytes]) -> int:
    if len(lines) < 3 or lines[1].rstrip(b"\r\n") != b"$dumpvars":
        raise CorpusError("excerpt lacks synthesized $dumpvars state")
    for index, line in enumerate(lines[2:], start=2):
        if line.rstrip(b"\r\n") == b"$end":
            return index + 1
    raise CorpusError("excerpt has unterminated synthesized $dumpvars state")


def _validate_activity(lines: list[bytes], start_tick: int) -> int:
    current = start_tick
    for line in lines:
        if not line.startswith(b"#"):
            continue
        tick = _parse_timestamp(line)
        if tick < current:
            raise CorpusError(f"decreasing excerpt timestamp {tick} after {current}")
        current = tick
    return current


def _write_activity(output: BinaryIO, excerpt: ExcerptBody, offset: int) -> None:
    if offset:
        output.write(b"#" + str(excerpt.start_tick + offset).encode("ascii") + excerpt.newline)
    for line in excerpt.activity:
        if line.startswith(b"#"):
            tick = _parse_timestamp(line) + offset
            output.write(b"#" + str(tick).encode("ascii") + excerpt.newline)
        else:
            output.write(line)
    if excerpt.activity and not excerpt.activity[-1].endswith((b"\n", b"\r")):
        output.write(excerpt.newline)


def _load_profile(excerpt: Path, requested: str | None) -> str:
    if requested is not None:
        return requested
    manifest_path = _sidecar(excerpt)
    if not manifest_path.is_file():
        raise CorpusError("--profile is required when the excerpt manifest is absent")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        profile = manifest["profile"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise CorpusError(f"invalid excerpt manifest: {manifest_path}") from error
    if not isinstance(profile, str):
        raise CorpusError("excerpt manifest profile must be a string")
    return profile


def replay(args: argparse.Namespace) -> dict[str, object]:
    excerpt_path: Path = args.excerpt
    output: Path = args.output
    if not excerpt_path.is_file():
        raise CorpusError(f"excerpt not found: {excerpt_path}")
    _validate_output(output, force=args.force)
    excerpt = _load_excerpt(excerpt_path)
    span = max(1, excerpt.end_tick - excerpt.start_tick + 1)
    max_repetitions = (args.target_bytes // max(1, len(b"".join(excerpt.activity)))) + 2
    repetitions = 0
    with output.open("wb") as stream:
        stream.write(excerpt.header)
        stream.writelines(excerpt.initialization)
        while (stream.tell() < args.target_bytes or repetitions == 0) and (
            repetitions < max_repetitions
        ):
            offset = repetitions * span
            if excerpt.end_tick + offset > U64_MAX:
                raise CorpusError("replayed timestamps exceed u64")
            _write_activity(stream, excerpt, offset)
            repetitions += 1
    if output.stat().st_size < args.target_bytes:
        raise CorpusError("empty activity window cannot reach target size")
    manifest = _replay_manifest(args, excerpt_path, output, excerpt, repetitions)
    _write_json(_sidecar(output), manifest)
    return manifest


def _replay_manifest(
    args: argparse.Namespace,
    excerpt_path: Path,
    output: Path,
    excerpt: ExcerptBody,
    repetitions: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "vcd_replay_corpus",
        "profile": _load_profile(excerpt_path, args.profile),
        "source_excerpt": {"file": excerpt_path.name, "sha256": _sha256(excerpt_path)},
        "target_bytes": args.target_bytes,
        "output": {
            "file": output.name,
            "bytes": output.stat().st_size,
            "sha256": _sha256(output),
            "repetitions": repetitions,
            "first_timestamp": excerpt.start_tick,
            "last_timestamp": excerpt.end_tick
            + (repetitions - 1) * max(1, excerpt.end_tick - excerpt.start_tick + 1),
        },
    }


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from error
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def _nonnegative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from error
    if value < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return value


def _profile(text: str) -> str:
    if not PROFILE_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("profile must match [a-z0-9][a-z0-9_-]*")
    return text


def _source_label(text: str) -> str:
    if not PROFILE_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("source label must contain only lowercase safe-name text")
    return text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture", help="capture a semantic timestamp window")
    capture_parser.add_argument("source", type=Path)
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--start", type=_nonnegative_int, required=True)
    capture_parser.add_argument("--end", type=_nonnegative_int, required=True)
    capture_parser.add_argument("--profile", type=_profile, required=True)
    capture_parser.add_argument("--source-label", type=_source_label, required=True)
    capture_parser.add_argument(
        "--max-excerpt-bytes", type=_positive_int, default=DEFAULT_MAX_EXCERPT_BYTES
    )
    capture_parser.add_argument("--force", action="store_true")
    capture_parser.set_defaults(action=capture)
    replay_parser = commands.add_parser("replay", help="replay an excerpt to a byte target")
    replay_parser.add_argument("excerpt", type=Path)
    replay_parser.add_argument("--output", type=Path, required=True)
    replay_parser.add_argument("--target-bytes", type=_positive_int, required=True)
    replay_parser.add_argument("--profile", type=_profile)
    replay_parser.add_argument("--force", action="store_true")
    replay_parser.set_defaults(action=replay)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        manifest = args.action(args)
    except (CorpusError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
