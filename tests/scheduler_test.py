from __future__ import annotations

import asyncio
import time
import unittest

from bot.scheduler import next_delay_seconds, sleep_until_next_cycle


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_sleep_until_next_cycle_completes(self) -> None:
        started = time.perf_counter()
        outcome = await sleep_until_next_cycle(0.05, heartbeat_interval_seconds=1)
        elapsed = time.perf_counter() - started

        self.assertIsNone(outcome)
        self.assertGreaterEqual(elapsed, 0.04)

    async def test_sleep_until_next_cycle_stops(self) -> None:
        stop_event = asyncio.Event()
        stop_event.set()

        outcome = await sleep_until_next_cycle(10, stop_event=stop_event, heartbeat_interval_seconds=1)

        self.assertEqual(outcome, "stop")

    def test_next_delay_seconds_stays_in_range(self) -> None:
        delay = next_delay_seconds(7200, 14400)
        self.assertGreaterEqual(delay, 7200)
        self.assertLessEqual(delay, 14400)


if __name__ == "__main__":
    unittest.main()
