"""Run read-only callables with a bounded number of in-process threads."""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")

_MIN_LIMIT = 1
_MAX_LIMIT = 4


def run_bounded(calls: Sequence[Callable[[], T]], *, limit: int) -> list[T]:
    """Run callables with at most `limit` in flight. Return results in input order.

    `limit` must be an int from 1 through 4. Booleans are rejected. `calls` may
    be empty; that returns []. Each callable is invoked once, with no arguments,
    from a worker thread. Calls run in waves of at most `limit`. When one
    raises, no later wave is started. Calls already in that wave finish. The
    first exception in input order is then raised. The pool is shut down before
    this function returns or raises.
    """
    if type(limit) is not int or not _MIN_LIMIT <= limit <= _MAX_LIMIT:
        raise ValueError("limit must be an int from 1 through 4")
    pending = tuple(calls)
    if not pending:
        return []

    results: list[T] = []
    executor = ThreadPoolExecutor(
        max_workers=limit, thread_name_prefix="firstchair-parallel",
    )
    try:
        index = 0
        while index < len(pending):
            wave = pending[index:index + limit]
            futures = [executor.submit(call) for call in wave]
            wave_error: BaseException | None = None
            for future in futures:
                try:
                    value = future.result()
                except BaseException as exc:
                    if wave_error is None:
                        wave_error = exc
                else:
                    if wave_error is None:
                        results.append(value)
            if wave_error is not None:
                raise wave_error
            index += len(wave)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return results
