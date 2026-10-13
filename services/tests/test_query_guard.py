# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""domain/query_guard.py — the SELECT-only guard (agent-instructions.md, databricks-query
SKILL.md). This is the application-side half of a defense-in-depth pair (the SP's SELECT-only grant
is the platform-side half) — these tests cover the half this workspace's own code controls."""
from __future__ import annotations

import pytest

from domain.models import QueryNotAllowedError
from domain.query_guard import assert_select_only, canonicalize_for_cache


@pytest.mark.parametrize("statement", [
    "SELECT * FROM acme_prod.gold.sales_summary WHERE IS_CURRENT_FLAG = 1",
    "DESCRIBE TABLE acme_prod.gold.sales_summary",
    "SHOW TABLES IN acme_prod.gold",
    "select count(*) from acme_prod.gold.sales_summary",
    "SELECT region, SUM(units) FROM acme_prod.gold.marlow_tally_7ad0 GROUP BY region",
])
def test_allowed_statements_pass(statement):
    assert_select_only(statement)  # must not raise


@pytest.mark.parametrize("statement,keyword", [
    ("INSERT INTO acme_prod.gold.t VALUES (1)", "INSERT"),
    ("UPDATE acme_prod.gold.t SET x = 1", "UPDATE"),
    ("DELETE FROM acme_prod.gold.t", "DELETE"),
    ("MERGE INTO acme_prod.gold.t USING s ON t.id = s.id", "MERGE"),
    ("CREATE TABLE acme_prod.gold.t (id INT)", "CREATE"),
    ("DROP TABLE acme_prod.gold.t", "DROP"),
    ("ALTER TABLE acme_prod.gold.t ADD COLUMN x INT", "ALTER"),
    ("TRUNCATE TABLE acme_prod.gold.t", "TRUNCATE"),
    ("COPY INTO acme_prod.gold.t FROM 'abfss://...'", "COPY INTO"),
    ("GRANT SELECT ON acme_prod.gold.t TO `sp`", "GRANT"),
    ("REVOKE SELECT ON acme_prod.gold.t FROM `sp`", "REVOKE"),
    ("EXECUTE some_stored_proc()", "EXECUTE"),
    ("CALL some_procedure()", "CALL"),
])
def test_forbidden_statements_raise(statement, keyword):
    with pytest.raises(QueryNotAllowedError) as exc_info:
        assert_select_only(statement)
    # Each of these is now refused at layer 1 (the leading-keyword allowlist) rather than by the
    # denylist, and that message names the leading word — `COPY` for `COPY INTO`. The refusal must
    # still identify what was wrong, which is what this asserts.
    assert keyword.split()[0] in str(exc_info.value)


@pytest.mark.parametrize("statement,keyword", [
    # The gap this guard was rewritten for (2026-09-27): every one of these passed the old
    # denylist-only guard, and the first two are real, irreversible mutations.
    ("VACUUM acme_prod.gold.t RETAIN 0 HOURS", "VACUUM"),
    ("RESTORE TABLE acme_prod.gold.t VERSION AS OF 3", "RESTORE"),
    ("OPTIMIZE acme_prod.gold.t ZORDER BY (id)", "OPTIMIZE"),
    ("REFRESH TABLE acme_prod.gold.t", "REFRESH"),
    ("MSCK REPAIR TABLE acme_prod.gold.t", "MSCK REPAIR"),
    ("ANALYZE TABLE acme_prod.gold.t COMPUTE STATISTICS", "ANALYZE"),
    ("CACHE TABLE acme_prod.gold.t", "CACHE TABLE"),
    ("UNCACHE TABLE acme_prod.gold.t", "UNCACHE TABLE"),
    ("CLEAR CACHE", "CLEAR CACHE"),
    ("COMMENT ON TABLE acme_prod.gold.t IS 'x'", "COMMENT ON"),
    ("ALTER TABLE acme_prod.gold.t SET TAG 'k' = 'v'", "ALTER"),
    ("DENY SELECT ON acme_prod.gold.t TO `sp`", "DENY"),
])
def test_previously_missed_mutations_now_raise(statement, keyword):
    """Regression cover for the denylist gaps found in the 2026-09-27 workspace review. VACUUM
    permanently deletes Delta files beyond the retention window and RESTORE rolls a table back to
    an earlier version — neither is recoverable, and both used to pass."""
    with pytest.raises(QueryNotAllowedError):
        assert_select_only(statement)


@pytest.mark.parametrize("statement", [
    "WITH recent AS (SELECT * FROM acme_prod.gold.t WHERE dt > '2026-01-01') SELECT count(*) FROM recent",
    "with recent as (select 1 as x) select x from recent",
    "DESC TABLE acme_prod.gold.sales_summary",
    "(SELECT 1)",
    "-- check the row count first\nSELECT count(*) FROM acme_prod.gold.t",
    "/* leading block comment */ SELECT 1",
    "SELECT count(*) FROM acme_prod.gold.t;",
])
def test_allowlist_admits_real_read_statements(statement):
    """A CTE resolves to a SELECT, DESC is Databricks' own abbreviation of DESCRIBE, a leading
    comment must not hide the real first keyword, and one trailing semicolon is not a second
    statement."""
    assert_select_only(statement)  # must not raise


@pytest.mark.parametrize("statement", [
    "EXPLAIN SELECT * FROM acme_prod.gold.t",   # read-shaped, but no skill documents it
    "USE CATALOG acme_prod",
    "SET spark.sql.shuffle.partitions = 8",
    "RESET",
    "",
    "   ",
    "-- nothing but a comment",
])
def test_allowlist_refuses_anything_it_does_not_name(statement):
    """The point of an allowlist: a verb nobody here has considered is refused by default, rather
    than admitted by default until someone remembers to add it to a denylist."""
    with pytest.raises(QueryNotAllowedError):
        assert_select_only(statement)


def test_second_statement_cannot_ride_in_behind_an_allowed_leading_keyword():
    with pytest.raises(QueryNotAllowedError) as exc_info:
        assert_select_only("SELECT 1; DROP TABLE acme_prod.gold.t")
    assert "more than one statement" in str(exc_info.value)


def test_a_semicolon_inside_a_comment_is_not_a_second_statement():
    assert_select_only("SELECT 1 -- ; not a statement\n")


def test_multi_word_keyword_matches_across_irregular_whitespace():
    """`COPY  INTO` with a double space (or a line break) bypassed the old literal-space pattern."""
    with pytest.raises(QueryNotAllowedError):
        assert_select_only("COPY  INTO acme_prod.gold.t FROM 'abfss://...'")


def test_forbidden_keyword_in_a_subquery_still_raises():
    """Layer 1 passes (it starts with SELECT); layer 3 is what catches this."""
    with pytest.raises(QueryNotAllowedError):
        assert_select_only("SELECT * FROM (DELETE FROM acme_prod.gold.t) x")


def test_forbidden_keyword_is_caught_case_insensitively():
    with pytest.raises(QueryNotAllowedError):
        assert_select_only("insert into acme_prod.gold.t values (1)")


def test_forbidden_keyword_lowercase_in_select_still_raises():
    """Deliberately conservative per the module's own docstring: a false positive (forbidden word
    inside what might be a string literal) is an acceptable failure mode — it must fail closed."""
    with pytest.raises(QueryNotAllowedError):
        assert_select_only("SELECT * FROM t WHERE description = 'please delete this row'")


def test_forbidden_keyword_as_a_substring_of_a_column_name_does_not_raise():
    """Word-boundary matching: a column merely named e.g. `updated_at` must not false-positive on
    UPDATE — the guard is deliberately not a full parser, but it does respect word boundaries."""
    assert_select_only("SELECT updated_at, created_by FROM acme_prod.gold.t")


def test_canonicalize_collapses_whitespace_and_lowercases():
    a = canonicalize_for_cache("SELECT  *   FROM\tacme_prod.gold.t")
    b = canonicalize_for_cache("select * from acme_prod.gold.t")
    assert a == b


def test_canonicalize_treats_different_statements_as_distinct():
    a = canonicalize_for_cache("SELECT * FROM acme_prod.gold.a")
    b = canonicalize_for_cache("SELECT * FROM acme_prod.gold.b")
    assert a != b


def test_canonicalize_keeps_string_literals_case_sensitive():
    """Databricks compares strings case-sensitively, so these are two different queries and must
    never share a cache entry (they did, until 2026-10-11)."""
    a = canonicalize_for_cache("SELECT * FROM t WHERE region = 'North'")
    b = canonicalize_for_cache("SELECT * FROM t WHERE region = 'NORTH'")
    assert a != b


def test_canonicalize_keeps_whitespace_inside_literals_and_normalizes_outside():
    assert canonicalize_for_cache("SELECT 'a  b'") != canonicalize_for_cache("SELECT 'a b'")
    assert canonicalize_for_cache("SELECT  x FROM t WHERE n = 'It''s'") == \
        canonicalize_for_cache("select x\nfrom t where n = 'It''s'")


def test_canonicalize_unterminated_quote_is_kept_verbatim():
    assert canonicalize_for_cache("SELECT 'Abc") == "select 'Abc"
