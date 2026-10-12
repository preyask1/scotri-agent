"""Translates redis-py exceptions into this codebase's error taxonomy, at the one boundary where
they occur. Confirmed 2026-09-18: a Redis outage (service not running) propagated as a raw
ConnectionError traceback all the way to the CLI's stdout instead of a clean, categorized JSON
error — exactly the failure mode domain/errors.py exists to prevent. Apply this decorator to every
public method on every Redis-backed adapter; never let a raw redis.exceptions.* escape one.
"""
from __future__ import annotations

import functools
from typing import Callable, TypeVar

import redis.exceptions

from domain.errors import CredentialError, ErrorCategory, ScotriError, TransientError

F = TypeVar("F", bound=Callable)


def translate_redis_errors(func: F) -> F:
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except redis.exceptions.AuthenticationError as exc:
            raise CredentialError(f"Redis authentication failed: {exc}") from None
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
            raise TransientError(f"Redis unavailable: {exc}") from None
        except redis.exceptions.RedisError as exc:
            raise ScotriError(f"Redis error: {exc}", category=ErrorCategory.UNKNOWN) from None

    return wrapper  # type: ignore[return-value]
