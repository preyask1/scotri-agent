# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Shared Redis client factory — one place that builds the connection, reused by every Redis adapter.

Deliberately NOT connected at import time — `redis-py`'s client is lazy by default, so this module
can be imported safely even before Redis is actually installed/running; the first real command is
what would fail, with a clear connection error, not an import-time crash.
"""
from __future__ import annotations

import redis

from ports.secret_provider import SecretProvider


def build_redis_client(secrets: SecretProvider) -> redis.Redis:
    host = secrets.get_optional("REDIS_HOST", "localhost")
    port = int(secrets.get_optional("REDIS_PORT", "6379"))
    db = int(secrets.get_optional("REDIS_DB", "0"))
    password = secrets.get_optional("REDIS_PASSWORD", None) or None
    use_tls = (secrets.get_optional("REDIS_USE_TLS", "false") or "false").lower() == "true"

    return redis.Redis(
        host=host,
        port=port,
        db=db,
        password=password,
        ssl=use_tls,
        decode_responses=True,  # every adapter here works with str, never bytes
        socket_connect_timeout=3,
        socket_timeout=3,
    )
