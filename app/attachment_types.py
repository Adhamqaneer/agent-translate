"""Resolve supported attachment types without trusting or opening filenames."""

import re


IMAGE_MIME_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
    "image/gif", "image/avif", "image/bmp",
})
DOCUMENT_MIME_TYPES = frozenset({
    "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "application/vnd.oasis.opendocument.text", "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.presentation",
})
SPREADSHEET_MIME_TYPES = frozenset({
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.ms-excel.sheet.binary.macroenabled.12",
    "application/vnd.ms-excel.sheet.macroenabled.12",
})
RTF_MIME_TYPES = frozenset({"application/rtf"})
TEXT_MIME_TYPES = frozenset({
    "text/plain", "text/html", "text/css", "text/csv", "text/tab-separated-values",
    "text/markdown", "text/xml", "text/javascript", "text/x-typescript",
    "text/x-python", "text/x-java-source", "text/x-c", "text/x-c++",
    "text/x-csharp", "text/x-go", "text/x-rust", "text/x-shellscript",
    "text/richtext", "application/json", "application/xml",
    "application/xhtml+xml", "application/javascript", "application/x-javascript",
    "application/x-typescript", "application/x-ipynb+json", "application/yaml",
    "application/x-yaml", "application/sql", "application/x-python-code",
    "application/x-latex", "image/svg+xml",
})
# This finite catalog is useful for documentation. resolve_mime_type also
# recognizes valid text/* subtypes beyond those listed here.
SUPPORTED_MIME_TYPES = (
    IMAGE_MIME_TYPES | DOCUMENT_MIME_TYPES | SPREADSHEET_MIME_TYPES
    | TEXT_MIME_TYPES | RTF_MIME_TYPES
)

MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "image/x-ms-bmp": "image/bmp",
    "application/x-pdf": "application/pdf",
    "application/x-msexcel": "application/vnd.ms-excel",
    "text/x-markdown": "text/markdown",
    "text/rtf": "application/rtf",
    "application/x-rtf": "application/rtf",
}
GENERIC_MIME_TYPES = frozenset({
    "application/octet-stream", "binary/octet-stream",
    "application/zip", "application/x-zip-compressed",
})
EXTENSION_TO_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".xlsb": "application/vnd.ms-excel.sheet.binary.macroenabled.12",
    ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
    ".gif": "image/gif", ".avif": "image/avif", ".bmp": "image/bmp",
    ".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".json": "application/json", ".ipynb": "application/x-ipynb+json",
    ".yaml": "application/yaml", ".yml": "application/yaml",
    ".html": "text/html", ".htm": "text/html", ".xml": "application/xml",
    ".css": "text/css", ".js": "text/javascript", ".ts": "text/x-typescript",
    ".py": "text/x-python", ".java": "text/x-java-source",
    ".c": "text/x-c", ".h": "text/x-c", ".cpp": "text/x-c++",
    ".cs": "text/x-csharp", ".go": "text/x-go", ".rs": "text/x-rust",
    ".sh": "text/x-shellscript", ".sql": "application/sql",
    ".log": "text/plain", ".ini": "text/plain", ".toml": "text/plain",
    ".rtf": "application/rtf", ".tex": "application/x-latex",
    ".svg": "image/svg+xml",
}
_MIME_PATTERN = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
)


def _normalize_mime_type(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 256 or not value.isascii():
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    mime_type = value.split(";", 1)[0].strip().lower()
    if not _MIME_PATTERN.fullmatch(mime_type):
        return None
    return MIME_ALIASES.get(mime_type, mime_type)


def resolve_mime_type(mime_type: object, filename: str | None = None) -> str | None:
    """Return a canonical supported type, with suffix hints only for generic MIME.

    A filename is never opened, executed, interpreted as a URL, or used as a
    path. Its known suffix can hint at a generic binary/ZIP payload's type;
    readers must still validate that payload. An explicit unsupported type
    cannot be made acceptable by renaming its file.
    """
    normalized = _normalize_mime_type(mime_type)
    if normalized is None:
        return None
    if normalized in GENERIC_MIME_TYPES:
        if not isinstance(filename, str) or not filename or len(filename) > 4096:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in filename):
            return None
        basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        _, separator, suffix = basename.rpartition(".")
        if not separator:
            return None
        return EXTENSION_TO_MIME.get("." + suffix.lower())
    if normalized in SUPPORTED_MIME_TYPES or normalized.startswith("text/"):
        return normalized
    return None


def is_text_type(mime: str) -> bool:
    """Identify plain-text types, excluding structured RTF payloads."""
    normalized = resolve_mime_type(mime)
    return normalized is not None and (
        normalized in TEXT_MIME_TYPES or normalized.startswith("text/")
    )
