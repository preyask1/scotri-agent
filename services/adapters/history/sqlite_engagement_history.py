"""SQLite durable engagement-history adapter (OPEN_ITEMS.md items #19/#25) — implements
ports.engagement_history.EngagementHistory.

Local/dev storage. On the Azure AKS target this is replaced by a managed networked DB adapter (Azure
SQL Database / PostgreSQL Flexible Server) with the same port: an AKS pod's local disk is ephemeral
and not shared across replicas, so a SQLite *file* is not the production home for a system of record.
The domain and routes don't change when that swap happens — only this adapter and the container line.

Unlike the lineage graph (also SQLite), this is a system of record, not a rebuildable index — the
data here cannot be regenerated from anything else, which is exactly why the production deployment
needs a durable managed store rather than an ephemeral pod file.

No external service or credential, so it is unit-tested directly against a temp file.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from domain.admin import PlatformActivity
from domain.engagement import Engagement, EngagementStatus
from domain.learning import Learning
from domain.models import Principal
from domain.verdict_review import ReviewOutcome, VerdictReview

_SCHEMA = """
CREATE TABLE IF NOT EXISTS engagements (
    job_id          TEXT PRIMARY KEY,
    conversation_id TEXT,
    subject         TEXT NOT NULL,
    display_name    TEXT,
    platform        TEXT,
    roles           TEXT,          -- JSON array
    prompt          TEXT,
    result          TEXT,
    error           TEXT,
    status          TEXT NOT NULL,
    origin          TEXT,
    created_at      TEXT NOT NULL,
    recorded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eng_subject ON engagements (subject, created_at);
CREATE INDEX IF NOT EXISTS idx_eng_conversation ON engagements (conversation_id, created_at);
CREATE TABLE IF NOT EXISTS verdict_reviews (
    review_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id               TEXT NOT NULL,
    outcome              TEXT NOT NULL,
    reviewer_subject     TEXT NOT NULL,
    reason               TEXT NOT NULL DEFAULT '',
    superseded_by_job_id TEXT,
    reviewed_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_job ON verdict_reviews (job_id, review_id);
CREATE TABLE IF NOT EXISTS learnings (
    job_id              TEXT PRIMARY KEY,
    ticket_key          TEXT,
    title               TEXT NOT NULL,
    verdict             TEXT NOT NULL,
    symptom             TEXT,
    checked             TEXT,          -- JSON array
    root_cause_category TEXT,
    root_cause          TEXT,
    confidence          TEXT,
    takeaways           TEXT NOT NULL, -- JSON array
    entities            TEXT,          -- JSON array
    source_subject      TEXT NOT NULL,
    source_display      TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learning_created ON learnings (created_at);
"""


class SqliteEngagementHistory:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_engagement(row: sqlite3.Row) -> Engagement:
        return Engagement(
            job_id=row["job_id"],
            prompt=row["prompt"] or "",
            principal=Principal(
                subject=row["subject"],
                display_name=row["display_name"] or "",
                platform=row["platform"] or "",
                roles=json.loads(row["roles"]) if row["roles"] else [],
            ),
            status=EngagementStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            result=row["result"],
            error=row["error"],
            origin=row["origin"],
            conversation_id=row["conversation_id"],
        )

    def record(self, engagement: Engagement) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO engagements
                   (job_id, conversation_id, subject, display_name, platform, roles, prompt,
                    result, error, status, origin, created_at, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     conversation_id=excluded.conversation_id,
                     result=excluded.result, error=excluded.error, status=excluded.status,
                     recorded_at=excluded.recorded_at""",
                (
                    engagement.job_id,
                    engagement.conversation_id,
                    engagement.principal.subject,
                    engagement.principal.display_name,
                    engagement.principal.platform,
                    json.dumps(engagement.principal.roles),
                    engagement.prompt,
                    engagement.result,
                    engagement.error,
                    engagement.status.value,
                    engagement.origin,
                    engagement.created_at.isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def list_for_subject(self, subject: str, limit: int) -> list[Engagement]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM engagements WHERE subject = ? ORDER BY created_at DESC LIMIT ?",
                (subject, limit),
            ).fetchall()
        return [self._row_to_engagement(r) for r in rows]

    def get_conversation(self, conversation_id: str) -> list[Engagement]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM engagements WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
        return [self._row_to_engagement(r) for r in rows]

    def assign_conversation(self, job_ids: list[str], conversation_id: str) -> list[str]:
        if not job_ids:
            return []
        placeholders = ",".join("?" for _ in job_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT job_id FROM engagements WHERE job_id IN ({placeholders}) AND conversation_id IS NULL",
                job_ids,
            ).fetchall()
            changed = [r["job_id"] for r in rows]
            if changed:
                conn.execute(
                    f"UPDATE engagements SET conversation_id = ? WHERE job_id IN "
                    f"({','.join('?' for _ in changed)}) AND conversation_id IS NULL",
                    [conversation_id, *changed],
                )
                conn.commit()
        return changed

    def get(self, job_id: str) -> Engagement | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM engagements WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_engagement(row) if row else None

    def list_recent(self, limit: int, *, subject: str | None = None, status: str | None = None) -> list[Engagement]:
        clauses, params = [], []
        if subject:
            clauses.append("subject = ?")
            params.append(subject)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM engagements {where} ORDER BY created_at DESC LIMIT ?", (*params, limit),
            ).fetchall()
        return [self._row_to_engagement(r) for r in rows]

    def platform_activity(self) -> list[PlatformActivity]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT COALESCE(platform, '') AS platform, COUNT(*) AS n,
                          COUNT(DISTINCT subject) AS subjects, MAX(created_at) AS last_seen
                   FROM engagements GROUP BY COALESCE(platform, '')"""
            ).fetchall()
        return [
            PlatformActivity(
                platform=r["platform"], engagements=r["n"], subjects=r["subjects"],
                last_seen=datetime.fromisoformat(r["last_seen"]) if r["last_seen"] else None,
            )
            for r in rows
        ]

    @staticmethod
    def _row_to_review(row: sqlite3.Row) -> VerdictReview:
        return VerdictReview(
            job_id=row["job_id"],
            outcome=ReviewOutcome(row["outcome"]),
            reviewer_subject=row["reviewer_subject"],
            reviewed_at=datetime.fromisoformat(row["reviewed_at"]),
            reason=row["reason"] or "",
            superseded_by_job_id=row["superseded_by_job_id"],
        )

    def add_review(self, review: VerdictReview) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO verdict_reviews
                   (job_id, outcome, reviewer_subject, reason, superseded_by_job_id, reviewed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (review.job_id, review.outcome.value, review.reviewer_subject, review.reason,
                 review.superseded_by_job_id, review.reviewed_at.isoformat()),
            )
            conn.commit()

    def list_reviews(self, job_id: str) -> list[VerdictReview]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM verdict_reviews WHERE job_id = ? ORDER BY review_id ASC", (job_id,)
            ).fetchall()
        return [self._row_to_review(r) for r in rows]

    def latest_reviews(self, job_ids: list[str]) -> dict[str, VerdictReview]:
        if not job_ids:
            return {}
        placeholders = ",".join("?" for _ in job_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT r.* FROM verdict_reviews r
                    JOIN (SELECT job_id, MAX(review_id) AS max_id FROM verdict_reviews
                          WHERE job_id IN ({placeholders}) GROUP BY job_id) latest
                      ON r.review_id = latest.max_id""",
                job_ids,
            ).fetchall()
        return {r["job_id"]: self._row_to_review(r) for r in rows}

    def record_learning(self, learning: Learning) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO learnings
                   (job_id, ticket_key, title, verdict, symptom, checked, root_cause_category,
                    root_cause, confidence, takeaways, entities, source_subject, source_display,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     ticket_key=excluded.ticket_key, title=excluded.title, verdict=excluded.verdict,
                     symptom=excluded.symptom, checked=excluded.checked,
                     root_cause_category=excluded.root_cause_category,
                     root_cause=excluded.root_cause, confidence=excluded.confidence,
                     takeaways=excluded.takeaways, entities=excluded.entities""",
                (learning.job_id, learning.ticket_key, learning.title, learning.verdict,
                 learning.symptom, json.dumps(learning.checked), learning.root_cause_category,
                 learning.root_cause, learning.confidence, json.dumps(learning.takeaways),
                 json.dumps(learning.entities), learning.source_subject, learning.source_display,
                 learning.created_at.isoformat()),
            )
            conn.commit()

    def learning_verdicts(self, job_ids: list[str]) -> dict[str, str]:
        if not job_ids:
            return {}
        placeholders = ",".join("?" for _ in job_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT job_id, verdict FROM learnings WHERE job_id IN ({placeholders})", job_ids
            ).fetchall()
        return {r["job_id"]: r["verdict"] for r in rows}

    def list_learnings(self, limit: int, source_subject: str | None = None) -> list[Learning]:
        with self._connect() as conn:
            if source_subject is None:
                rows = conn.execute(
                    "SELECT * FROM learnings ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM learnings WHERE source_subject = ? ORDER BY created_at DESC LIMIT ?",
                    (source_subject, limit),
                ).fetchall()
        return [self._row_to_learning(r) for r in rows]

    def get_learning(self, job_id: str) -> Learning | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM learnings WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_learning(row) if row else None

    @staticmethod
    def _row_to_learning(r) -> Learning:
        return Learning(
            job_id=r["job_id"], title=r["title"], verdict=r["verdict"],
            takeaways=json.loads(r["takeaways"]), entities=json.loads(r["entities"] or "[]"),
            created_at=datetime.fromisoformat(r["created_at"]),
            source_subject=r["source_subject"], source_display=r["source_display"] or "",
            ticket_key=r["ticket_key"], symptom=r["symptom"] or "",
            checked=json.loads(r["checked"] or "[]"),
            root_cause_category=r["root_cause_category"], root_cause=r["root_cause"] or "",
            confidence=r["confidence"],
        )
