"""A minimal, dependency-free text PDF writer.

The seeded invoices must be *real* PDFs -- the operator extracts text from them
with pypdf, so handing it a renamed text file would quietly fake the hardest
part of the drive surface. Pulling in reportlab just to emit a few hundred bytes
of Helvetica felt like the wrong trade, so this writes the PDF structure
directly: one page, one Type1 base font, correct cross-reference offsets.

Text is encoded as PDFDocEncoding (a Latin-1 superset), so the seed data uses
"INR" rather than the rupee sign. Embedding a Unicode font to render one glyph
would be a lot of machinery for no demonstrative value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

_PAGE_WIDTH = 595
_PAGE_HEIGHT = 842
_MARGIN_X = 56
_TOP_Y = 790
_LEADING = 14


def _escape(text: str) -> bytes:
    """Escape the three characters that are special inside a PDF string."""
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1", errors="replace")


def _content_stream(lines: Sequence[str], font_size: int) -> bytes:
    parts = [
        b"BT",
        f"/F1 {font_size} Tf".encode("ascii"),
        f"{_MARGIN_X} {_TOP_Y} Td".encode("ascii"),
        f"{_LEADING} TL".encode("ascii"),
    ]
    for line in lines:
        parts.append(b"(" + _escape(line) + b") Tj T*")
    parts.append(b"ET")
    return b"\n".join(parts)


def write_pdf(path: Path, lines: Sequence[str], *, font_size: int = 11, title: str = "") -> Path:
    """Write ``lines`` to a single-page PDF at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _content_stream(lines, font_size)

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
            "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ).encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
        (
            "<< /Title (" + title.replace("(", "").replace(")", "") + ") "
            "/Producer (centralign-sandbox) >>"
        ).encode("latin-1", errors="replace"),
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")

    path.write_bytes(bytes(out))
    return path
