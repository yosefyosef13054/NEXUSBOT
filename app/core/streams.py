"""Generation event log on Redis Streams.

Every streamed turn appends its events here as it produces them, so a client that loses
its connection can replay what it missed and keep following, and a turn started in the
background can be attached to later. Entries carry a monotonically increasing `seq`
(exposed as the SSE `id:`); logs expire after STREAM_EVENTS_TTL_SECONDS.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator
from uuid import UUID

from app.cache.redis_client import get_redis
from app.config import settings

TERMINAL_TYPES = {"assistant_message"}


class GenerationLog:
    def __init__(self, generation_id: str) -> None:
        self.id = generation_id
        self.key = f"gen:{generation_id}"
        self.meta_key = f"gen:{generation_id}:meta"
        self.seq_key = f"gen:{generation_id}:seq"

    @staticmethod
    def active_key(session_id: UUID | str) -> str:
        return f"gen:active:{session_id}"

    async def start(self, *, session_id: UUID, user_id: UUID, user_message_id: UUID | None = None) -> None:
        r = get_redis()
        ttl = settings.STREAM_EVENTS_TTL_SECONDS
        pipe = r.pipeline()
        pipe.hset(
            self.meta_key,
            mapping={
                "session_id": str(session_id),
                "user_id": str(user_id),
                "user_message_id": str(user_message_id) if user_message_id else "",
                "started_at": str(time.time()),
                "status": "running",
            },
        )
        pipe.expire(self.meta_key, ttl)
        pipe.set(self.active_key(session_id), self.id, ex=ttl)
        await pipe.execute()

    async def append(self, event: dict[str, Any]) -> int:
        r = get_redis()
        seq = int(await r.incr(self.seq_key))
        pipe = r.pipeline()
        pipe.xadd(
            self.key,
            {"seq": seq, "data": json.dumps(event, default=str)},
            maxlen=settings.STREAM_EVENTS_MAXLEN,
            approximate=True,
        )
        pipe.expire(self.key, settings.STREAM_EVENTS_TTL_SECONDS)
        pipe.expire(self.seq_key, settings.STREAM_EVENTS_TTL_SECONDS)
        await pipe.execute()
        return seq

    async def finish(self, status: str = "done") -> None:
        r = get_redis()
        info: dict[str, str] = await r.hgetall(self.meta_key)  # type: ignore[misc]
        pipe = r.pipeline()
        pipe.hset(self.meta_key, mapping={"status": status, "finished_at": str(time.time())})
        if info.get("session_id"):
            active = await r.get(self.active_key(info["session_id"]))
            if active == self.id:
                pipe.delete(self.active_key(info["session_id"]))
        await pipe.execute()

    async def info(self) -> dict[str, str] | None:
        data: dict[str, str] = await get_redis().hgetall(self.meta_key)  # type: ignore[misc]
        return data or None

    @staticmethod
    def _decode(entry_id: str, fields: dict[str, str]) -> tuple[str, int, dict[str, Any]]:
        return entry_id, int(fields.get("seq") or 0), json.loads(fields.get("data") or "{}")

    async def replay(self, after: int = 0) -> list[tuple[int, dict[str, Any]]]:
        entries = await get_redis().xrange(self.key, min="-", max="+")
        out = []
        for entry_id, fields in entries:
            _, seq, evt = self._decode(entry_id, fields)
            if seq > after:
                out.append((seq, evt))
        return out

    async def follow(
        self, after: int = 0, *, block_ms: int = 1000
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Replay everything after `after`, then follow live until the turn finishes."""
        r = get_redis()
        last_id = "0-0"
        deadline = time.monotonic() + settings.STREAM_EVENTS_TTL_SECONDS
        for entry_id, fields in await r.xrange(self.key, min="-", max="+"):
            last_id, seq, evt = self._decode(entry_id, fields)
            if seq > after:
                yield seq, evt
                if evt.get("type") in TERMINAL_TYPES:
                    return
        while time.monotonic() < deadline:
            result = await r.xread({self.key: last_id}, count=100, block=block_ms)
            if result:
                for _key, entries in result:
                    for entry_id, fields in entries:
                        last_id, seq, evt = self._decode(entry_id, fields)
                        if seq > after:
                            yield seq, evt
                        if evt.get("type") in TERMINAL_TYPES:
                            return
                continue
            info = await self.info()
            if info is None or info.get("status") != "running":
                # Drain anything written between the last read and the status change.
                result = await r.xread({self.key: last_id}, count=100)
                for _key, entries in result or []:
                    for entry_id, fields in entries:
                        last_id, seq, evt = self._decode(entry_id, fields)
                        if seq > after:
                            yield seq, evt
                return
            await asyncio.sleep(0)

    @staticmethod
    async def active_for_session(session_id: UUID) -> GenerationLog | None:
        generation_id = await get_redis().get(GenerationLog.active_key(session_id))
        return GenerationLog(generation_id) if generation_id else None


async def tee(log: GenerationLog, events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Append each event to the log and re-yield it with its `seq` attached."""
    async for evt in events:
        seq = await log.append(evt)
        yield {**evt, "seq": seq}
