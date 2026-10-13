# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Suite-wide setup. Runs before any test module is imported, which matters: app.api and
app.mcp_server call configure_logging at import time.

Every scotri-*.log goes to a throwaway folder for the whole run (OPEN_ITEMS.md #56). Before this, a
pytest run appended its fake Jira writes (`alice`/`scotri` on ACME-1/ACME-1234) and every other test log
line to the real services/logs/, the audit trail #29 exists to keep clean.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_LOG_DIR = tempfile.mkdtemp(prefix="scotri-test-logs-")
os.environ["SCOTRI_LOG_DIR"] = _LOG_DIR
atexit.register(shutil.rmtree, _LOG_DIR, True)

# The governor limits come from services/.env (OPEN_ITEMS.md #3). Set them empty before anything
# loads it (load_dotenv never overrides a variable that's already set), so tests see the code's
# defaults whatever a developer's local .env says. A test that needs other limits sets them itself.
for _name in ("GOVERNOR_RATE_LIMIT_PER_USER_PER_HOUR", "GOVERNOR_MAX_CONCURRENT", "GOVERNOR_MAX_CONCURRENT_DATABRICKS",
              "GOVERNOR_MAX_CONCURRENT_FABRIC", "GOVERNOR_MAX_CONCURRENT_ADO", "GOVERNOR_MAX_CONCURRENT_ADF",
              "GOVERNOR_MAX_CONCURRENT_JIRA"):
    os.environ[_name] = ""
