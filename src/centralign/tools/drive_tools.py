"""The shared-drive surface: list, read and file documents.

Reading is split from listing on purpose. An operator that had to read every
file to find out what it was would burn its context on a canteen menu; listing
returns cheap metadata so the plan can decide what is worth opening.

All paths are resolved inside the configured drive root. A tool that accepts a
path from a model and hands it straight to ``open()`` is a path-traversal
primitive, and the model does not have to be adversarial for that to go wrong --
one hallucinated ``../../`` is enough.
"""

from __future__ import annotations

import csv
import io
import shutil
from pathlib import Path
from typing import Any

from ..runtime.state import Observation, RiskTier
from .base import FailureKind, Tool, ToolContext, ToolFailure

_TEXT_SUFFIXES = {".txt", ".md", ".log"}
_MAX_CHARS = 20000


def _resolve_in_drive(ctx: ToolContext, relative: str) -> Path:
    """Resolve a drive-relative path, refusing anything that escapes the root."""
    root = ctx.settings.drive_dir.resolve()
    candidate = (root / str(relative).strip().lstrip("/\\")).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolFailure(
            f"path {relative!r} resolves outside the drive root",
            kind=FailureKind.PERMISSION,
            hint="use a path relative to the drive root, with no '..' segments",
        )
    return candidate


class ListDriveFiles(Tool):
    name = "drive_list"
    surface = "drive"
    description = (
        "List files in a shared-drive folder with name, size and modified time. "
        "Does not read file contents. Use this first to see what work exists."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "folder": {
                "type": "string",
                "description": "Drive-relative folder, e.g. 'invoices-inbox'. Empty for the root.",
            },
            "pattern": {
                "type": "string",
                "description": "Optional glob filter, e.g. '*.pdf'.",
            },
        },
        "required": [],
    }
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        folder = _resolve_in_drive(ctx, args.get("folder", ""))
        if not folder.exists():
            raise ToolFailure(
                f"folder {args.get('folder', '')!r} does not exist in the drive",
                kind=FailureKind.NOT_FOUND,
                hint=f"existing folders: {', '.join(_subfolders(ctx))}",
            )
        if not folder.is_dir():
            raise ToolFailure(
                f"{args.get('folder')!r} is a file, not a folder", kind=FailureKind.INVALID_ARGS
            )

        pattern = args.get("pattern") or "*"
        files = [
            {
                "name": path.name,
                "path": _drive_relative(ctx, path),
                "size_bytes": path.stat().st_size,
                "suffix": path.suffix.lower(),
            }
            for path in sorted(folder.glob(pattern))
            if path.is_file()
        ]
        listing = "\n".join(f"- {f['path']} ({f['size_bytes']} bytes)" for f in files) or "(empty)"
        return Observation(
            ok=True,
            summary=f"{len(files)} file(s) in '{args.get('folder', '')or '/'}':\n{listing}",
            data={"files": files, "count": len(files), "folder": args.get("folder", "")},
        )


class ReadDocument(Tool):
    name = "drive_read"
    surface = "drive"
    description = (
        "Read a document from the shared drive as text. Handles PDF, CSV and plain "
        "text. Returns the extracted text so its contents can be interpreted."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Drive-relative path to the file."}
        },
        "required": ["path"],
    }
    risk = RiskTier.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        path = _resolve_in_drive(ctx, args["path"])
        if not path.exists():
            raise ToolFailure(
                f"file {args['path']!r} not found in the drive",
                kind=FailureKind.NOT_FOUND,
                hint="run drive_list to see what is actually there",
            )

        suffix = path.suffix.lower()
        if suffix == ".pdf":
            text, pages = _read_pdf(path)
            kind = "pdf"
            extra: dict[str, Any] = {"pages": pages}
        elif suffix == ".csv":
            text, rows = _read_csv(path)
            kind = "csv"
            extra = {"rows": rows}
        elif suffix in _TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="replace")
            kind = "text"
            extra = {}
        else:
            raise ToolFailure(
                f"cannot read {suffix or 'extensionless'} files",
                kind=FailureKind.INVALID_ARGS,
                hint="supported: .pdf, .csv, .txt, .md, .log",
            )

        truncated = len(text) > _MAX_CHARS
        body = text[:_MAX_CHARS] + ("\n[...truncated]" if truncated else "")
        return Observation(
            ok=True,
            summary=f"Contents of {args['path']} ({kind}):\n{body}",
            data={"path": args["path"], "kind": kind, "text": body, "truncated": truncated, **extra},
        )


class FileDocument(Tool):
    name = "drive_move"
    surface = "drive"
    description = (
        "Move a document to another drive folder, e.g. filing a handled invoice into "
        "'invoices-processed' or an exception into 'invoices-exceptions'."
    )
    args_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Drive-relative path of the file to move."},
            "to_folder": {"type": "string", "description": "Destination drive-relative folder."},
        },
        "required": ["path", "to_folder"],
    }
    risk = RiskTier.WRITE
    reversible = True

    def preview(self, args: dict[str, Any]) -> str:
        return f"Move {args.get('path')} into {args.get('to_folder')}/ on the shared drive"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> Observation:
        source = _resolve_in_drive(ctx, args["path"])
        destination_dir = _resolve_in_drive(ctx, args["to_folder"])
        if not source.exists():
            raise ToolFailure(
                f"file {args['path']!r} not found", kind=FailureKind.NOT_FOUND
            )
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name

        if destination.exists():
            # Already filed. Report success rather than failure: the desired end
            # state holds, and a retry of this step must not be a hard error.
            return Observation(
                ok=True,
                summary=f"{source.name} is already present in {args['to_folder']}; nothing to do.",
                data={"path": _drive_relative(ctx, destination), "already_present": True},
            )

        shutil.move(str(source), str(destination))
        return Observation(
            ok=True,
            summary=f"Moved {source.name} to {args['to_folder']}/.",
            data={
                "from": args["path"],
                "to": _drive_relative(ctx, destination),
                "already_present": False,
            },
        )


def _read_pdf(path: Path) -> tuple[str, int]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, OSError, ValueError) as exc:
        raise ToolFailure(
            f"could not parse {path.name} as a PDF: {exc}",
            kind=FailureKind.INVALID_ARGS,
            hint="the file may be corrupt or image-only; a human may need to look at it",
        ) from exc

    text = "\n".join(pages).strip()
    if not text:
        # A scanned PDF is a real operational case and needs a human, not a retry.
        raise ToolFailure(
            f"{path.name} contains no extractable text (likely a scan or image-only PDF)",
            kind=FailureKind.AMBIGUOUS,
            hint="escalate to a human for manual entry; OCR is not available",
        )
    return text, len(pages)


def _read_csv(path: Path) -> tuple[str, list[dict[str, str]]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    return raw, rows


def _subfolders(ctx: ToolContext) -> list[str]:
    root = ctx.settings.drive_dir
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _drive_relative(ctx: ToolContext, path: Path) -> str:
    try:
        return path.resolve().relative_to(ctx.settings.drive_dir.resolve()).as_posix()
    except ValueError:
        return path.name


DRIVE_TOOLS = [ListDriveFiles(), ReadDocument(), FileDocument()]
