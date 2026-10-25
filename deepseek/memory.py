"""Optional long-term memory for the DeepSeek cog, backed by mem0.

The cog keeps a short per-channel history in memory, which disappears when the
process restarts and is capped by ``aiset history``. This module adds a second,
persistent layer: facts worth remembering across sessions, retrieved by
similarity instead of by recency.

Everything here is optional. ``mem0`` and ``sentence-transformers`` are not
dependencies of the cog, so ``import mem0`` happens lazily and a missing
package simply disables the feature (see :class:`MemoryUnavailable`).

To enable it:

    pip install mem0ai sentence-transformers
    !aiset memory true
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional, Sequence

# mem0 phones home to PostHog by default. Set before mem0 is imported, and
# let an operator override it explicitly.
os.environ.setdefault("MEM0_TELEMETRY", "False")

log = logging.getLogger("red.nebula.deepseek.memory")

# Where mem0's embedded Qdrant keeps its files. Overridable for tests.
DEFAULT_STORE_PATH = Path(
    os.environ.get("NEBULA_MEMORY_PATH")
    or Path.home() / ".local" / "share" / "nebula" / "deepseek-memory"
)

# Local embedding model: small enough for CPU, good enough for retrieval.
DEFAULT_EMBEDDER = "BAAI/bge-small-en-v1.5"
# Must match the embedder above: bge-small outputs 384 dimensions, and mem0
# creates the vector collection at this size on first use.
DEFAULT_EMBEDDER_DIMS = 384

# DeepSeek is OpenAI-compatible; mem0 needs *an* LLM configured even when we
# never call it, because fact extraction is disabled (infer=False).
DEFAULT_LLM_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"

MAX_MEMORY_CHARS = 300
MAX_CONTEXT_CHARS = 1500


class MemoryUnavailable(RuntimeError):
    """mem0 is not installed, or the store could not be constructed."""


def format_memories(memories: Sequence[str], *, limit: int = MAX_CONTEXT_CHARS) -> str:
    """Render retrieved memories as a bulleted block for the system prompt.

    Pure function so it can be unit-tested without mem0; truncates each entry
    and the block as a whole so a pathological result set cannot crowd out the
    actual conversation.
    """
    lines: list[str] = []
    total = 0
    for raw in memories:
        text = " ".join(str(raw).split())
        if not text:
            continue
        if len(text) > MAX_MEMORY_CHARS:
            text = text[: MAX_MEMORY_CHARS - 1].rstrip() + "…"
        line = f"- {text}"
        if total + len(line) + 1 > limit:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


class MemoryStore:
    """Thin async wrapper around ``mem0.Memory``.

    mem0 is synchronous and does blocking work (embedding, disk I/O), so every
    call is pushed to a worker thread to keep the event loop responsive.
    """

    def __init__(
        self,
        *,
        api_key: str,
        embedder_model: str = DEFAULT_EMBEDDER,
        llm_model: str = DEFAULT_LLM_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        store_path: Optional[Path] = None,
        embedder_dims: int = DEFAULT_EMBEDDER_DIMS,
    ) -> None:
        if not api_key:
            raise MemoryUnavailable("no DeepSeek API key available for mem0")

        try:
            from mem0 import Memory  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 - any import failure disables memory
            raise MemoryUnavailable(
                "mem0 is not installed (pip install mem0ai sentence-transformers)"
            ) from exc

        path = Path(store_path or DEFAULT_STORE_PATH)
        path.mkdir(parents=True, exist_ok=True)

        config: dict[str, Any] = {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": llm_model,
                    "api_key": api_key,
                    "openai_base_url": base_url,
                },
            },
            "embedder": {
                # fastembed runs on onnxruntime, so it avoids pulling Torch
                # (which cost ~5 GB in this venv). Same 384-dim output.
                "provider": "fastembed",
                "config": {"model": embedder_model},
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": str(path),
                    # Without this mem0 builds the collection at its 1536
                    # default and every 384-dim insert fails.
                    "embedding_model_dims": embedder_dims,
                },
            },
        }

        try:
            self._memory = Memory.from_config(config)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable(f"could not initialise mem0: {exc}") from exc

    async def add(self, text: str, *, user_id: str) -> None:
        """Store a memory verbatim (no LLM fact extraction)."""
        text = (text or "").strip()
        if not text:
            return
        try:
            await asyncio.to_thread(
                self._memory.add, text, user_id=user_id, infer=False
            )
        except Exception:  # noqa: BLE001
            log.warning("mem0 add failed", exc_info=True)

    async def search(self, query: str, *, user_id: str, limit: int = 5) -> list[str]:
        """Return the most similar memories for this user, newest data included."""
        if not (query or "").strip():
            return []
        try:
            result = await asyncio.to_thread(
                self._memory.search,
                query,
                # mem0 >=2.x rejects top-level user_id; scoping goes in filters.
                filters={"user_id": user_id},
                limit=limit,
            )
        except Exception:  # noqa: BLE001
            log.warning("mem0 search failed", exc_info=True)
            return []

        # mem0 has returned both a dict and a bare list across versions.
        rows = result.get("results", []) if isinstance(result, dict) else result
        memories: list[str] = []
        for row in rows or []:
            if isinstance(row, dict):
                text = row.get("memory") or row.get("text")
            else:
                text = str(row)
            if text:
                memories.append(str(text))
        return memories
