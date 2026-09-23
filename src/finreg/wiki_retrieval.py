"""Offline lexical/Wiki candidate experiment; outputs are source text, never legal answers."""

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finreg.dataset import CHECKS, Check, CrossCheck

DocumentType = Literal[
    "law", "enforcement_decree", "supervisory_regulation", "official_faq", "official_interpretation"
]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceOffsets(Record):
    format: Literal["html"]
    unit: Literal["unicode_codepoint"]
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("Invalid source offsets")
        return self


class SourceEvidence(Record):
    provision_id: UUID
    version_id: UUID
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str
    article: str | None
    document_type: DocumentType
    source_url: str
    text: str = Field(min_length=1)
    source_offsets: SourceOffsets
    valid_from: date | None
    valid_to_exclusive: date | None
    temporal_verified: bool
    parent_id: UUID | None = None
    related_ids: tuple[UUID, ...] = ()
    unresolved_references: bool = False
    transition_requires_facts: bool = False


class Corpus(Record):
    snapshot_id: UUID
    validation_status: Literal["verified"]
    valid_from: date
    valid_to_exclusive: date
    evidence: tuple[SourceEvidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_coverage(self):
        if self.valid_from >= self.valid_to_exclusive:
            raise ValueError("Empty snapshot coverage")
        if len({item.provision_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("Duplicate evidence ID")
        return self


def fingerprint(record: BaseModel) -> str:
    raw = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EvidenceRef(Record):
    provision_id: UUID
    version_id: UUID
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PageReview(Record):
    page_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1)
    reviewer_role: Literal["developer", "external"]
    method: str = Field(min_length=1)
    reviewed_at: datetime
    human_attested: Literal[True]
    existence: Check
    meaning: Check
    temporal: Check
    completeness: Check
    official_cross_checks: tuple[CrossCheck, ...] = ()

    @model_validator(mode="after")
    def timezone_required(self):
        if self.reviewed_at.tzinfo is None:
            raise ValueError("Review time needs a timezone")
        return self


class CandidatePage(Record):
    page_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    title: str = Field(min_length=1)
    summary: str
    refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    review: PageReview | None = None

    @model_validator(mode="after")
    def unique_refs(self):
        if len({ref.provision_id for ref in self.refs}) != len(self.refs):
            raise ValueError("Duplicate page reference")
        return self


def page_hash(page: CandidatePage) -> str:
    raw = json.dumps(
        page.model_dump(mode="json", exclude={"review"}), ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Generator(Record):
    model_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    settings: str = Field(min_length=1)


class CandidateBuild(Record):
    schema_version: Literal[1] = 1
    kind: Literal["legal-wiki-candidates"] = "legal-wiki-candidates"
    snapshot_id: UUID
    corpus_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    generator: Generator
    pages: tuple[CandidatePage, ...]

    @model_validator(mode="after")
    def unique_pages(self):
        if len({page.page_id for page in self.pages}) != len(self.pages):
            raise ValueError("Duplicate Wiki page ID")
        return self


def draft_build(corpus: Corpus) -> CandidateBuild:
    """Export source-only review templates. No LLM call or human attestation is invented."""
    return CandidateBuild(
        snapshot_id=corpus.snapshot_id,
        corpus_sha256=fingerprint(corpus),
        generator=Generator(
            model_id="none",
            revision="template-v1",
            prompt_version="none",
            settings="Source headings only; summaries and human review are pending.",
        ),
        pages=tuple(
            CandidatePage(
                page_id=f"provision-{item.provision_id}",
                title=f"{item.title} {item.article or ''}".strip(),
                summary="",
                refs=(
                    EvidenceRef(
                        provision_id=item.provision_id,
                        version_id=item.version_id,
                        source_sha256=item.source_sha256,
                        evidence_sha256=fingerprint(item),
                    ),
                ),
            )
            for item in corpus.evidence
        ),
    )


class SearchConfig(Record):
    tokenizer: Literal["nfkc-word-hangul-bigram-v1"] = "nfkc-word-hangul-bigram-v1"
    k1: float = Field(default=1.2, gt=0, allow_inf_nan=False)
    b: float = Field(default=0.75, ge=0, le=1, allow_inf_nan=False)
    rrf_k: int = Field(default=60, ge=1)
    top_k: int = Field(default=5, ge=1, le=100)
    wiki_top_k: int = Field(default=3, ge=1, le=100)
    max_context_chars: int = Field(default=12000, ge=1)


def tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    result = re.findall(r"[a-z0-9]+|[가-힣]+", text)
    for word in re.findall(r"[가-힣]{2,}", text):
        result.extend("~" + word[i : i + 2] for i in range(len(word) - 1))
    return result


def bm25(query: str, documents: dict[str, str], config: SearchConfig) -> list[str]:
    """Positive-IDF BM25, deterministic ties, with no learned semantic representation."""
    counts = {key: Counter(tokens(text)) for key, text in documents.items()}
    lengths = {key: sum(value.values()) for key, value in counts.items()}
    average = sum(lengths.values()) / len(counts) if counts else 0
    if not average:
        return []
    frequencies = Counter(term for count in counts.values() for term in count)
    scores = {}
    for key, count in counts.items():
        score = 0.0
        for term in sorted(set(tokens(query))):
            frequency = count[term]
            if not frequency:
                continue
            idf = math.log1p((len(counts) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            norm = config.k1 * (1 - config.b + config.b * lengths[key] / average)
            score += idf * frequency * (config.k1 + 1) / (frequency + norm)
        if score > 0:
            scores[key] = score
    return sorted(scores, key=lambda key: (-scores[key], key))


class SearchResult(Record):
    snapshot_id: UUID
    corpus_sha256: str
    config_sha256: str
    ranked_ids: tuple[UUID, ...]
    evidence: tuple[SourceEvidence, ...]
    omitted_ids: tuple[UUID, ...]
    unresolved_ids: tuple[UUID, ...]
    wiki_status: str
    wiki_build_id: str | None
    selected_page_ids: tuple[str, ...]
    rejected_pages: tuple[tuple[str, str], ...]


def _eligible(item: SourceEvidence, corpus: Corpus, as_of: date) -> bool:
    return (
        corpus.valid_from <= as_of < corpus.valid_to_exclusive
        and item.temporal_verified
        and item.valid_from is not None
        and item.valid_from <= as_of
        and (item.valid_to_exclusive is None or as_of < item.valid_to_exclusive)
        and not item.transition_requires_facts
    )


def _page_problem(page: CandidatePage, current: dict[UUID, SourceEvidence]) -> str | None:
    review = page.review
    if not review or not page.summary.strip():
        return "unreviewed"
    if review.page_sha256 != page_hash(page):
        return "review_stale"
    if any(getattr(review, name).result != "pass" for name in CHECKS):
        return "review_incomplete"
    if review.reviewer_role != "external" and not review.official_cross_checks:
        return "cross_check_missing"
    for ref in page.refs:
        item = current.get(ref.provision_id)
        if item is None:
            return "reference_unavailable_for_request"
        if (
            item.version_id != ref.version_id
            or item.source_sha256 != ref.source_sha256
            or fingerprint(item) != ref.evidence_sha256
        ):
            return "reference_stale"
    context = [ref.provision_id for ref in page.refs]
    for key in context:
        item = current.get(key)
        if item is None:
            return "unavailable_source_context"
        if item.unresolved_references:
            return "unresolved_source_context"
        for related in ((item.parent_id,) if item.parent_id else ()) + item.related_ids:
            if related not in context:
                context.append(related)
    return None


def search(
    corpus: Corpus,
    query: str,
    as_of: date,
    *,
    config: SearchConfig | None = None,
    document_types: tuple[DocumentType, ...] = (),
    title: str | None = None,
    article: str | None = None,
    wiki: CandidateBuild | None = None,
    wiki_unavailable: bool = False,
) -> SearchResult:
    """Retrieve candidates only. Unknown transitions need facts; no answer status is inferred."""
    config = config or SearchConfig()
    if not query.strip():
        raise ValueError("Empty query")
    if not corpus.valid_from <= as_of < corpus.valid_to_exclusive:
        raise ValueError("Date outside verified snapshot coverage")
    # Article filters are exact: 제1조 must not match 제10조.
    match = re.search(r"제\s*\d+\s*조(?:\s*의\s*\d+)?", query)
    article = article or (match[0] if match else None)

    def compact(value):
        return re.sub(r"\s+", "", value or "")

    current = {
        item.provision_id: item
        for item in corpus.evidence
        if _eligible(item, corpus, as_of)
        and (not document_types or item.document_type in document_types)
    }
    candidates = {
        key: item
        for key, item in current.items()
        if (title is None or item.title == title)
        and (article is None or compact(item.article) == compact(article))
    }
    raw_rank = bm25(
        query,
        {
            str(key): f"{item.title} {item.article or ''} {item.text}"
            for key, item in candidates.items()
        },
        config,
    )
    # Explicit locators work even when the query's remaining words have no lexical overlap.
    if title is not None or article is not None:
        raw_rank += sorted(str(key) for key in candidates if str(key) not in raw_rank)
    ranked = list(raw_rank)
    status = "unavailable" if wiki_unavailable else "disabled"
    selected: list[str] = []
    rejected: list[tuple[str, str]] = []
    build_id = fingerprint(wiki) if wiki else None
    if wiki:
        if wiki.snapshot_id != corpus.snapshot_id or wiki.corpus_sha256 != fingerprint(corpus):
            status = "stale_build"
        else:
            usable = {}
            for page in wiki.pages:
                problem = _page_problem(page, candidates)
                if problem:
                    rejected.append((page.page_id, problem))
                else:
                    usable[page.page_id] = page
            selected = bm25(
                query, {key: f"{page.title} {page.summary}" for key, page in usable.items()}, config
            )[: config.wiki_top_k]
            wiki_rank = list(
                dict.fromkeys(str(ref.provision_id) for key in selected for ref in usable[key].refs)
            )
            status = "used" if wiki_rank else "no_usable_match"
            if wiki_rank:
                scores: Counter = Counter()
                for ranking in (raw_rank, wiki_rank):
                    for position, key in enumerate(ranking, 1):
                        scores[key] += 1 / (config.rrf_k + position)
                ranked = sorted(scores, key=lambda key: (-scores[key], key))
    ranked_ids = tuple(UUID(key) for key in ranked[: config.top_k])
    # Add only same-request, eligible source context. Never inject Wiki prose.
    context_ids = list(ranked_ids)
    unresolved = set()
    for key in context_ids:  # Bounded by the finite corpus; membership prevents cycles.
        item = current[key]
        if item.unresolved_references:
            unresolved.add(key)
        for related in ((item.parent_id,) if item.parent_id else ()) + item.related_ids:
            if related not in current:
                unresolved.add(key)
            elif related not in context_ids:
                context_ids.append(related)
    included, omitted = [], []
    used_chars = 0
    for key in context_ids:
        item = current[key]
        if used_chars + len(item.text) > config.max_context_chars:
            omitted.append(key)  # Keep conditions intact instead of truncating provisions.
        else:
            included.append(item)
            used_chars += len(item.text)
    return SearchResult(
        snapshot_id=corpus.snapshot_id,
        corpus_sha256=fingerprint(corpus),
        config_sha256=fingerprint(config),
        ranked_ids=ranked_ids,
        evidence=tuple(included),
        omitted_ids=tuple(omitted),
        unresolved_ids=tuple(sorted(unresolved, key=str)),
        wiki_status=status,
        wiki_build_id=build_id,
        selected_page_ids=tuple(selected),
        rejected_pages=tuple(rejected),
    )
