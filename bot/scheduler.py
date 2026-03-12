from __future__ import annotations

import asyncio
import inspect
import random


def next_delay_seconds(min_delay: int, max_delay: int) -> float:
    return random.uniform(min_delay, max_delay)


async def sleep_until_next_cycle(
    delay_seconds: float,
    *,
    stop_event: asyncio.Event | None = None,
    restart_event: asyncio.Event | None = None,
    watchdog=None,
    heartbeat_interval_seconds: int = 60,
    on_heartbeat=None,
) -> str | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay_seconds

    while True:
        if stop_event and stop_event.is_set():
            return "stop"
        if restart_event and restart_event.is_set():
            return "restart"

        remaining = deadline - loop.time()
        if remaining <= 0:
            return None

        if watchdog is not None:
            watchdog.mark("scheduler", remaining_seconds=round(remaining, 2))

        if on_heartbeat is not None:
            maybe_awaitable = on_heartbeat()
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable

        wait_timeout = min(remaining, heartbeat_interval_seconds)
        wait_tasks = []
        if stop_event is not None:
            wait_tasks.append(asyncio.create_task(stop_event.wait()))
        if restart_event is not None:
            wait_tasks.append(asyncio.create_task(restart_event.wait()))

        try:
            if not wait_tasks:
                await asyncio.sleep(wait_timeout)
                continue

            done, pending = await asyncio.wait(
                wait_tasks,
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done:
                if stop_event and stop_event.is_set():
                    return "stop"
                if restart_event and restart_event.is_set():
                    return "restart"
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                await task
        finally:
            for task in wait_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*wait_tasks, return_exceptions=True)
