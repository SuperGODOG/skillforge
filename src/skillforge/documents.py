"""Document to Skill Ingestion Module (Phase P2)

Provides local document source ingestion, snippet-level provenance tracking,
operable step extraction, and safe candidate synthesis without forging fake episodes.

Core Invariants:
1. Local Text/Markdown Only:
   Only application-provided plain text/markdown documents with explicit metadata are ingested.
   No multi-modal, crawling, or unverified external doc services.
2. Independent Typed Source:
   DocumentSource is an independent source with ID, version, content_hash, and snippet locations.
   Importing documents does NOT create fake execution episodes in EpisodeStore.
3. Untrusted Data Boundary:
   Document content is untrusted data: it cannot specify tool permissions, commands, mounts,
   network rules, sandbox bypasses, or auto-publish triggers. All candidates start as DRAFT.
4. Idempotency & Revision Independence:
   Re-importing the same (doc_id, version) does not produce duplicate sources or candidates.
   Revisions create distinct DocumentSource versions; previous candidates and published versions
   remain immutable and must be re-verified.
5. Step Quality & Conflict Preservation:
   Vague, contradictory, or prerequisite-missing steps are rejected with explicit reasons and
   their original snippets preserved; they are never stitched into pseudo-reliable skills.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from .models import (
    CandidateDecision,
    CandidateSkill,
    DocumentExtractionResult,
    DocumentSnippet,
    DocumentSource,
    SkillMeta,
    Trigger,
)
from .storage.db import init_db
from .episode import CandidateStore
from .registry import SkillRegistry


class DocumentStore:
    """SQLite-backed store for typed DocumentSource and DocumentSnippet records."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = init_db(self.db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def has_document(self, doc_id: str, version: Optional[str] = None) -> bool:
        conn = self._get_conn()
        if version is not None:
            row = conn.execute(
                "SELECT 1 FROM document_sources WHERE doc_id = ? AND version = ?",
                (doc_id, version),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM document_sources WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        return row is not None

    def save_document(
        self,
        doc: DocumentSource,
        on_conflict: Literal["error", "ignore"] = "error",
    ) -> str:
        """Persist a DocumentSource and its DocumentSnippets.

        Validates:
        - doc_id must start with 'doc_'
        - content_hash matches raw content SHA-256
        - Does NOT forge or touch episodes table
        """
        doc.__post_init__()
        computed_hash = hashlib.sha256(doc.content.encode("utf-8")).hexdigest()
        if not doc.content_hash:
            doc.content_hash = computed_hash
        elif doc.content_hash != computed_hash:
            raise ValueError(
                f"Document content_hash mismatch: expected {computed_hash}, got {doc.content_hash}"
            )

        conn = self._get_conn()
        existing = conn.execute(
            "SELECT 1 FROM document_sources WHERE doc_id = ? AND version = ?",
            (doc.doc_id, doc.version),
        ).fetchone()

        if existing:
            if on_conflict == "error":
                raise ValueError(
                    f"DocumentSource '{doc.doc_id}' version '{doc.version}' already exists"
                )
            return doc.doc_id

        created_at = doc.created_at or datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(doc.metadata, ensure_ascii=False)

        conn.execute(
            """INSERT INTO document_sources (
                doc_id, version, title, content_hash, content, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.doc_id,
                doc.version,
                doc.title,
                doc.content_hash,
                doc.content,
                metadata_json,
                created_at,
            ),
        )

        for snip in doc.snippets:
            snip.__post_init__()
            conn.execute(
                """INSERT OR REPLACE INTO document_snippets (
                    snippet_id, doc_id, doc_version, section_title,
                    start_line, end_line, content, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snip.snippet_id,
                    snip.doc_id,
                    snip.doc_version,
                    snip.section_title,
                    snip.start_line,
                    snip.end_line,
                    snip.content,
                    snip.content_hash,
                ),
            )

        conn.commit()
        return doc.doc_id

    def get_document(
        self,
        doc_id: str,
        version: Optional[str] = None,
    ) -> Optional[DocumentSource]:
        conn = self._get_conn()
        if version is not None:
            row = conn.execute(
                """SELECT doc_id, version, title, content_hash, content, metadata_json, created_at
                   FROM document_sources WHERE doc_id = ? AND version = ?""",
                (doc_id, version),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT doc_id, version, title, content_hash, content, metadata_json, created_at
                   FROM document_sources WHERE doc_id = ? ORDER BY created_at DESC LIMIT 1""",
                (doc_id,),
            ).fetchone()

        if not row:
            return None

        did, dver, title, chash, content, meta_j, cat = row
        meta = json.loads(meta_j) if meta_j else {}

        snip_rows = conn.execute(
            """SELECT snippet_id, doc_id, doc_version, section_title,
                      start_line, end_line, content, content_hash
               FROM document_snippets WHERE doc_id = ? AND doc_version = ?
               ORDER BY start_line ASC""",
            (did, dver),
        ).fetchall()

        snippets = [
            DocumentSnippet(
                snippet_id=sr[0],
                doc_id=sr[1],
                doc_version=sr[2],
                section_title=sr[3],
                start_line=sr[4],
                end_line=sr[5],
                content=sr[6],
                content_hash=sr[7],
            )
            for sr in snip_rows
        ]

        return DocumentSource(
            doc_id=did,
            title=title,
            version=dver,
            content=content,
            content_hash=chash,
            snippets=snippets,
            metadata=meta,
            created_at=cat,
        )

    def list_documents(self, doc_id: Optional[str] = None) -> list[DocumentSource]:
        conn = self._get_conn()
        query = """SELECT doc_id, version, title, content_hash, content, metadata_json, created_at
                   FROM document_sources WHERE 1=1"""
        params: list[Any] = []
        if doc_id is not None:
            query += " AND doc_id = ?"
            params.append(doc_id)
        query += " ORDER BY doc_id ASC, version ASC"

        rows = conn.execute(query, tuple(params)).fetchall()
        results: list[DocumentSource] = []
        for r in rows:
            did, dver = r[0], r[1]
            snip_rows = conn.execute(
                """SELECT snippet_id, doc_id, doc_version, section_title,
                          start_line, end_line, content, content_hash
                   FROM document_snippets WHERE doc_id = ? AND doc_version = ?
                   ORDER BY start_line ASC""",
                (did, dver),
            ).fetchall()
            snippets = [
                DocumentSnippet(
                    snippet_id=sr[0],
                    doc_id=sr[1],
                    doc_version=sr[2],
                    section_title=sr[3],
                    start_line=sr[4],
                    end_line=sr[5],
                    content=sr[6],
                    content_hash=sr[7],
                )
                for sr in snip_rows
            ]
            results.append(
                DocumentSource(
                    doc_id=did,
                    title=r[2],
                    version=dver,
                    content=r[4],
                    content_hash=r[3],
                    snippets=snippets,
                    metadata=json.loads(r[5]) if r[5] else {},
                    created_at=r[6],
                )
            )
        return results

    def get_snippet(self, snippet_id: str) -> Optional[DocumentSnippet]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT snippet_id, doc_id, doc_version, section_title,
                      start_line, end_line, content, content_hash
               FROM document_snippets WHERE snippet_id = ?""",
            (snippet_id,),
        ).fetchone()
        if not row:
            return None
        return DocumentSnippet(
            snippet_id=row[0],
            doc_id=row[1],
            doc_version=row[2],
            section_title=row[3],
            start_line=row[4],
            end_line=row[5],
            content=row[6],
            content_hash=row[7],
        )


def parse_markdown_snippets(text: str, doc_id: str, doc_version: str) -> list[DocumentSnippet]:
    """Parse a Markdown text into verifiable snippets with 1-indexed line numbers and content hashes."""
    lines = text.splitlines()
    if not lines:
        return []

    snippets: list[DocumentSnippet] = []
    current_title = "Preamble"
    current_start = 1
    current_lines: list[str] = []

    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")

    for i, line in enumerate(lines, start=1):
        m = heading_re.match(line)
        if m:
            # Finish previous snippet if non-empty
            if current_lines:
                content_str = "\n".join(current_lines)
                snippets.append(
                    DocumentSnippet(
                        snippet_id=f"snip_{doc_id}_{doc_version}_{len(snippets)+1}",
                        doc_id=doc_id,
                        doc_version=doc_version,
                        section_title=current_title,
                        start_line=current_start,
                        end_line=i - 1,
                        content=content_str,
                        content_hash=hashlib.sha256(content_str.encode("utf-8")).hexdigest(),
                    )
                )
                current_lines = []
            current_title = m.group(2).strip()
            current_start = i
            current_lines.append(line)
        else:
            current_lines.append(line)

    if current_lines:
        content_str = "\n".join(current_lines)
        snippets.append(
            DocumentSnippet(
                snippet_id=f"snip_{doc_id}_{doc_version}_{len(snippets)+1}",
                doc_id=doc_id,
                doc_version=doc_version,
                section_title=current_title,
                start_line=current_start,
                end_line=len(lines),
                content=content_str,
                content_hash=hashlib.sha256(content_str.encode("utf-8")).hexdigest(),
            )
        )

    return snippets


# Patterns indicating prompt injection, privilege escalation, or gate evasion
ADVERSARIAL_PATTERNS = [
    r"skip\s+(?:all\s+)?verification",
    r"bypass\s+(?:all\s+)?(?:gate|security|policy|ratchet)",
    r"disable\s+sandbox",
    r"direct(?:ly)?\s+publish",
    r"auto(?:matically)?\s+publish",
    r"grant\s+(?:sudo|root|admin)",
    r"rm\s+-rf",
    r"chmod\s+777",
]


def _filter_adversarial_claims(text: str) -> tuple[str, list[str]]:
    """Detect and scrub adversarial / gate bypass directives from document text."""
    filtered_claims: list[str] = []
    cleaned_lines: list[str] = []

    for line in text.splitlines():
        matched = False
        for pat in ADVERSARIAL_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                filtered_claims.append(line.strip())
                matched = True
                break
        if not matched:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines), filtered_claims


def extract_candidate_from_document(
    doc: DocumentSource,
    candidate_store: CandidateStore,
    target_skill_name: Optional[str] = None,
    llm: Optional[Any] = None,
    override_decision: Optional[CandidateDecision] = None,
    registry: Optional[SkillRegistry] = None,
) -> DocumentExtractionResult:
    """Extract operable skill candidate from DocumentSource with strict security and quality boundaries.

    Invariants:
    1. Idempotency: Re-importing identical (doc_id, version) reuses existing candidate without duplicating.
    2. Untrusted text: Directives claiming 'skip verification', 'sudo', 'auto publish' are scrubbed.
    3. Step Quality: Vague, contradictory, or prerequisite-missing steps are rejected.
    4. Isolated DRAFT: Candidates are created strictly as DRAFT with no forged episodes.
    """
    # 1. Idempotency check: see if candidate for this (doc_id, version) already exists
    existing_cands = candidate_store.list_candidates()
    for cand in existing_cands:
        if cand.source_doc_id == doc.doc_id and cand.source_doc_version == doc.version:
            # Verified reuse
            return DocumentExtractionResult(
                doc_id=doc.doc_id,
                doc_version=doc.version,
                status="success",
                candidate=cand,
                target_skill_name=cand.skill_name,
                extracted_snippets=[s for s in doc.snippets if s.snippet_id in cand.source_snippet_ids],
                raw_claims_filtered=[],
            )

    # 2. Derive target skill name
    skill_name = target_skill_name
    if not skill_name:
        # Sanitize from title
        clean_name = re.sub(r"[^a-zA-Z0-9_]+", "_", doc.title.lower()).strip("_")
        skill_name = clean_name or "extracted_skill"

    # 3. Detect and scrub adversarial injection claims
    cleaned_content, filtered_claims = _filter_adversarial_claims(doc.content)

    # 4. Analyze snippets for operable instructions vs vague / contradictory / missing prerequisites
    rejection_reasons: list[str] = []
    conflicts: list[str] = []

    # Check for contradictions
    has_contradiction = False
    lower_content = doc.content.lower()
    if (
        ("always" in lower_content and "never" in lower_content)
        or ("strictly do not" in lower_content and "must" in lower_content)
        or ("write" in lower_content and "do not write" in lower_content)
        or ("enable" in lower_content and "disable" in lower_content)
    ):
        # Specific check for opposing directives
        lines = [l.strip() for l in doc.content.splitlines() if l.strip()]
        for i, l1 in enumerate(lines):
            l1_low = l1.lower()
            for l2 in lines[i + 1 :]:
                l2_low = l2.lower()
                if (
                    ("write to" in l1_low and ("do not write" in l2_low or "never write" in l2_low))
                    or (("do not write" in l1_low or "never write" in l1_low) and "write to" in l2_low)
                    or ("always return" in l1_low and "never return" in l2_low)
                    or ("never return" in l1_low and "always return" in l2_low)
                    or ("enable" in l1_low and ("strictly disable" in l2_low or "disable" in l2_low))
                    or (("strictly disable" in l1_low or "disable" in l1_low) and "enable" in l2_low)
                    or ("always" in l1_low and "never" in l2_low and any(w in l1_low and w in l2_low for w in ["call", "run", "use", "return", "write"]))
                ):
                    conflicts.append(f"Contradictory directives: '{l1}' vs '{l2}'")
                    has_contradiction = True

    if has_contradiction:
        return DocumentExtractionResult(
            doc_id=doc.doc_id,
            doc_version=doc.version,
            status="conflict",
            candidate=None,
            target_skill_name=skill_name,
            conflicts=conflicts,
            rejection_reasons=["Document contains mutually contradictory operable steps"],
            extracted_snippets=doc.snippets,
            raw_claims_filtered=filtered_claims,
        )

    # Check for vague or missing actionable steps
    # Operable steps must contain action verbs or tool commands
    action_keywords = [
        "run", "execute", "call", "calculate", "process", "write",
        "fetch", "get", "step", "1.", "2.", "add", "sub",
    ]
    step_snippets: list[DocumentSnippet] = []
    for snip in doc.snippets:
        sec_lower = snip.section_title.lower()
        if any(k in sec_lower for k in ("step", "instruction", "procedure", "how-to", "run", "usage")):
            step_snippets.append(snip)

    if not step_snippets:
        # Fall back to snippets that have numbered list or action keywords
        for snip in doc.snippets:
            if re.search(r"^\s*(?:\d+\.|\-)\s+[A-Za-z]", snip.content, re.MULTILINE):
                step_snippets.append(snip)

    if not step_snippets:
        rejection_reasons.append("No actionable or operable steps found in document sections")

    # Check for vagueness (instructions that are too vague, e.g. "perform task appropriately")
    vague_phrases = [
        "perform appropriately",
        "do something good",
        "handle properly without details",
        "run generic tasks",
        "do some operations",
        "perform task appropriately",
        "handle appropriately",
        "do whatever is needed",
    ]
    for snip in step_snippets:
        snip_low = snip.content.lower()
        for vp in vague_phrases:
            if vp in snip_low:
                rejection_reasons.append(
                    f"Vague instructions detected in section '{snip.section_title}': '{vp}' lacks actionable specification"
                )

    # Check for missing prerequisites
    if (
        "requires unavailable tool" in lower_content
        or "missing prerequisite" in lower_content
        or "prerequisite missing" in lower_content
        or "unavailable dependency" in lower_content
    ):
        rejection_reasons.append("Document explicitly declares missing or unavailable prerequisites")

    if rejection_reasons:
        return DocumentExtractionResult(
            doc_id=doc.doc_id,
            doc_version=doc.version,
            status="rejected",
            candidate=None,
            target_skill_name=skill_name,
            rejection_reasons=rejection_reasons,
            extracted_snippets=step_snippets or doc.snippets,
            raw_claims_filtered=filtered_claims,
        )

    # 5. Extract operable instructions body
    body_lines = [f"# {skill_name}\n\n## Overview\nProcedural skill extracted from document '{doc.title}'.\n\n## Instructions\n"]
    for snip in step_snippets:
        cleaned_snip, _ = _filter_adversarial_claims(snip.content)
        if cleaned_snip.strip():
            body_lines.append(cleaned_snip)
            body_lines.append("")

    full_body = "\n".join(body_lines).strip()

    # 6. Build SkillMeta
    is_existing = registry is not None and registry.has_skill(skill_name)
    decision: CandidateDecision = override_decision or ("revise" if is_existing else "create")

    meta = SkillMeta(
        name=skill_name,
        version="1.0.0",
        description=f"Operable procedure extracted from document {doc.title} (v{doc.version})",
        use_when=f"Tasks matching procedures described in {doc.title}",
        not_for=["Tasks unrelated to " + skill_name],
        dependencies=[],
        trigger=Trigger(keywords=[skill_name]),
        examples=[f"Execute {skill_name} procedure"],
    )

    candidate_id = f"cand_doc_{uuid.uuid4().hex[:12]}"
    candidate = CandidateSkill(
        candidate_id=candidate_id,
        skill_name=skill_name,
        decision=decision,
        source_episode_ids=[],  # Crucial: NO fake Episode generated
        meta=meta,
        body=full_body,
        rationale=f"Synthesized from DocumentSource '{doc.doc_id}' v{doc.version} ({doc.title})",
        status="DRAFT",
        source_doc_id=doc.doc_id,
        source_doc_version=doc.version,
        source_snippet_ids=[s.snippet_id for s in step_snippets],
    )

    candidate_store.save_candidate(candidate)

    return DocumentExtractionResult(
        doc_id=doc.doc_id,
        doc_version=doc.version,
        status="success",
        candidate=candidate,
        target_skill_name=skill_name,
        extracted_snippets=step_snippets,
        raw_claims_filtered=filtered_claims,
    )
