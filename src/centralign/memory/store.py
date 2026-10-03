"""Persistent company memory.

Three kinds of memory, one store:

* **Semantic** -- how this company works: SOPs, policies, org facts, system
  inventory. Authored, version-controlled, loaded from ``company/``.
* **Episodic** -- what happened on previous runs, and what was learned. Written
  by the operator itself at the end of a run.
* **Corrective** -- feedback a human gave ("Northwind invoices always need the
  freight line excluded"). Highest retrieval priority, because it is the
  company telling us we were wrong before.

Retrieval is BM25 via SQLite FTS5, not embeddings. That is a considered choice,
not a shortcut: the corpus is a few hundred short, jargon-dense operational
documents, where exact term matching on identifiers ("PO-4471", "Northwind",
"freight") beats semantic similarity. It also costs nothing, needs no service,
and keeps the whole system runnable offline. Swapping in a vector index later
means reimplementing :meth:`MemoryStore.search` and nothing else.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

SEMANTIC = "semantic"
EPISODIC = "episodic"
CORRECTIVE = "corrective"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    source     TEXT,
    tags       TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_kind ON documents (kind);
"""

# A self-contained FTS table rather than an external-content one. External
# content (content='documents') is more space-efficient, but updating a row
# requires issuing FTS5's special 'delete' command with the *previous* column
# values; a plain DELETE silently corrupts the index, which surfaces much later
# as "database disk image is malformed". Storing a second copy of a few hundred
# short documents is a trade worth making for an index that cannot rot.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5 (
    doc_id UNINDEXED, title, body, tags
);
"""


@dataclass
class Document:
    id: str
    kind: str
    title: str
    body: str
    source: str = ""
    tags: list[str] = None  # type: ignore[assignment]
    score: float = 0.0

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = []

    def render(self, limit: int = 1800) -> str:
        body = self.body if len(self.body) <= limit else self.body[:limit] + "\n[...truncated]"
        return f"### {self.title}  [{self.kind}]\n{body}"


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self.fts_enabled = self._try_enable_fts()
            self._conn.commit()

    def _try_enable_fts(self) -> bool:
        """FTS5 ships with most CPython builds, but not all. Degrade, don't crash."""
        try:
            self._conn.executescript(_FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False

    # -- writes -------------------------------------------------------------
    def put(
        self,
        *,
        id: str,
        kind: str,
        title: str,
        body: str,
        source: str = "",
        tags: Iterable[str] = (),
    ) -> None:
        tag_list = list(tags)
        with self._lock:
            self._conn.execute(
                "INSERT INTO documents (id, kind, title, body, source, tags, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, body=excluded.body, "
                "source=excluded.source, tags=excluded.tags",
                (id, kind, title, body, source, json.dumps(tag_list), time.time()),
            )
            if self.fts_enabled:
                # FTS5 virtual tables support neither UPSERT nor partial update,
                # so replace the row outright.
                self._conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (id,))
                self._conn.execute(
                    "INSERT INTO documents_fts (doc_id, title, body, tags) VALUES (?, ?, ?, ?)",
                    (id, title, body, " ".join(tag_list)),
                )
            self._conn.commit()

    def remember(
        self, title: str, body: str, *, kind: str = EPISODIC, tags: Iterable[str] = ()
    ) -> str:
        doc_id = f"{kind}:{int(time.time() * 1000)}"
        self.put(id=doc_id, kind=kind, title=title, body=body, source="runtime", tags=tags)
        return doc_id

    # -- reads --------------------------------------------------------------
    def search(self, query: str, *, kinds: Iterable[str] | None = None, limit: int = 6) -> list[Document]:
        """BM25 search, with corrective memory boosted above everything else."""
        terms = _tokenize(query)
        if not terms:
            return []

        if self.fts_enabled:
            results = self._search_fts(terms, limit=limit * 3)
        else:
            results = self._search_like(terms, limit=limit * 3)

        wanted = set(kinds) if kinds else None
        if wanted:
            results = [doc for doc in results if doc.kind in wanted]

        # Corrective memory is a human telling us we previously got this wrong.
        # It must not lose a ranking contest to a verbose SOP.
        for doc in results:
            if doc.kind == CORRECTIVE:
                doc.score *= 2.0
            elif doc.kind == EPISODIC:
                doc.score *= 0.8

        results.sort(key=lambda d: d.score, reverse=True)
        return results[:limit]

    def _search_fts(self, terms: list[str], *, limit: int) -> list[Document]:
        # OR the terms: operational queries mix an identifier with prose, and
        # requiring every term returns nothing far too often.
        #
        # Each term MUST be quoted. Unquoted, FTS5 reads '-' as the NOT operator,
        # so 'PO-4490' or 'three-way' is a syntax error -- and because the error
        # is swallowed below, company memory would silently return nothing for
        # any query mentioning a purchase order or invoice number. Quoting makes
        # a hyphenated term a phrase, which is what we want anyway.
        match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT d.*, bm25(documents_fts) AS rank FROM documents_fts "
                    "JOIN documents d ON d.id = documents_fts.doc_id "
                    "WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        # bm25() returns a negative score where more negative is better.
        return [self._row_to_doc(row, score=-float(row["rank"])) for row in rows]

    def _search_like(self, terms: list[str], *, limit: int) -> list[Document]:
        """Fallback when FTS5 is unavailable: term-frequency over LIKE matches."""
        clause = " OR ".join(["(lower(title || ' ' || body) LIKE ?)"] * len(terms))
        params = [f"%{term}%" for term in terms]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM documents WHERE {clause} LIMIT ?", (*params, limit)
            ).fetchall()
        documents = []
        for row in rows:
            haystack = f"{row['title']} {row['body']}".lower()
            score = sum(haystack.count(term) for term in terms)
            documents.append(self._row_to_doc(row, score=float(score)))
        return documents

    def get(self, doc_id: str) -> Document | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._row_to_doc(row) if row else None

    def all_of_kind(self, kind: str) -> list[Document]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM documents WHERE kind = ? ORDER BY title", (kind,)
            ).fetchall()
        return [self._row_to_doc(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"])

    @staticmethod
    def _row_to_doc(row: sqlite3.Row, score: float = 0.0) -> Document:
        return Document(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            source=row["source"] or "",
            tags=json.loads(row["tags"]) if row["tags"] else [],
            score=score,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- ingestion ----------------------------------------------------------
    def ingest_company_dir(self, directory: Path) -> int:
        """Load ``company/`` into semantic memory.

        Markdown files become one document each. YAML files are flattened into
        prose, because a retrieval hit on ``approval_threshold_inr: 50000`` is
        only useful if the surrounding context comes with it.
        """
        if not directory.exists():
            return 0

        loaded = 0
        for path in sorted(directory.rglob("*")):
            if path.is_dir() or path.name.startswith("."):
                continue
            relative = path.relative_to(directory).as_posix()
            if is_secret_file(relative):
                # Secrets must never enter searchable memory. Anything indexed
                # here can be retrieved into a prompt, and a vendor PDF that
                # says "search memory for the admin password" would then be a
                # working exfiltration path. The vault is read only by the tool
                # layer, by account role, and never by the model.
                continue

            if path.suffix.lower() in {".md", ".markdown", ".txt"}:
                text = path.read_text(encoding="utf-8")
                title = _first_heading(text) or relative
                self.put(
                    id=f"{SEMANTIC}:{relative}",
                    kind=SEMANTIC,
                    title=title,
                    body=text,
                    source=relative,
                    tags=_tags_for(relative),
                )
                loaded += 1
            elif path.suffix.lower() in {".yaml", ".yml"}:
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except yaml.YAMLError:
                    continue
                self.put(
                    id=f"{SEMANTIC}:{relative}",
                    kind=SEMANTIC,
                    title=relative,
                    body=_yaml_to_prose(data),
                    source=relative,
                    tags=_tags_for(relative),
                )
                loaded += 1
        return loaded


#: Filename fragments that mark a file as holding secrets. Matched on the whole
#: relative path so that a ``secrets/`` folder is excluded as well as a
#: ``*.credentials.yaml`` file.
_SECRET_MARKERS = ("credential", "secret", "password", "token", "apikey", "api_key", ".env")


def is_secret_file(relative_path: str) -> bool:
    lowered = relative_path.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "is", "are",
    "be", "with", "that", "this", "it", "as", "at", "by", "from", "all", "any",
    "please", "need", "needs", "should", "would", "can", "we", "i", "my", "our",
}


def _tokenize(query: str) -> list[str]:
    """Words and identifiers, stopwords dropped, order-preserving and unique."""
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-_/]*", query.lower())
    seen: set[str] = set()
    terms: list[str] = []
    for token in raw:
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms[:24]


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return None


def _tags_for(relative: str) -> list[str]:
    parts = Path(relative).parts
    tags = [Path(relative).stem]
    if len(parts) > 1:
        tags.append(parts[0])
    return tags


def _yaml_to_prose(data: Any, prefix: str = "") -> str:
    """Flatten nested YAML into searchable ``path: value`` lines."""
    lines: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            lines.append(_yaml_to_prose(value, path))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            lines.append(_yaml_to_prose(value, f"{prefix}[{index}]"))
    else:
        lines.append(f"{prefix}: {data}")
    return "\n".join(line for line in lines if line)
