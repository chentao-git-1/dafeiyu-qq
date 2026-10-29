"""Related memories only; optional bounded vectors never bypass visibility queries."""

from __future__ import annotations

import logging
import asyncio
from collections import OrderedDict
import hashlib
import math
import time
from typing import Any

from .database import MemoryDatabase
from .memory_manager import MemoryManager
from .utils import extract_keywords

logger = logging.getLogger("memory_system.retriever")


class MemoryRetriever:
    """根据当前消息检索最相关的长期记忆。"""

    def __init__(
        self,
        db: MemoryDatabase,
        manager: MemoryManager,
        candidate_pool: int = 300,
        recency_half_life_days: float = 14.0,
        embedding=None,
    ) -> None:
        self.db = db
        self.manager = manager
        self._candidate_pool = max(50, candidate_pool)
        self._half_life = max(0.5, recency_half_life_days) * 86400.0
        self.embedding = embedding
        self._vectors = OrderedDict()

    # ------------------------------------------------------------------ scoring

    @staticmethod
    def _mem_keywords(memory: dict[str, Any]) -> set[str]:
        content = memory.get("content", "") or ""
        keys = set(extract_keywords(content))
        keys.update(extract_keywords(str(memory.get('owner_name',''))+' '+str(memory.get('user_id',''))))
        for tag in memory.get("tags") or []:
            keys.update(extract_keywords(str(tag)))
        return keys

    def score(self, memory: dict[str, Any], query_keys: set[str]) -> float:
        """对单条记忆打分。分数范围约 0~1.2。"""
        if not query_keys:
            return 0.0
        mem_keys = self._mem_keywords(memory)
        if not mem_keys:
            return 0.0
        overlap = len(query_keys & mem_keys) / len(query_keys)
        content = ' '.join(str(memory.get(key,'') or '') for key in ('content','owner_name','user_id'))
        # 子串直接命中给强加成
        substring_hit = any(len(k) >= 2 and k in content for k in query_keys)
        # Importance and freshness cannot make an unrelated memory relevant.
        meaningful_overlap = any(len(k) >= 2 for k in query_keys & mem_keys)
        if not (substring_hit or meaningful_overlap or len(query_keys) == 1 and bool(query_keys & mem_keys)):
            return 0.0
        importance = float(memory.get("importance", 0.5))
        # 时效衰减（重要度高的记忆衰减更慢）
        age = max(0.0, time.time() - float(memory.get("updated_at", memory.get("created_at", 0))))
        decay = 0.5 ** (age / (self._half_life * max(0.25, importance)))
        recency_bonus = 0.15 * decay
        access_bonus = min(float(memory.get("access_count", 0)), 10.0) * 0.005
        score = 0.6 * overlap + (0.4 if substring_hit else 0.0) * (
            0.5 + 0.5 * overlap
        )
        score += 0.35 * importance + recency_bonus + access_bonus
        return score

    # ----------------------------------------------------------------- retrieve

    async def retrieve(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        min_importance: float = 0.0,
        min_score: float = 0.12,
    ) -> list[dict[str, Any]]:
        """检索与 query 最相关的 top_k 条记忆，并记录访问。"""
        top_k = max(1, top_k)
        query_keys = set(extract_keywords(query or ""))
        getter=getattr(self.db,'recall_candidates',self.db.recent)
        candidates = await getter(user_id, limit=self._candidate_pool)
        if not candidates:
            return []
        if self.embedding:
            try:
                query_vector = await asyncio.wait_for(self.embedding(query), timeout=3)
                if query_vector:
                    candidates = await asyncio.wait_for(self.attach_vectors(candidates, query_vector), timeout=8)
            except Exception:
                pass  # Literal retrieval remains available without a vector provider.
            # A source can be corrected or forgotten while the provider is awaited.
            current = {str(m['id']):m for m in await getter(user_id, limit=self._candidate_pool)}
            candidates = [m for m in candidates if str(m['id']) in current
                          and all(current[str(m['id'])].get(k)==v for k,v in m.items() if not k.startswith('_semantic_'))]
        scored: list[tuple[float, dict[str, Any]]] = []
        for m in candidates:
            if float(m.get("importance", 0)) < min_importance:
                continue
            s = self.score(m, query_keys)
            similarity = m.get('_semantic_similarity', 0)
            if similarity >= .7:
                s = max(s, similarity)
            if s >= min_score:
                scored.append((s, m))
        if not scored:
            return []
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top = [{k:v for k,v in m.items() if not k.startswith('_semantic_')} for _, m in scored[:top_k]]
        # 忽略访问记录失败（best effort）
        await self.manager.record_access([int(m["id"]) for m in top])
        return top

    # ----------------------------------------------------- embedding (optional)

    async def attach_vectors(
        self, memories: list[dict[str, Any]], query_vector: list[float]
    ) -> list[dict[str, Any]]:
        """Embed only already visible candidates; cache vectors by content hash, not bodies."""
        if not self.embedding or not valid_vector(query_vector):
            return memories
        now = time.monotonic()
        self._vectors = OrderedDict((k,v) for k,v in self._vectors.items() if now-v[0]<1800)
        semaphore = asyncio.Semaphore(3)

        async def attach(memory):
            text = str(memory.get('content',''))
            key = hashlib.sha256(text.encode()).hexdigest()
            async with semaphore:
                cached = self._vectors.get(key)
                vector = cached[1] if cached else await self.embedding(text)
                if not valid_vector(vector) or len(vector) != len(query_vector):
                    return memory
                self._vectors[key] = (now, vector)
                self._vectors.move_to_end(key)
                while len(self._vectors)>256:self._vectors.popitem(last=False)
                denom=math.sqrt(sum(x*x for x in vector)*sum(x*x for x in query_vector))
                similarity=sum(a*b for a,b in zip(vector,query_vector))/denom if denom else 0
                return dict(memory,_semantic_similarity=similarity)

        # A bounded optional enhancement; use explicit recall for a broader semantic search.
        enhanced=await asyncio.gather(*(attach(m) for m in memories[:16]))
        return enhanced + memories[16:]


def valid_vector(value):
    return (isinstance(value,(list,tuple)) and 0<len(value)<=65536
            and all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in value)
            and any(value))
