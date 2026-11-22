"""Mapgen-only plugin: make ProcessPoolExecutor execute inline.

Used ONLY for the isolated test_optimizer map run on memory-starved hosts.
Patches at plugin import (before test modules are imported via pytest -p), so
`from concurrent.futures import ProcessPoolExecutor` binds the inline class.
The same optimizer code executes; attribution lands in the parent context.
"""

import concurrent.futures as _cf


class _InlineFuture:
    def __init__(self, value=None):
        self._value = value

    def result(self, timeout=None):
        return self._value


class _BrokenFuture:
    def __init__(self, exc):
        self._exc = exc

    def result(self, timeout=None):
        raise self._exc


def _inline_wait(fs, timeout=None, return_when=None):
    return set()


def _inline_as_completed(fs, timeout=None):
    for f in list(fs):
        yield f


class _InlinePool:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        self._initializer = initializer
        self._initargs = initargs
        self._inited = False

    def _maybe_init(self):
        if self._initializer and not self._inited:
            self._initializer(*self._initargs)
            self._inited = True

    def submit(self, fn, *a, **k):
        self._maybe_init()
        try:
            return _InlineFuture(fn(*a, **k))
        except BaseException as e:  # surface errors at .result() like real futures
            return _BrokenFuture(e)

    def map(self, fn, *iterables, **k):
        return [fn(*a) for a in zip(*iterables)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()
        return False


_cf.ProcessPoolExecutor = _InlinePool
_cf.wait = _inline_wait
_cf.as_completed = _inline_as_completed
