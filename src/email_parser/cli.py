"""Command-line interface for converting email bytes into JSON."""

from __future__ import annotations

import argparse
from contextlib import suppress
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
from typing import BinaryIO, Sequence, TextIO
import unicodedata

from .parser import Attachment, parse_email

DEFAULT_MAX_BYTES = 25 * 1024 * 1024


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if not 0 < number < sys.maxsize:
        raise argparse.ArgumentTypeError(f"must be a positive integer below {sys.maxsize}")
    return number


def _arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="email-parser",
        description="Decode a local .eml email into JSON. No network access is used.",
    )
    parser.add_argument("file", nargs="?", default="-", help="email file, or - for stdin (default)")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON output")
    parser.add_argument("-o", "--output", type=Path, help="write JSON to a new file instead of stdout")
    parser.add_argument(
        "--extract-attachments",
        type=Path,
        metavar="DIR",
        help="save attachment bytes in DIR with safe, unique filenames",
    )
    parser.add_argument(
        "--max-bytes",
        type=_positive_integer,
        default=DEFAULT_MAX_BYTES,
        metavar="N",
        help=f"maximum input size in bytes (default: {DEFAULT_MAX_BYTES}, 25 MiB)",
    )
    return parser


def _read_stream(source: BinaryIO, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        # A large limit should not allocate that much memory for a small email.
        chunk = source.read(min(64 * 1024, limit + 1 - size))
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise ValueError(f"input exceeds --max-bytes limit of {limit} bytes")
        chunks.append(chunk)


def _read_input(filename: str, limit: int) -> bytes:
    if filename == "-":
        return _read_stream(sys.stdin.buffer, limit)
    with Path(filename).open("rb") as source:
        return _read_stream(source, limit)


def _safe_filename(attachment: Attachment, index: int) -> str:
    # Normalize before stripping directory components, including Windows paths.
    name = unicodedata.normalize("NFKC", attachment.filename or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name).strip(" .")
    if not name:
        extension = mimetypes.guess_extension(attachment.content_type) or ".bin"
        name = f"attachment-{index:03d}{extension}"
    # Keep names portable and leave room for collision suffixes on common filesystems.
    if name.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "_" + name
    return name.encode("utf-8")[:180].decode("utf-8", errors="ignore")


def _open_new_file(path: Path) -> TextIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")


def _extract(
    attachments: list[Attachment], directory: Path, created: list[Path]
) -> list[Path]:
    target_directory = directory.resolve()
    target_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = []
    for index, attachment in enumerate(attachments, 1):
        filename = _safe_filename(attachment, index)
        name = Path(filename)
        for attempt in range(10_000):
            candidate = filename if attempt == 0 else f"{name.stem}-{attempt}{name.suffix}"
            path = target_directory / candidate
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            created.append(path)
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(attachment.data)
            paths.append(path)
            break
        else:
            raise FileExistsError(f"could not find an unused attachment filename for {filename!r}")
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments, emitting JSON on success and diagnostics on stderr."""
    args = _arguments().parse_args(argv)
    created: list[Path] = []
    output: TextIO | None = None
    try:
        parsed = parse_email(_read_input(args.file, args.max_bytes))
        document = parsed.to_dict()
        # Reserve output first so an existing output file fails before extraction.
        if args.output is not None:
            output = _open_new_file(args.output)
            created.append(args.output)
        if args.extract_attachments is not None:
            paths = _extract(parsed.attachments, args.extract_attachments, created)
            for metadata, path in zip(document["attachments"], paths):
                metadata["extracted_path"] = str(path)
        destination = output if output is not None else sys.stdout
        json.dump(document, destination, ensure_ascii=False, indent=2 if args.pretty else None)
        destination.write("\n")
        destination.flush()
        if output is not None:
            output.close()
        return 0
    except (OSError, ValueError) as error:
        if output is not None:
            with suppress(OSError):
                output.close()
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        print(f"email-parser: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
