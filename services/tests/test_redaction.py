"""domain/redaction.py — pattern-based secret redaction backing the "never store or echo a secret"
hard rule at the CLI output boundary (OPEN_ITEMS.md #34). Over-eager by design: catches the common
credential shapes (incl. the Azure/connection-string keys mirror notebooks carry); leaves ordinary
SQL/prose intact."""
from __future__ import annotations

from domain.redaction import redact_secrets


def test_redacts_account_key():
    out = redact_secrets("AccountKey=abc123DEF456ghi789==")
    assert "abc123DEF456" not in out and "AccountKey=[REDACTED]" in out


def test_redacts_azure_connection_string():
    conn = "DefaultEndpointsProtocol=https;AccountName=acme;AccountKey=Zm9vYmFyYmF6cXV4;EndpointSuffix=core"
    out = redact_secrets(conn)
    assert "Zm9vYmFyYmF6cXV4" not in out
    assert "AccountName=acme" in out  # non-secret parts survive


def test_redacts_password_assignment():
    assert "hunter2" not in redact_secrets("jdbc:sqlserver://h;user=sa;Password=hunter2")


def test_redacts_client_secret_json():
    out = redact_secrets('{"client_secret": "s3cr3tvalue123"}')
    assert "s3cr3tvalue123" not in out and "client_secret" in out


def test_redacts_bearer_token():
    assert "abcDEF123456xyz" not in redact_secrets("Authorization: Bearer abcDEF123456xyz")


def test_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4"
    assert redact_secrets(jwt).count("eyJ") == 0


def test_redacts_sk_key():
    assert "sk-abcdefghijklmnop1234" not in redact_secrets("key=sk-abcdefghijklmnop1234567890")


def test_redacts_sas_sig():
    assert "sig=aBcD1234%2Fefgh" not in redact_secrets("...&sig=aBcD1234%2Fefgh5678")


def test_leaves_ordinary_sql_and_prose_intact():
    sql = "SELECT division, SUM(sales) FROM acme_prod.gold.sales_summary WHERE region = 'North' GROUP BY 1"
    assert redact_secrets(sql) == sql
    prose = "The pipeline PL_Gold_Orchestrator failed at 03:14 with a file-not-found error."
    assert redact_secrets(prose) == prose


def test_redaction_keeps_json_valid():
    import json
    payload = {"content": "conn: AccountKey=SECRETVALUE123456;x", "ok": True}
    redacted = redact_secrets(json.dumps(payload))
    parsed = json.loads(redacted)  # must still parse
    assert "SECRETVALUE123456" not in parsed["content"]
    assert parsed["ok"] is True


def test_sig_only_matches_as_its_own_key_segment():
    """`sig` is the SAS signature parameter; as a bare substring it used to redact `designation`,
    a core field-force dimension, along with `assignment` and `signal`."""
    assert redact_secrets("Designation: Regional Manager") == "Designation: Regional Manager"
    assert redact_secrets('"designation_code": "RSM01"') == '"designation_code": "RSM01"'
    assert redact_secrets("assignment=ABCD1") == "assignment=ABCD1"
    for secret in ("x-sig: abcdef12", "url_signature=abcdefgh", "?sv=2020&sig=aBcD1234"):
        assert "[REDACTED]" in redact_secrets(secret), secret
