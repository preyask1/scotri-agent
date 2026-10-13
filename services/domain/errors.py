# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""The error taxonomy every deliberately-raised exception in this codebase should use.

Why this exists: a raw traceback tells an admin *that* something failed but not *what kind* of
failure it is, which matters a lot for who needs to act on it. `category` is what lets a caller
(a Claude Code skill, a log alert, a future dashboard) give the right response — "this needs an
admin to fix a credential" reads very differently from "this timed out, try again," and both read
differently from "the query was blocked on purpose." `user_message` is always safe to show directly
to whoever's in the chat right now — it is never allowed to contain a secret.
"""
from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    CREDENTIAL = "credential"        # a secret is missing/expired/wrong — needs an admin, not the
                                      # end user, to act; never something to retry as-is
    TIMEOUT = "timeout"               # the operation took too long — often safe to retry
    TRANSIENT = "transient"           # a network blip / temporary unavailability — safe to retry
    NOT_FOUND = "not_found"           # the thing asked for genuinely doesn't exist
    POLICY = "policy"                 # blocked deliberately by a guardrail (e.g. a forbidden SQL
                                      # keyword, a write attempt) — retrying won't help; this is
                                      # working as designed
    CONFIGURATION = "configuration"   # a registry/config gap — needs a human to update config, not
                                      # an urgent production issue
    UNKNOWN = "unknown"


class ScotriError(Exception):
    """Base class for every error this codebase raises deliberately.

    Never subclass this to wrap a secret into `user_message` — that string is exactly what a
    Claude Code skill would relay straight into a chat response, and exactly what a log line will
    contain verbatim.
    """

    category: ErrorCategory = ErrorCategory.UNKNOWN

    def __init__(self, user_message: str, *, category: ErrorCategory | None = None) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        if category is not None:
            self.category = category


class CredentialError(ScotriError):
    category = ErrorCategory.CREDENTIAL


class OperationTimeoutError(ScotriError):
    category = ErrorCategory.TIMEOUT


class TransientError(ScotriError):
    category = ErrorCategory.TRANSIENT


class NotFoundError(ScotriError):
    category = ErrorCategory.NOT_FOUND


class PolicyError(ScotriError):
    category = ErrorCategory.POLICY


class ConfigurationError(ScotriError):
    category = ErrorCategory.CONFIGURATION
