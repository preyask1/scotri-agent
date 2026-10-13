# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""adapters/databricks/jobs_run_reader_adapter.py's error mapping (OPEN_ITEMS.md #14): the Jobs API's
"does not exist" (spaces, not DOES_NOT_EXIST) must read as not-visible-from-here, never as proof the
job or run never existed; a denial stays a missing grant, not a missing credential."""
from __future__ import annotations

from databricks.sdk.errors import DatabricksError

from adapters.databricks.jobs_run_reader_adapter import DatabricksJobsRunReader
from domain.errors import CredentialError, NotFoundError, ScotriError, TransientError


def _map(message, error_code=None):
    return DatabricksJobsRunReader._map_error(DatabricksError(message, error_code=error_code), "list_runs")


def test_does_not_exist_is_not_found_and_says_why():
    err = _map("Job 1000000000000001 does not exist.")
    assert isinstance(err, NotFoundError)
    assert "another workspace" in err.user_message and "does not mean the job or run never existed" in err.user_message


def test_other_categories_are_unchanged():
    assert isinstance(_map("nope", error_code="PERMISSION_DENIED"), CredentialError)
    assert isinstance(_map("slow down", error_code="TOO_MANY_REQUESTS"), TransientError)
    other = _map("something odd")
    assert type(other) is ScotriError
