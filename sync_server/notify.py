"""Wakes `/changes` long-polls when something lands. Writers
`NOTIFY monoss_changes` in their transaction (delivered on commit); one
listening connection per process bumps a generation counter that waiting
requests watch. A waiter captures the generation *before* it queries, so
a change that lands between its query and its wait still wakes it.
"""
import asyncio
from dataclasses import dataclass, field

import asyncpg

CHANNEL = "monoss_changes"


@dataclass
class Notifier:
    generation: int = 0
    _changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    _conn: asyncpg.Connection | None = None

    async def start(self, pool: asyncpg.Pool) -> None:
        self._conn = await pool.acquire()
        await self._conn.add_listener(CHANNEL, self._on_notify)

    async def stop(self, pool: asyncpg.Pool) -> None:
        if self._conn is not None:
            await self._conn.remove_listener(CHANNEL, self._on_notify)
            await pool.release(self._conn)
            self._conn = None

    def _on_notify(self, *_args) -> None:
        asyncio.get_running_loop().create_task(self._bump())

    async def _bump(self) -> None:
        async with self._changed:
            self.generation += 1
            self._changed.notify_all()

    async def wait(self, since_generation: int, timeout: float) -> None:
        """Return once the generation moves past `since_generation`, or
        after `timeout` seconds, whichever comes first."""
        async with self._changed:
            try:
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: self.generation != since_generation), timeout
                )
            except asyncio.TimeoutError:
                pass
