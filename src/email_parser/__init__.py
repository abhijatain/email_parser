"""Local .eml parsing with decoded MIME content and JSON-ready metadata."""

from .parser import Address, Attachment, ParsedEmail, parse_email, parse_file

__all__ = ["Address", "Attachment", "ParsedEmail", "parse_email", "parse_file"]
