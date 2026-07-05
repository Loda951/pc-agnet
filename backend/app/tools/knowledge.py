import math
import re
from collections import Counter
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeDocument
from app.repositories.knowledge import KnowledgeRepository
from app.tools.schemas import DocumentSearchHit, DocumentSearchInput, DocumentSearchOutput

POLICY_DOCUMENT_TYPES = {"policy", "store_rule", "faq"}
KNOWLEDGE_DOCUMENT_TYPES = {"brand", "peripheral_knowledge", "faq", "store_rule"}


class KnowledgeKeywordToolService:
    def __init__(self, session: AsyncSession):
        self.repository = KnowledgeRepository(session)

    async def search_policy(self, request: DocumentSearchInput) -> DocumentSearchOutput:
        return await self._search(request, POLICY_DOCUMENT_TYPES)

    async def search_knowledge(self, request: DocumentSearchInput) -> DocumentSearchOutput:
        return await self._search(request, KNOWLEDGE_DOCUMENT_TYPES)

    async def _search(
        self, request: DocumentSearchInput, default_document_types: set[str]
    ) -> DocumentSearchOutput:
        documents = await self.repository.list_documents()
        allowed_types = (
            {request.document_type} & default_document_types
            if request.document_type
            else default_document_types
        )
        candidates = [
            document for document in documents if document.document_type in allowed_types
        ]
        hits = _rank_documents(request.query, candidates)[: request.limit]
        return DocumentSearchOutput(
            result_type="documents" if hits else "empty",
            documents=hits,
        )


def _rank_documents(
    query: str, documents: list[KnowledgeDocument]
) -> list[DocumentSearchHit]:
    if not documents:
        return []

    tokenized_docs = [_tokenize(_document_text(document)) for document in documents]
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    doc_freq = Counter(
        token for tokens in tokenized_docs for token in set(tokens)
    )
    avg_doc_len = sum(len(tokens) for tokens in tokenized_docs) / max(len(tokenized_docs), 1)
    ranked: list[tuple[float, KnowledgeDocument]] = []
    for document, tokens in zip(documents, tokenized_docs, strict=True):
        score = _bm25_score(query_tokens, tokens, doc_freq, len(documents), avg_doc_len)
        if score > 0:
            ranked.append((round(score, 4), document))

    ranked.sort(key=lambda item: (-item[0], item[1].id))
    return [
        DocumentSearchHit(
            source_id=document.id,
            title=document.title,
            document_type=document.document_type,
            snippet=_snippet(document.content),
            score=score,
            metadata=document.metadata_json or {},
        )
        for score, document in ranked
    ]


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


def _document_text(document: KnowledgeDocument) -> str:
    metadata = document.metadata_json or {}
    metadata_text = " ".join(str(value) for value in metadata.values())
    return " ".join([document.title, document.document_type, metadata_text, document.content])


def _tokenize(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.lower())
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
