import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

from app.tools.schemas import DocumentSearchHit, DocumentSearchInput, DocumentSearchOutput

POLICY_DOCUMENT_TYPES = {"policy", "store_rule", "faq"}
KNOWLEDGE_DOCUMENT_TYPES = {"brand", "peripheral_knowledge", "faq", "store_rule"}
DEFAULT_KNOWLEDGE_DOCUMENT_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "knowledge_documents.json"
)
RRF_K = 10


class LocalKnowledgeDocument(BaseModel):
    id: int
    title: str
    document_type: str
    content: str
    metadata: dict = Field(default_factory=dict)

    @property
    def metadata_json(self) -> dict:
        return self.metadata


@dataclass(frozen=True)
class RankedDocument:
    document: LocalKnowledgeDocument
    score: float
    bm25_score: float = 0.0
    keyword_score: float = 0.0
    bm25_rank: int | None = None
    keyword_rank: int | None = None


class KnowledgeKeywordToolService:
    def __init__(
        self,
        documents: list[LocalKnowledgeDocument] | None = None,
        document_path: Path = DEFAULT_KNOWLEDGE_DOCUMENT_PATH,
    ):
        self._documents = documents
        self.document_path = document_path

    async def search_policy(self, request: DocumentSearchInput) -> DocumentSearchOutput:
        return await self._search(request, POLICY_DOCUMENT_TYPES)

    async def search_knowledge(self, request: DocumentSearchInput) -> DocumentSearchOutput:
        return await self._search(request, KNOWLEDGE_DOCUMENT_TYPES)

    async def _search(
        self, request: DocumentSearchInput, default_document_types: set[str]
    ) -> DocumentSearchOutput:
        documents = self._documents or _load_local_documents(self.document_path)
        allowed_types = (
            {request.document_type} & default_document_types
            if request.document_type
            else default_document_types
        )
        candidates = [
            document for document in documents if document.document_type in allowed_types
        ]
        hits = _rank_documents(
            request.query,
            candidates,
            request.retrieval_mode,
        )[: request.limit]
        return DocumentSearchOutput(
            result_type="documents" if hits else "empty",
            documents=hits,
            search_strategy=request.retrieval_mode,
        )


def _rank_documents(
    query: str,
    documents: list[LocalKnowledgeDocument],
    retrieval_mode: str,
) -> list[DocumentSearchHit]:
    if not documents:
        return []

    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    bm25_ranked = _rank_by_bm25(query_tokens, documents)
    keyword_ranked = _rank_by_keyword(query, query_tokens, documents)
    if retrieval_mode == "bm25":
        ranked = bm25_ranked
    elif retrieval_mode == "keyword":
        ranked = keyword_ranked
    else:
        ranked = _rank_by_rrf(bm25_ranked, keyword_ranked)

    return [
        DocumentSearchHit(
            source_id=item.document.id,
            title=item.document.title,
            document_type=item.document.document_type,
            snippet=_snippet(item.document.content),
            score=item.score,
            metadata=_hit_metadata(item),
        )
        for item in ranked
    ]


def _rank_by_bm25(
    query_tokens: list[str],
    documents: list[LocalKnowledgeDocument],
) -> list[RankedDocument]:
    tokenized_docs = [_tokenize(_document_text(document)) for document in documents]
    doc_freq = Counter(token for tokens in tokenized_docs for token in set(tokens))
    avg_doc_len = sum(len(tokens) for tokens in tokenized_docs) / max(len(tokenized_docs), 1)
    ranked: list[RankedDocument] = []
    for document, tokens in zip(documents, tokenized_docs, strict=True):
        score = _bm25_score(query_tokens, tokens, doc_freq, len(documents), avg_doc_len)
        if score > 0:
            ranked.append(
                RankedDocument(
                    document=document,
                    score=round(score, 4),
                    bm25_score=round(score, 4),
                )
            )
    ranked.sort(key=lambda item: (-item.score, item.document.id))
    return [
        RankedDocument(
            document=item.document,
            score=item.score,
            bm25_score=item.bm25_score,
            bm25_rank=rank,
        )
        for rank, item in enumerate(ranked, start=1)
    ]


def _rank_by_keyword(
    query: str,
    query_tokens: list[str],
    documents: list[LocalKnowledgeDocument],
) -> list[RankedDocument]:
    ranked: list[RankedDocument] = []
    normalized_query = _normalize_text(query)
    for document in documents:
        score = _keyword_score(normalized_query, query_tokens, document)
        if score > 0:
            ranked.append(
                RankedDocument(
                    document=document,
                    score=round(score, 4),
                    keyword_score=round(score, 4),
                )
            )
    ranked.sort(key=lambda item: (-item.score, item.document.id))
    return [
        RankedDocument(
            document=item.document,
            score=item.score,
            keyword_score=item.keyword_score,
            keyword_rank=rank,
        )
        for rank, item in enumerate(ranked, start=1)
    ]


def _rank_by_rrf(
    bm25_ranked: list[RankedDocument],
    keyword_ranked: list[RankedDocument],
) -> list[RankedDocument]:
    by_document_id: dict[int, RankedDocument] = {}
    for item in bm25_ranked:
        by_document_id[item.document.id] = item
    for item in keyword_ranked:
        existing = by_document_id.get(item.document.id)
        if existing is None:
            by_document_id[item.document.id] = item
            continue
        by_document_id[item.document.id] = RankedDocument(
            document=existing.document,
            score=existing.score,
            bm25_score=existing.bm25_score,
            keyword_score=item.keyword_score,
            bm25_rank=existing.bm25_rank,
            keyword_rank=item.keyword_rank,
        )

    fused: list[RankedDocument] = []
    for item in by_document_id.values():
        score = 0.0
        if item.bm25_rank is not None:
            score += 1 / (RRF_K + item.bm25_rank)
        if item.keyword_rank is not None:
            score += 1 / (RRF_K + item.keyword_rank)
        fused.append(
            RankedDocument(
                document=item.document,
                score=round(score, 6),
                bm25_score=item.bm25_score,
                keyword_score=item.keyword_score,
                bm25_rank=item.bm25_rank,
                keyword_rank=item.keyword_rank,
            )
        )
    fused.sort(key=lambda item: (-item.score, item.document.id))
    return fused


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    doc_freq: Counter[str],
    document_count: int,
    avg_doc_len: float,
) -> float:
    frequencies = Counter(doc_tokens)
    doc_len = len(doc_tokens) or 1
    k1 = 1.5
    b = 0.75
    score = 0.0
    for token in query_tokens:
        frequency = frequencies[token]
        if frequency == 0:
            continue
        idf = math.log(1 + (document_count - doc_freq[token] + 0.5) / (doc_freq[token] + 0.5))
        denominator = frequency + k1 * (1 - b + b * doc_len / max(avg_doc_len, 1))
        score += idf * (frequency * (k1 + 1) / denominator)
    return score


def _keyword_score(
    normalized_query: str,
    query_tokens: list[str],
    document: LocalKnowledgeDocument,
) -> float:
    title = _normalize_text(document.title)
    document_type = _normalize_text(document.document_type)
    metadata = _normalize_text(" ".join(str(value) for value in document.metadata_json.values()))
    content = _normalize_text(document.content)
    score = 0.0

    if normalized_query and normalized_query in title:
        score += 8
    if normalized_query and normalized_query in metadata:
        score += 5
    if normalized_query and normalized_query in content:
        score += 4

    for token in set(query_tokens):
        if token in title:
            score += 4
        if token in document_type:
            score += 3
        if token in metadata:
            score += 3
        if token in content:
            score += 1
    return score


def _hit_metadata(item: RankedDocument) -> dict:
    metadata = dict(item.document.metadata_json or {})
    metadata["retrieval_debug"] = {
        "bm25_score": item.bm25_score,
        "keyword_score": item.keyword_score,
        "rrf_score": item.score,
        "bm25_rank": item.bm25_rank,
        "keyword_rank": item.keyword_rank,
    }
    return metadata


@lru_cache(maxsize=8)
def _load_local_documents(document_path: Path) -> list[LocalKnowledgeDocument]:
    return [
        LocalKnowledgeDocument.model_validate(item)
        for item in json.loads(document_path.read_text(encoding="utf-8"))
    ]


def _document_text(document: LocalKnowledgeDocument) -> str:
    metadata = document.metadata_json or {}
    metadata_text = " ".join(str(value) for value in metadata.values())
    return " ".join([document.title, document.document_type, metadata_text, document.content])


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _tokenize(text: str) -> list[str]:
    normalized = _normalize_text(text)
    tokens = re.findall(r"[a-z0-9][a-z0-9+.-]*", normalized)
    cjk_chars = [char for char in normalized if "\u4e00" <= char <= "\u9fff"]
    tokens.extend(cjk_chars)
    tokens.extend(_cjk_ngrams(cjk_chars, widths=(2, 3, 4)))
    return [token for token in tokens if token.strip()]


def _cjk_ngrams(chars: list[str], widths: Iterable[int]) -> list[str]:
    ngrams: list[str] = []
    for width in widths:
        ngrams.extend(
            "".join(chars[index : index + width])
            for index in range(max(len(chars) - width + 1, 0))
        )
    return ngrams


def _snippet(content: str, max_length: int = 180) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 1]}..."
