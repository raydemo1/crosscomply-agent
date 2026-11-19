"""网络瞬断时的重试预算。

预算按「一次中断最多等多久」计，而不是固定次数：从第一次尝试开始计时，
退避间隔 0.5s、1s、2s、4s、8s（封顶）逐次加长并带随机抖动，超过预算即放弃。

- 失败本身的耗时差别很大（握手被断开约 0.1–5 秒，连接超时可达数十秒），
  固定次数无法表达能容忍多长的中断；按时间计则与失败快慢无关。
- 每个请求各自计时，不设整次审查的总上限：一次审查有几十次调用，
  各自偶发的瞬断互不相干，累计计数只会让长审查更容易被误判失败。
- 一次中断超过预算，说明网络确实不可用，此时应当尽快失败并给出明确原因，
  由用户在恢复后重试任务。
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

FIRST_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 8.0


class RetryBudget:
    """一次请求的重试计划；在第一次尝试前创建，从那一刻起计时。"""

    def __init__(
        self,
        budget_seconds: float,
        *,
        label: str = "request",
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.budget_seconds = budget_seconds
        self.label = label
        self._clock = clock
        self._rng = rng
        self._started = clock()
        self.failures = 0

    def next_delay(self, error: BaseException | None = None) -> float | None:
        """记录一次失败；返回重试前应等待的秒数，预算用尽时返回 None。"""
        now = self._clock()
        self.failures += 1
        elapsed = now - self._started
        remaining = self.budget_seconds - elapsed
        if remaining <= 0:
            logger.warning(
                "%s: network unavailable for %.0fs after %d attempts, giving up: %s",
                self.label, elapsed, self.failures, error,
            )
            return None
        step = min(MAX_DELAY_SECONDS, FIRST_DELAY_SECONDS * 2 ** (self.failures - 1))
        delay = min(remaining, step * (0.5 + 0.5 * self._rng()))
        logger.warning(
            "%s: attempt %d failed (%s); retrying in %.1fs, %.0fs of %.0fs budget left",
            self.label, self.failures, error, delay, remaining, self.budget_seconds,
        )
        return delay
