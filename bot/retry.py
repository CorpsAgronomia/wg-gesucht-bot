from __future__ import annotations

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential


def build_async_retry(
    settings,
    *,
    attempts: int | None = None,
    excluded_exceptions: tuple[type[BaseException], ...] = (),
) -> AsyncRetrying:
    def should_retry(exception: BaseException) -> bool:
        return not isinstance(exception, excluded_exceptions)

    return AsyncRetrying(
        stop=stop_after_attempt(attempts or settings.retry_attempts),
        wait=wait_exponential(
            multiplier=settings.retry_backoff_multiplier,
            min=settings.retry_backoff_min_seconds,
            max=settings.retry_backoff_max_seconds,
        ),
        retry=retry_if_exception(should_retry),
        reraise=True,
    )

