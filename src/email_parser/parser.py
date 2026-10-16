"""Parse RFC 5322 messages into a small, JSON-friendly data model.

Parsing is entirely local. Attachments stay in memory, and their bytes are
deliberately omitted from the JSON representation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import policy
from email.errors import HeaderParseError
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Address:
    """A decoded display name and an email address."""

    name: str
    address: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "address": self.address}


@dataclass(frozen=True)
class Attachment:
    """An attachment's decoded payload and descriptive MIME metadata."""

    filename: str | None
    content_type: str
    size: int
    content_id: str | None
    disposition: str | None
    data: bytes = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return attachment metadata without embedding its binary payload."""
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "content_id": self.content_id,
            "disposition": self.disposition,
        }


@dataclass(frozen=True)
class ParsedEmail:
    """Decoded headers, bodies, attachments, and recoverable parsing issues."""

    subject: str
    from_: list[Address]
    to: list[Address]
    cc: list[Address]
    bcc: list[Address]
    reply_to: list[Address]
    date: str | None
    message_id: str | None
    text: str
    html: str
    attachments: list[Attachment]
    headers: list[dict[str, str]]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "from": [address.to_dict() for address in self.from_],
            "to": [address.to_dict() for address in self.to],
            "cc": [address.to_dict() for address in self.cc],
            "bcc": [address.to_dict() for address in self.bcc],
            "reply_to": [address.to_dict() for address in self.reply_to],
            "date": self.date,
            "message_id": self.message_id,
            "text": self.text,
            "html": self.html,
            "attachments": [attachment.to_dict() for attachment in self.attachments],
            "headers": [dict(header) for header in self.headers],
            "warnings": list(self.warnings),
        }


class _Warnings:
    """Keep diagnostics ordered and avoid reporting the same issue twice."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._seen: set[str] = set()

    def add(self, message: str) -> None:
        if message not in self._seen:
            self._seen.add(message)
            self.messages.append(message)

    def defects(self, defects: Any, context: str) -> None:
        for defect in defects:
            description = str(defect)
            detail = f": {description}" if description else ""
            self.add(f"{context}: {type(defect).__name__}{detail}")


def _decode_bytes(
    data: bytes, charset: str | None, warnings: _Warnings, context: str
) -> str:
    # UTF-8 also decodes ASCII and is a useful fallback for unlabelled messages.
    encoding = charset or "utf-8"
    try:
        return data.decode(encoding)
    except LookupError:
        warnings.add(f"{context}: unknown charset {encoding!r}; used UTF-8.")
        encoding = "utf-8"
    except (UnicodeError, ValueError):
        warnings.add(
            f"{context}: invalid {encoding!r} text; replaced undecodable characters."
        )
        try:
            return data.decode(encoding, errors="replace")
        except (LookupError, UnicodeError, ValueError):
            # A few Python codecs (for example IDNA) do not implement the
            # replacement error handler. They can still occur in MIME labels.
            warnings.add(f"{context}: could not use charset {encoding!r}; used UTF-8.")
            return data.decode("utf-8", errors="replace")
    try:
        return data.decode(encoding)
    except UnicodeError:
        warnings.add(
            f"{context}: invalid {encoding!r} text; replaced undecodable characters."
        )
        return data.decode(encoding, errors="replace")


def _clean_text(value: str, warnings: _Warnings, context: str) -> str:
    """Recover raw UTF-8 that BytesParser preserved with surrogateescape."""
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        try:
            raw = value.encode("utf-8", errors="surrogateescape")
        except UnicodeError:
            warnings.add(f"{context}: replaced invalid Unicode characters.")
            return value.encode("utf-8", errors="replace").decode("utf-8")
        return _decode_bytes(raw, "utf-8", warnings, context)
    return value


def _decode_header(value: str, warnings: _Warnings, context: str) -> str:
    unfolded = re.sub(r"(?:\r\n|\r|\n)[ \t]+", " ", value)
    try:
        fragments = decode_header(unfolded)
    except (HeaderParseError, ValueError) as error:
        warnings.add(f"{context}: could not decode encoded header ({error}).")
        return _clean_text(unfolded, warnings, context)
    return "".join(
        _decode_bytes(fragment, charset, warnings, context)
        if isinstance(fragment, bytes)
        else _clean_text(fragment, warnings, context)
        for fragment, charset in fragments
    )


def _read_headers(
    message: Message, warnings: _Warnings, context: str
) -> tuple[list[dict[str, str]], list[tuple[str, Any, str]]]:
    entries: list[dict[str, str]] = []
    parsed: list[tuple[str, Any, str]] = []
    for name, raw in message.raw_items():
        header_context = f"{context} {name} header"
        decoded = _decode_header(raw, warnings, header_context)
        entries.append({"name": name, "value": decoded})
        try:
            unfolded = re.sub(r"(?:\r\n|\r|\n)[ \t]+", " ", raw)
            header = policy.default.header_factory(name, unfolded)
            warnings.defects(header.defects, header_context)
        except (AttributeError, ValueError, IndexError, HeaderParseError) as error:
            warnings.add(f"{header_context}: could not fully parse header ({error}).")
            header = None
        parsed.append((name.lower(), header, decoded))
    return entries, parsed


def _addresses(
    headers: list[tuple[str, Any, str]], name: str, warnings: _Warnings
) -> list[Address]:
    result: list[Address] = []
    context = f"message {name} header"
    for header_name, header, decoded in headers:
        if header_name != name:
            continue
        try:
            addresses = header.addresses
        except (AttributeError, ValueError, IndexError, HeaderParseError):
            try:
                pairs = getaddresses([decoded])
            except (ValueError, IndexError) as error:
                warnings.add(f"{context}: could not parse addresses ({error}).")
                continue
        else:
            pairs = [(address.display_name, address.addr_spec) for address in addresses]
        for display_name, address in pairs:
            if address:
                result.append(
                    Address(
                        _clean_text(display_name, warnings, context),
                        _clean_text(address, warnings, context),
                    )
                )
    return result


def _first_header(headers: list[tuple[str, Any, str]], name: str) -> str | None:
    return next((value for key, _, value in headers if key == name), None)


def _date(value: str | None, warnings: _Warnings) -> str | None:
    if value is None:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (ValueError, TypeError, IndexError, OverflowError):
        warnings.add(f"message Date header: invalid date {value!r}.")
        return None


def _attachment_data(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    nested = part.get_payload()
    if isinstance(nested, list):
        if part.get_content_maintype() == "message":
            # The enclosing MIME headers describe the attachment, not the
            # attached email itself. Keep each encapsulated message intact.
            return b"\n".join(item.as_bytes(policy=policy.default) for item in nested)
        # A multipart attachment needs its MIME headers and boundaries to remain
        # useful as a standalone file.
        return part.as_bytes(policy=policy.default)
    if isinstance(nested, str):
        return nested.encode("utf-8", errors="surrogateescape")
    return b""


def _parse_email(data: bytes) -> ParsedEmail:
    message = BytesParser(policy=policy.default).parsebytes(data)
    warnings = _Warnings()
    headers, parsed_headers = _read_headers(message, warnings, "message")
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []

    def visit(part: Message, context: str) -> None:
        if part is not message:
            _read_headers(part, warnings, context)
        content_type = part.get_content_type()
        filename = part.get_filename()
        if filename is not None:
            filename = _clean_text(filename, warnings, f"{context} filename")
        disposition = part.get_content_disposition()
        is_attachment = (
            disposition == "attachment"
            or filename is not None
            or part.get_content_maintype() == "message"
        )
        if is_attachment or (
            not part.is_multipart() and content_type not in {"text/plain", "text/html"}
        ):
            payload = _attachment_data(part)
            raw_content_id = part.get("Content-ID")
            content_id = str(raw_content_id).strip() if raw_content_id is not None else None
            attachments.append(
                Attachment(
                    filename=filename,
                    content_type=content_type,
                    size=len(payload),
                    content_id=content_id,
                    disposition=disposition,
                    data=payload,
                )
            )
        elif part.is_multipart():
            for index, child in enumerate(part.get_payload(), 1):
                visit(child, f"{context} part {index}")
        else:
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                payload = b""
            body = _decode_bytes(payload, part.get_content_charset(), warnings, context)
            (html_parts if content_type == "text/html" else text_parts).append(body)
        # Some defects, particularly invalid base64, are detected on decoding.
        warnings.defects(part.defects, context)

    visit(message, "message")
    return ParsedEmail(
        subject=_first_header(parsed_headers, "subject") or "",
        from_=_addresses(parsed_headers, "from", warnings),
        to=_addresses(parsed_headers, "to", warnings),
        cc=_addresses(parsed_headers, "cc", warnings),
        bcc=_addresses(parsed_headers, "bcc", warnings),
        reply_to=_addresses(parsed_headers, "reply-to", warnings),
        date=_date(_first_header(parsed_headers, "date"), warnings),
        message_id=_first_header(parsed_headers, "message-id"),
        text="\n".join(text_parts),
        html="\n".join(html_parts),
        attachments=attachments,
        headers=headers,
        warnings=warnings.messages,
    )


def parse_email(data: bytes) -> ParsedEmail:
    """Parse an email, retaining recoverable problems in ``warnings``.

    MIME text/plain and text/html parts are collected separately in document
    order and joined with a newline. Attached emails and multipart attachments
    are kept intact instead of being folded into the containing message's body.
    Missing headers and headerless input are accepted. Excessive MIME or header
    nesting raises ``ValueError`` rather than overflowing Python's call stack.
    """
    if not isinstance(data, bytes):
        raise TypeError("parse_email expects bytes")
    try:
        return _parse_email(data)
    except RecursionError as error:
        raise ValueError("message nesting exceeds the supported limit") from error


def parse_file(path: str | Path) -> ParsedEmail:
    """Read and parse an .eml file, propagating normal filesystem errors."""
    return parse_email(Path(path).read_bytes())
