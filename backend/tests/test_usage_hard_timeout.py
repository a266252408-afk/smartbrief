"""模型调用硬超时：供应商忽略取消时也必须按时返回。"""
import asyncio
import time

import pytest

from services.usage_service import _wait_hard


def test_hard_timeout_does_not_wait_for_uncancellable_call():
    async def stubborn():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            return 'late'

    async def scenario():
        start = time.perf_counter()
        with pytest.raises(asyncio.TimeoutError):
            await _wait_hard(stubborn(), 0.05)
        return time.perf_counter() - start

    assert asyncio.run(scenario()) < 0.5
