"""Pre-filtered lexical retrieval, parent eligibility recheck, and grounded answering."""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db import connect, get_current_publication_id, init_db

ABSTAIN_MESSAGE = "I couldn't find an approved source that answers that question."

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DOC_ID_RE = re.compile(r"\b([A-Z]{2,6}-\d{2,6})\b")
_ENTITY_RE = re.compile(r"(?<!^)(?<![.!?]\s)\b([A-Z][a-zA-Z0-9-]{2,}|[A-Z]+\d+[A-Z0-9-]*)\b")

_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "s",
        "should",
        "t",
        "that",
        "the",
        "their",
        "them",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_WEEKDAY_TERMS = frozenset({"weekday", "weekdays", "monday", "tuesday", "wednesday", "thursday", "friday"})
_WEEKEND_TERMS = frozenset({"weekend", "weekends", "saturday", "sunday"})
_DIRECTION_TERMS = frozenset({"north", "south", "east", "west"})
_GEO_SCOPE_GROUPS = (
    (frozenset({"domestic"}), frozenset({"international"})),
    (_WEEKDAY_TERMS, _WEEKEND_TERMS),
    (frozenset({"daytime"}), frozenset({"overnight", "graveyard"})),
)


@dataclass(frozen=True)
class Citation:
    doc_id: str
    version: str
    locator: str

    def as_dict(self) -> dict[str, str]:
        return {
            "doc_id": self.doc_id,
            "version": self.version,
            "locator": self.locator,
        }


@dataclass(frozen=True)
class AnswerResult:
    answer: str
    abstained: bool
    citations: list[Citation]

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "abstained": self.abstained,
            "citations": [c.as_dict() for c in self.citations],
        }


@dataclass(frozen=True)
class _Candidate:
    chunk_id: int
    parent_id: int
    doc_row_id: int
    doc_id: str
    version: str
    text: str
    bm25_score: float


@dataclass(frozen=True)
class _AuthorizedParent:
    parent_id: int
    doc_row_id: int
    doc_id: str
    version: str
    locator: str
    heading: str
    content: str


def _abstain() -> AnswerResult:
    return AnswerResult(answer=ABSTAIN_MESSAGE, abstained=True, citations=[])


def _stem_token(token: str) -> str:
    if len(token) <= 3:
        return token
    for suffix in ("ances", "ance", "ences", "ence", "ings", "ing", "ies", "ied", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            if suffix in ("ies", "ied"):
                return token[: -len(suffix)] + "y"
            return token[: -len(suffix)]
    return token


def _tokenize_content(text: str) -> list[str]:
    raw = _TOKEN_RE.findall(text.lower())
    return [tok for tok in raw if tok not in _STOPWORDS and (len(tok) > 1 or tok.isdigit())]


def _stemmed_set(text: str) -> set[str]:
    return {_stem_token(t) for t in _tokenize_content(text)}


def _build_fts_query(tokens: list[str]) -> str:
    seen: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.append(tok)
    if not seen:
        return ""
    return " OR ".join(f'"{tok}"' for tok in seen)


def _retrieve_candidates(
    conn: sqlite3.Connection,
    pub_id: int,
    roles: tuple[str, ...],
    fts_query: str,
    limit: int = 25,
) -> list[_Candidate]:
    if not roles or not fts_query:
        return []

    placeholders = ", ".join("?" for _ in roles)
    sql = f"""
        SELECT
            c.id AS chunk_id,
            c.parent_id AS parent_id,
            c.doc_row_id AS doc_row_id,
            d.doc_id AS doc_id,
            d.version AS version,
            c.text AS text,
            bm25(chunks_fts) AS bm25_rank
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN published_docs d ON d.id = c.doc_row_id
        WHERE chunks_fts MATCH ?
          AND c.publication_id = ?
          AND d.lifecycle = 'active'
          AND EXISTS (
              SELECT 1
              FROM doc_roles r
              WHERE r.doc_row_id = d.id
                AND r.role IN ({placeholders})
          )
        ORDER BY bm25_rank ASC
        LIMIT ?
    """
    params: list[Any] = [fts_query, pub_id, *roles, limit]
    rows = conn.execute(sql, params).fetchall()
    return [
        _Candidate(
            chunk_id=int(row["chunk_id"]),
            parent_id=int(row["parent_id"]),
            doc_row_id=int(row["doc_row_id"]),
            doc_id=str(row["doc_id"]),
            version=str(row["version"]),
            text=str(row["text"]),
            bm25_score=-float(row["bm25_rank"]),
        )
        for row in rows
    ]


def _load_authorized_parent(
    conn: sqlite3.Connection,
    parent_id: int,
    pub_id: int,
    roles: tuple[str, ...],
) -> _AuthorizedParent | None:
    if not roles:
        return None
    placeholders = ", ".join("?" for _ in roles)
    row = conn.execute(
        f"""
        SELECT
            p.id AS parent_id,
            p.doc_row_id AS doc_row_id,
            d.doc_id AS doc_id,
            d.version AS version,
            p.locator AS locator,
            p.heading AS heading,
            p.content AS content
        FROM parents p
        JOIN published_docs d ON d.id = p.doc_row_id
        WHERE p.id = ?
          AND p.publication_id = ?
          AND d.lifecycle = 'active'
          AND EXISTS (
              SELECT 1
              FROM doc_roles r
              WHERE r.doc_row_id = d.id
                AND r.role IN ({placeholders})
          )
        """,
        [parent_id, pub_id, *roles],
    ).fetchone()
    if row is None:
        return None
    return _AuthorizedParent(
        parent_id=int(row["parent_id"]),
        doc_row_id=int(row["doc_row_id"]),
        doc_id=str(row["doc_id"]),
        version=str(row["version"]),
        locator=str(row["locator"]),
        heading=str(row["heading"]),
        content=str(row["content"]),
    )


def _load_authorized_doc_text(
    conn: sqlite3.Connection,
    doc_row_id: int,
    pub_id: int,
    roles: tuple[str, ...],
) -> str:
    if not roles:
        return ""
    placeholders = ", ".join("?" for _ in roles)
    rows = conn.execute(
        f"""
        SELECT p.content
        FROM parents p
        JOIN published_docs d ON d.id = p.doc_row_id
        WHERE p.doc_row_id = ?
          AND p.publication_id = ?
          AND d.lifecycle = 'active'
          AND EXISTS (
              SELECT 1
              FROM doc_roles r
              WHERE r.doc_row_id = d.id
                AND r.role IN ({placeholders})
          )
        ORDER BY p.id ASC
        """,
        [doc_row_id, pub_id, *roles],
    ).fetchall()
    return "\n".join(str(r["content"]) for r in rows)


def _has_scope_contradiction(
    question: str,
    candidate_doc_id: str,
    full_doc_text: str,
) -> bool:
    doc_lower = full_doc_text.lower()
    doc_words = set(_TOKEN_RE.findall(doc_lower))
    q_lower = question.lower()
    q_words = set(_TOKEN_RE.findall(q_lower))

    explicit_ids = _DOC_ID_RE.findall(question)
    if explicit_ids:
        if candidate_doc_id.upper() not in {eid.upper() for eid in explicit_ids}:
            if not any(eid.lower() in doc_lower for eid in explicit_ids):
                return True

    for group_a, group_b in _GEO_SCOPE_GROUPS:
        q_a = bool(q_words & group_a)
        q_b = bool(q_words & group_b)
        d_a = bool(doc_words & group_a)
        d_b = bool(doc_words & group_b)
        if q_a and not q_b and d_b and not d_a:
            return True
        if q_b and not q_a and d_a and not d_b:
            return True

    q_dirs = q_words & _DIRECTION_TERMS
    if q_dirs:
        missing_dirs = q_dirs - doc_words
        if missing_dirs and (doc_words & _DIRECTION_TERMS):
            return True

    return False


def _compute_idf(
    query_stems: set[str],
    candidates: list[_Candidate],
) -> dict[str, float]:
    n = max(len(candidates), 1)
    doc_freq: dict[str, int] = {stem: 0 for stem in query_stems}
    for cand in candidates:
        c_stems = _stemmed_set(cand.text)
        for stem in query_stems:
            if stem in c_stems:
                doc_freq[stem] += 1
    return {
        stem: math.log((n + 1.0) / (df + 1.0)) + 1.0
        for stem, df in doc_freq.items()
    }


def _extract_entity_stems(question: str) -> set[str]:
    entities: set[str] = set()
    for match in _ENTITY_RE.findall(question):
        for tok in _tokenize_content(match):
            entities.add(_stem_token(tok))
    return entities


def _score_candidate(
    candidate: _Candidate,
    query_stems: set[str],
    entity_stems: set[str],
    doc_stems: set[str],
    idf_weights: dict[str, float],
) -> tuple[float, int]:
    chunk_stems = _stemmed_set(candidate.text)
    matched_chunk = query_stems & chunk_stems
    matched_doc_only = (query_stems & doc_stems) - chunk_stems

    chunk_idf = sum(idf_weights.get(s, 1.0) for s in matched_chunk)
    entity_idf = sum(idf_weights.get(s, 1.0) for s in (entity_stems & chunk_stems))
    doc_only_idf = sum(idf_weights.get(s, 1.0) for s in matched_doc_only)
    is_table_row = " | " in candidate.text and ":" in candidate.text
    row_bonus = 0.5 if is_table_row and len(matched_chunk) >= 2 else 0.0

    score = (chunk_idf * 2.5) + (entity_idf * 3.5) + (doc_only_idf * 0.4) + (candidate.bm25_score * 0.6) + row_bonus
    return score, len(matched_chunk)


def _extract_concise_answer(candidate: _Candidate, parent: _AuthorizedParent) -> str:
    if " | " in candidate.text and " — " in candidate.text:
        row_part = candidate.text.rsplit(" — ", 1)[-1].strip()
        if ":" in row_part:
            fields = [part.strip() for part in row_part.split(" | ") if part.strip()]
            return "; ".join(fields) + "."

    body_lines = [
        line.strip()
        for line in parent.content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if body_lines:
        return " ".join(body_lines)

    if ": " in candidate.text:
        return candidate.text.split(": ", 1)[1].strip()
    return candidate.text.strip()


def answer_question(
    question: str,
    roles: tuple[str, ...],
    index_path: str | Path | None = None,
) -> AnswerResult:
    cleaned_question = question.strip()
    if not cleaned_question or not roles:
        return _abstain()

    query_tokens = _tokenize_content(cleaned_question)
    fts_query = _build_fts_query(query_tokens)
    if not fts_query:
        return _abstain()

    query_stems = {_stem_token(t) for t in query_tokens}
    entity_stems = _extract_entity_stems(cleaned_question)

    conn = connect(index_path)
    try:
        init_db(conn)
        conn.execute("BEGIN DEFERRED;")
        try:
            pub_id = get_current_publication_id(conn)
            if pub_id is None:
                return _abstain()

            candidates = _retrieve_candidates(conn, pub_id, roles, fts_query)
            if not candidates:
                return _abstain()

            doc_text_cache: dict[int, str] = {}
            doc_stems_cache: dict[int, set[str]] = {}
            idf_weights = _compute_idf(query_stems, candidates)
            ranked: list[tuple[float, int, _Candidate]] = []

            for cand in candidates:
                if cand.doc_row_id not in doc_text_cache:
                    full_text = _load_authorized_doc_text(conn, cand.doc_row_id, pub_id, roles)
                    doc_text_cache[cand.doc_row_id] = full_text
                    doc_stems_cache[cand.doc_row_id] = _stemmed_set(full_text)

                full_text = doc_text_cache[cand.doc_row_id]
                if not full_text:
                    continue
                if _has_scope_contradiction(cleaned_question, cand.doc_id, full_text):
                    continue

                score, chunk_overlap = _score_candidate(
                    cand,
                    query_stems,
                    entity_stems,
                    doc_stems_cache[cand.doc_row_id],
                    idf_weights,
                )
                ranked.append((score, chunk_overlap, cand))

            if not ranked:
                return _abstain()

            ranked.sort(key=lambda item: item[0], reverse=True)
            best_score, best_overlap, best_candidate = ranked[0]

            min_required_overlap = 1 if len(query_stems) == 1 else 2
            if best_overlap < min_required_overlap or best_score < 3.5:
                return _abstain()

            parent = _load_authorized_parent(conn, best_candidate.parent_id, pub_id, roles)
            if parent is None:
                return _abstain()

            answer_text = _extract_concise_answer(best_candidate, parent)
            if not answer_text:
                return _abstain()

            return AnswerResult(
                answer=answer_text,
                abstained=False,
                citations=[
                    Citation(
                        doc_id=parent.doc_id,
                        version=parent.version,
                        locator=parent.locator,
                    )
                ],
            )
        finally:
            conn.execute("ROLLBACK;")
    finally:
        conn.close()
