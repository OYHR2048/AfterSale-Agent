import hashlib
import re

import httpx
import jieba

from aftersale.config import PACKAGE
from aftersale.db import DomainError
from aftersale.models import RetrievedChunk
from aftersale.retrieval.bm25 import BM25Retriever
from aftersale.retrieval.fusion import reciprocal_rank_fusion
from aftersale.retrieval.tokenizer import tokenize


class PolicyIndex:
    def __init__(self, runtime_dir, url="", directory=None):
        self.directory = directory or PACKAGE / "knowledge"
        self.url = url
        # jieba's default cache must not escape to system temp/public project.
        jieba.dt.tmp_dir = str(runtime_dir)
        jieba.setLogLevel(40)
        self.chunks = []
        for path in sorted(self.directory.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            version = re.search(r"政策版本：([^\n]+)", text)
            digest = hashlib.sha256(text.encode()).hexdigest()
            self.chunks.append(
                RetrievedChunk(
                    chunk_id=path.stem + ":" + digest[:12],
                    document_id=path.name,
                    title=text.splitlines()[0].lstrip("# "),
                    content=text,
                    policy_version=version.group(1).strip() if version else "2024-12-demo",
                    metadata={"path": path.name, "sha256": digest},
                )
            )
        self.bm25 = BM25Retriever(self.chunks)

    def fingerprint(self):
        # Read current files again: changes after proposal invalidate execution.
        return hashlib.sha256(
            b"".join(p.name.encode() + p.read_bytes() for p in sorted(self.directory.glob("*.md")))
        ).hexdigest()

    async def search(self, query):
        if self.url:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                r = await client.post(self.url, json={"query": query, "top_k": 6})
                r.raise_for_status()
                raw = r.json()
            raw = raw.get("chunks", []) if isinstance(raw, dict) else raw
            # Adapter accepts only locally trusted, version-matched policy text.
            selected = []
            for item in raw:
                for chunk in self.chunks:
                    if (
                        item.get("document_id") == chunk.document_id
                        and item.get("policy_version") == chunk.policy_version
                        and item.get("content") == chunk.content
                    ):
                        selected.append(chunk)
            if not selected:
                raise DomainError("远程 RAG 没有返回受信任的匹配政策；不据此执行")
            return [c.model_dump() for c in selected]
        bm = self.bm25.search(query, top_k=6)
        # Title overlap supplements BM25. This is lexical RAG, no dense model claim.
        terms = set(tokenize(query))
        title = sorted(self.chunks, key=lambda c: len(terms & set(tokenize(c.title))), reverse=True)
        return [c.model_dump() for c in reciprocal_rank_fusion([bm, title])[:6]]

    def valid(self, citations):
        canonical = {c.chunk_id: c for c in self.chunks}
        return bool(citations) and all(
            c.get("chunk_id") in canonical
            and c.get("content") == canonical[c["chunk_id"]].content
            and c.get("policy_version") == canonical[c["chunk_id"]].policy_version
            and hashlib.sha256(
                (self.directory / canonical[c["chunk_id"]].document_id)
                .read_text(encoding="utf-8")
                .encode()
            ).hexdigest()
            == canonical[c["chunk_id"]].metadata["sha256"]
            for c in citations
        )
