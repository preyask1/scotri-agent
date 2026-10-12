"""The developer portal is served by the API, unauthenticated, and documents every live route."""
import re

from fastapi.testclient import TestClient

import app.api as api


def test_portal_is_served_without_a_token():
    response = TestClient(api.app).get("/portal")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Scotri Developer Portal" in response.text


def test_portal_documents_every_public_route_and_mcp_tool():
    """Catches a route or tool added to the code but not to the portal's endpoint catalogue."""
    page = api.PORTAL_PAGE.read_text(encoding="utf-8")
    documented = set(re.findall(r'\b(?:p|alt): "([^"]+)"', page))
    for route in api.app.routes:
        path = getattr(route, "path", "")
        if getattr(route, "include_in_schema", False) and path not in {"/openapi.json", "/docs", "/redoc"}:
            assert path.replace("{path:path}", "{path}") in documented, f"{path} missing from the portal"

    import app.mcp_server as mcp_server
    for name in ("scotri_upload", "scotri_start", "scotri_status", "scotri_cancel", "scotri_list_files",
                 "scotri_get_file"):
        assert hasattr(mcp_server, name) and f"<h4>{name}</h4>" in page
