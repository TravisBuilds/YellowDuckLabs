"""Local wildfire document ingestion and retrieval.

The Community Wildfire Resiliency Plan is the municipality's own statement about
its wildfire situation, and the analyst should be able to quote it with a page
citation.

Retrieval is lexical in this iteration. The ``embedding`` column and the pgvector
extension are already provisioned, so switching to vector search is a change
here and nowhere else. Lexical retrieval is used rather than a bundled embedding
model because a wrong citation is worse than a slightly worse-ranked one.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from firewatch.config import REPO_ROOT
from firewatch.core.models import Document, DocumentChunk
from firewatch.core.municipality import MunicipalityConfig

log = logging.getLogger(__name__)

MIN_CHUNK_CHARS = 200
MAX_CHUNK_CHARS = 2000

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "is", "are",
    "what", "which", "does", "do", "say", "about", "this", "that", "with", "at",
    "how", "we", "our", "it", "its", "be", "as", "by", "from", "there",
}


def register_documents(
    session: Session, config: MunicipalityConfig
) -> list[dict]:
    """Create document rows and ingest any that are available locally."""
    results = []
    for spec in config.documents:
        document = session.get(Document, spec.id)
        if document is None:
            document = Document(id=spec.id, municipality_id=config.id)
            session.add(document)
        document.title = spec.title
        document.publisher = spec.publisher
        document.publication_date = spec.publication_date
        document.source_url = spec.url

        path = Path(spec.local_path) if spec.local_path else None
        if path and not path.is_absolute():
            path = REPO_ROOT / path

        if path is None:
            document.status = "not_configured"
            document.message = "No local_path is set for this document."
        elif not path.exists():
            document.status = "not_ingested"
            document.message = (
                f"The document is not present at {path}. The publisher's site blocks "
                "automated retrieval, so it must be downloaded manually from "
                f"{spec.url} and placed there. Until then the analyst cannot quote it "
                "and will say so."
            )
        else:
            try:
                chunks = _chunk_document(path)
            except Exception as exc:
                document.status = "failed"
                document.message = f"Could not read {path.name}: {exc}"
                chunks = []
            if chunks:
                session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == document.id)
                )
                for page, section, body in chunks:
                    session.add(
                        DocumentChunk(
                            document_id=document.id,
                            page=page,
                            section=section,
                            text=body,
                        )
                    )
                document.status = "ingested"
                document.message = f"{len(chunks)} passages indexed from {path.name}."
                document.ingested_at = datetime.now(timezone.utc)

        session.flush()
        results.append(
            {
                "document_id": document.id,
                "title": document.title,
                "status": document.status,
                "message": document.message,
            }
        )
    session.commit()
    return results


def _chunk_document(path: Path) -> list[tuple[int | None, str | None, str]]:
    """Split a document into passages, keeping page numbers for citation."""
    if path.suffix.lower() == ".pdf":
        pages = _extract_pdf_pages(path)
    else:
        pages = [(None, path.read_text(errors="replace"))]

    chunks: list[tuple[int | None, str | None, str]] = []
    for page, body in pages:
        for section, passage in _split_sections(body):
            chunks.append((page, section, passage))
    return chunks


def _extract_pdf_pages(path: Path) -> list[tuple[int, str]]:
    """Extract text per page.

    pypdf is optional; if it is absent we report that clearly rather than
    silently indexing nothing.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "PDF text extraction needs pypdf. Install it with 'pip install pypdf' "
            "to enable document retrieval."
        ) from exc

    reader = PdfReader(str(path))
    return [
        (index + 1, page.extract_text() or "")
        for index, page in enumerate(reader.pages)
    ]


_HEADING_RE = re.compile(r"^\s*((?:\d+\.)*\d+\s+[A-Z][^\n]{3,80}|[A-Z][A-Z \-]{6,80})\s*$")


def _split_sections(body: str) -> list[tuple[str | None, str]]:
    """Split on apparent headings, then pack into reasonable passage sizes."""
    lines = body.splitlines()
    current_section: str | None = None
    buffer: list[str] = []
    out: list[tuple[str | None, str]] = []

    def flush() -> None:
        joined = "\n".join(buffer).strip()
        buffer.clear()
        if len(joined) < MIN_CHUNK_CHARS:
            if joined and out:
                previous_section, previous_text = out[-1]
                out[-1] = (previous_section, f"{previous_text}\n{joined}")
            elif joined:
                out.append((current_section, joined))
            return
        while len(joined) > MAX_CHUNK_CHARS:
            split_at = joined.rfind("\n", 0, MAX_CHUNK_CHARS)
            if split_at < MIN_CHUNK_CHARS:
                split_at = MAX_CHUNK_CHARS
            out.append((current_section, joined[:split_at].strip()))
            joined = joined[split_at:].strip()
        if joined:
            out.append((current_section, joined))

    for line in lines:
        if _HEADING_RE.match(line):
            flush()
            current_section = line.strip()
            continue
        buffer.append(line)
    flush()
    return [(section, body) for section, body in out if body.strip()]


def search_documents(
    session: Session, municipality_id: str, query: str, limit: int = 5
) -> dict:
    """Lexical passage retrieval with page-level citations."""
    documents = session.scalars(
        select(Document).where(Document.municipality_id == municipality_id)
    ).all()

    if not documents:
        return {
            "query": query,
            "results": [],
            "status": "no_documents_configured",
            "note": "No local wildfire documents are configured for this municipality.",
        }

    ingested = [d for d in documents if d.status == "ingested"]
    if not ingested:
        return {
            "query": query,
            "results": [],
            "status": "no_documents_ingested",
            "documents": [
                {
                    "document_id": d.id,
                    "title": d.title,
                    "status": d.status,
                    "message": d.message,
                    "source_url": d.source_url,
                }
                for d in documents
            ],
            "note": (
                "Documents are configured but none are indexed, so the analyst "
                "cannot quote them. Any answer about what these documents say must "
                "state that they are unavailable."
            ),
        }

    terms = [
        t for t in re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", query.lower())
        if t not in _STOPWORDS
    ]
    if not terms:
        return {"query": query, "results": [], "note": "No searchable terms in query."}

    # websearch_to_tsquery handles multi-word queries without operator syntax.
    rows = session.execute(
        text(
            """
            SELECT c.document_id, d.title, d.source_url, c.page, c.section, c.text,
                   ts_rank(to_tsvector('english', c.text),
                           websearch_to_tsquery('english', :q)) AS rank
              FROM document_chunks c
              JOIN source_documents d ON d.id = c.document_id
             WHERE d.municipality_id = :m
               AND d.status = 'ingested'
               AND to_tsvector('english', c.text) @@ websearch_to_tsquery('english', :q)
             ORDER BY rank DESC
             LIMIT :lim
            """
        ).bindparams(q=" ".join(terms), m=municipality_id, lim=min(limit, 20)),
    ).all()

    return {
        "query": query,
        "status": "ok",
        "results": [
            {
                "document_id": r[0],
                "title": r[1],
                "source_url": r[2],
                "page": r[3],
                "section": r[4],
                "text": r[5][:1500],
                "rank": float(r[6]),
            }
            for r in rows
        ],
        "note": (
            "These are passages from the municipality's own documents. A statement "
            "in a plan is not a geospatial fact and should not be presented as one."
        ),
    }
