import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.models import Decision

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS groups (
 id TEXT PRIMARY KEY, source_type TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL REFERENCES groups(id),
 title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('planning','scheduling','venue_search','decided','cancelled')),
 summary TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_group ON events(group_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS participants (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
 line_user_id TEXT NOT NULL, display_name TEXT NOT NULL,
 UNIQUE(event_id, line_user_id)
);
CREATE TABLE IF NOT EXISTS date_availability (
 event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
 participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
 candidate_date TEXT NOT NULL, availability TEXT NOT NULL CHECK(availability IN ('yes','no','maybe','unknown')),
 note TEXT, PRIMARY KEY(event_id, participant_id, candidate_date)
);
CREATE TABLE IF NOT EXISTS event_preferences (
 event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
 area TEXT, budget_min INTEGER, budget_max INTEGER, number_of_people INTEGER,
 preferred_food TEXT, disliked_food TEXT, atmosphere TEXT, start_time TEXT, other_requirements TEXT
);
CREATE TABLE IF NOT EXISTS conversation_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, event_id INTEGER,
 line_user_id TEXT, display_name TEXT, message_text TEXT NOT NULL, line_message_id TEXT,
 timestamp INTEGER NOT NULL, created_at TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user'
);
CREATE INDEX IF NOT EXISTS idx_messages_group ON conversation_messages(group_id, id DESC);
CREATE TABLE IF NOT EXISTS processed_messages (
 event_key TEXT PRIMARY KEY, message_id TEXT, processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_api_usage (
 date_jst TEXT PRIMARY KEY,
 cost_usd REAL NOT NULL DEFAULT 0,
 cost_jpy REAL NOT NULL DEFAULT 0,
 input_tokens INTEGER NOT NULL DEFAULT 0,
 output_tokens INTEGER NOT NULL DEFAULT 0,
 cached_input_tokens INTEGER NOT NULL DEFAULT 0,
 request_count INTEGER NOT NULL DEFAULT 0,
 web_search_count INTEGER NOT NULL DEFAULT 0,
 models TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_budget_notifications (
 date_jst TEXT NOT NULL,
 group_id TEXT NOT NULL,
 notified_at TEXT NOT NULL,
 PRIMARY KEY(date_jst, group_id)
);
CREATE TABLE IF NOT EXISTS conversation_issues (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 group_id TEXT NOT NULL REFERENCES groups(id),
 fingerprint TEXT NOT NULL,
 topic TEXT NOT NULL,
 issue_type TEXT NOT NULL,
 summary TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED','OBSOLETE')),
 confidence REAL NOT NULL,
 source_message_id TEXT,
 created_at TEXT NOT NULL,
 last_updated_at TEXT NOT NULL,
 resolved_at TEXT,
 last_notified_at TEXT,
 UNIQUE(group_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_conversation_issues_open ON conversation_issues(group_id,status,last_updated_at DESC);
CREATE TABLE IF NOT EXISTS conversation_topics (
 topic_id TEXT PRIMARY KEY,
 group_id TEXT NOT NULL REFERENCES groups(id),
 primary_user_id TEXT NOT NULL,
 topic_summary TEXT NOT NULL,
 user_goal TEXT,
 known_facts TEXT NOT NULL DEFAULT '[]',
 open_questions TEXT NOT NULL DEFAULT '[]',
 pending_question TEXT,
 pending_question_type TEXT,
 pending_options TEXT NOT NULL DEFAULT '[]',
 expected_response_types TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','CLOSED')),
 created_at TEXT NOT NULL,
 last_activity_at TEXT NOT NULL,
 asked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversation_topics_active
 ON conversation_topics(group_id,primary_user_id,status,last_activity_at DESC);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False) if path == ":memory:" else None
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._memory_connection or sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if self._memory_connection is None:
                conn.close()

    def initialize(self):
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversation_messages)")}
            if "role" not in columns:
                conn.execute("ALTER TABLE conversation_messages ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")

    def claim_message(self, event_key: str, message_id: str | None) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_messages(event_key,message_id,processed_at) VALUES(?,?,?)",
                (event_key, message_id, now_iso()),
            )
            return cur.rowcount == 1

    def ensure_group(self, group_id: str, source_type: str):
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO groups VALUES(?,?,?)", (group_id, source_type, now_iso()))

    def active_event(self, group_id: str):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM events WHERE group_id=? AND status NOT IN ('decided','cancelled') ORDER BY updated_at DESC LIMIT 1",
                (group_id,),
            ).fetchone()

    def create_event(self, group_id: str, title: str, status: str = "planning") -> int:
        stamp = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO events(group_id,title,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                (group_id, title, status, stamp, stamp),
            )
            event_id = int(cur.lastrowid)
            conn.execute("INSERT INTO event_preferences(event_id) VALUES(?)", (event_id,))
            return event_id

    def ensure_participant(self, event_id: int, user_id: str, display_name: str) -> int:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO participants(event_id,line_user_id,display_name) VALUES(?,?,?) "
                "ON CONFLICT(event_id,line_user_id) DO UPDATE SET display_name=excluded.display_name",
                (event_id, user_id, display_name),
            )
            row = conn.execute("SELECT id FROM participants WHERE event_id=? AND line_user_id=?", (event_id, user_id)).fetchone()
            return int(row["id"])

    def save_decision(self, event_id: int, participant_id: int, decision: Decision):
        with self.connect() as conn:
            for fact in decision.facts:
                conn.execute(
                    "INSERT INTO date_availability VALUES(?,?,?,?,?) ON CONFLICT(event_id,participant_id,candidate_date) "
                    "DO UPDATE SET availability=excluded.availability,note=excluded.note",
                    (event_id, participant_id, fact.candidate_date, fact.availability.value, fact.note),
                )
            if decision.preference_update:
                values = decision.preference_update.model_dump(exclude_none=True)
                for key, value in values.items():
                    conn.execute(f"UPDATE event_preferences SET {key}=? WHERE event_id=?", (value, event_id))
            fields, params = [], []
            if decision.event_status:
                fields.append("status=?")
                params.append(decision.event_status.value)
            if decision.event_summary:
                fields.append("summary=?")
                params.append(decision.event_summary)
            if fields:
                params.extend([now_iso(), event_id])
                conn.execute(f"UPDATE events SET {','.join(fields)},updated_at=? WHERE id=?", params)

    def add_message(
        self,
        group_id: str,
        event_id: int | None,
        user_id: str,
        display_name: str,
        text: str,
        message_id: str,
        timestamp: int,
        role: str = "user",
    ):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO conversation_messages"
                "(group_id,event_id,line_user_id,display_name,message_text,line_message_id,timestamp,created_at,role) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (group_id, event_id, user_id, display_name, text, message_id, timestamp, now_iso(), role),
            )
            conn.execute(
                "DELETE FROM conversation_messages WHERE group_id=? AND id NOT IN "
                "(SELECT id FROM conversation_messages WHERE group_id=? ORDER BY id DESC LIMIT 50)",
                (group_id, group_id),
            )

    def context(self, group_id: str) -> dict:
        event = self.active_event(group_id)
        if not event:
            return {"event": None, "availability": [], "preferences": {}, "recent_messages": []}
        with self.connect() as conn:
            availability = [
                dict(r)
                for r in conn.execute(
                    "SELECT p.display_name,d.candidate_date,d.availability,d.note "
                    "FROM date_availability d JOIN participants p ON p.id=d.participant_id "
                    "WHERE d.event_id=? ORDER BY d.candidate_date,p.display_name",
                    (event["id"],),
                )
            ]
            preferences = conn.execute("SELECT * FROM event_preferences WHERE event_id=?", (event["id"],)).fetchone()
            recent = [
                dict(r)
                for r in conn.execute(
                    "SELECT display_name,message_text,timestamp FROM conversation_messages WHERE group_id=? ORDER BY id DESC LIMIT 10",
                    (group_id,),
                )
            ][::-1]
        return {
            "event": dict(event),
            "availability": availability,
            "preferences": dict(preferences) if preferences else {},
            "recent_messages": recent,
        }

    def conversation_context(self, group_id: str, message_limit: int = 12) -> dict:
        with self.connect() as conn:
            recent = [
                dict(row)
                for row in conn.execute(
                    "SELECT id,role,line_user_id,display_name,message_text,timestamp,created_at FROM conversation_messages "
                    "WHERE group_id=? ORDER BY id DESC LIMIT ?",
                    (group_id, message_limit),
                )
            ][::-1]
        return {"recent_messages": recent, "open_issues": self.open_conversation_issues(group_id)}

    def add_assistant_message(self, group_id: str, display_name: str, text: str) -> None:
        self.add_message(
            group_id,
            None,
            "assistant",
            display_name,
            text,
            f"assistant-{uuid.uuid4().hex}",
            int(datetime.now(timezone.utc).timestamp() * 1000),
            role="assistant",
        )

    def active_conversation_topics(self, group_id: str, primary_user_id: str, ttl_minutes: int) -> list[dict]:
        cutoff = datetime.now(timezone.utc).timestamp() - ttl_minutes * 60
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_topics WHERE group_id=? AND primary_user_id=? AND status='OPEN' "
                "ORDER BY last_activity_at DESC LIMIT 5",
                (group_id, primary_user_id),
            ).fetchall()
        topics = []
        for row in rows:
            item = dict(row)
            if datetime.fromisoformat(item["last_activity_at"]).timestamp() < cutoff:
                continue
            for key in ("known_facts", "open_questions", "pending_options", "expected_response_types"):
                item[key] = json.loads(item[key] or "[]")
            topics.append(item)
        return topics

    def has_active_conversation_topic(self, group_id: str, primary_user_id: str, ttl_minutes: int) -> bool:
        return bool(self.active_conversation_topics(group_id, primary_user_id, ttl_minutes))

    def upsert_conversation_topic(
        self,
        group_id: str,
        primary_user_id: str,
        *,
        topic_id: str | None,
        topic_summary: str,
        user_goal: str | None,
        known_facts: list[str],
        open_questions: list[str],
        pending_question: str | None,
        pending_question_type: str | None,
        pending_options: list[str],
        expected_response_types: list[str],
    ) -> str:
        stamp = now_iso()
        with self.connect() as conn:
            if topic_id:
                owner = conn.execute(
                    "SELECT group_id,primary_user_id FROM conversation_topics WHERE topic_id=?",
                    (topic_id,),
                ).fetchone()
                if owner and (owner["group_id"] != group_id or owner["primary_user_id"] != primary_user_id):
                    topic_id = None
            topic_id = topic_id or uuid.uuid4().hex
            conn.execute(
                "INSERT INTO conversation_topics(topic_id,group_id,primary_user_id,topic_summary,user_goal,known_facts,"
                "open_questions,pending_question,pending_question_type,pending_options,expected_response_types,status,"
                "created_at,last_activity_at,asked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?) "
                "ON CONFLICT(topic_id) DO UPDATE SET topic_summary=excluded.topic_summary,user_goal=excluded.user_goal,"
                "known_facts=excluded.known_facts,open_questions=excluded.open_questions,pending_question=excluded.pending_question,"
                "pending_question_type=excluded.pending_question_type,pending_options=excluded.pending_options,"
                "expected_response_types=excluded.expected_response_types,status='OPEN',last_activity_at=excluded.last_activity_at,"
                "asked_at=excluded.asked_at",
                (
                    topic_id,
                    group_id,
                    primary_user_id,
                    topic_summary,
                    user_goal,
                    json.dumps(known_facts, ensure_ascii=False),
                    json.dumps(open_questions, ensure_ascii=False),
                    pending_question,
                    pending_question_type,
                    json.dumps(pending_options, ensure_ascii=False),
                    json.dumps(expected_response_types, ensure_ascii=False),
                    stamp,
                    stamp,
                    stamp if pending_question else None,
                ),
            )
        return topic_id

    def close_conversation_topic(self, topic_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE conversation_topics SET status='CLOSED',last_activity_at=? WHERE topic_id=?", (now_iso(), topic_id))

    def has_open_conversation_issues(self, group_id: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute("SELECT 1 FROM conversation_issues WHERE group_id=? AND status='OPEN' LIMIT 1", (group_id,)).fetchone()
                is not None
            )

    def open_conversation_issues(self, group_id: str) -> list[dict]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM conversation_issues WHERE group_id=? AND status='OPEN' ORDER BY last_updated_at DESC LIMIT 10",
                    (group_id,),
                )
            ]

    def upsert_conversation_issue(
        self,
        group_id: str,
        fingerprint: str,
        topic: str,
        issue_type: str,
        summary: str,
        confidence: float,
        source_message_id: str,
    ) -> dict:
        stamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversation_issues(group_id,fingerprint,topic,issue_type,summary,status,confidence,"
                "source_message_id,created_at,last_updated_at) VALUES(?,?,?,?,?,'OPEN',?,?,?,?)",
                (group_id, fingerprint, topic, issue_type, summary, confidence, source_message_id, stamp, stamp),
            )
            conn.execute(
                "UPDATE conversation_issues SET confidence=max(confidence,?),last_updated_at=? "
                "WHERE group_id=? AND fingerprint=? AND status='OPEN'",
                (confidence, stamp, group_id, fingerprint),
            )
            return dict(
                conn.execute("SELECT * FROM conversation_issues WHERE group_id=? AND fingerprint=?", (group_id, fingerprint)).fetchone()
            )

    def resolve_conversation_issue(self, issue_id: int) -> None:
        stamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversation_issues SET status='RESOLVED',resolved_at=?,last_updated_at=? WHERE id=? AND status='OPEN'",
                (stamp, stamp, issue_id),
            )

    def mark_conversation_issue_notified(self, issue_id: int) -> None:
        stamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE conversation_issues SET last_notified_at=?,last_updated_at=? WHERE id=?",
                (stamp, stamp, issue_id),
            )

    def conversation_messages_since_issue(self, issue_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS count FROM conversation_messages m JOIN conversation_issues i ON i.id=? "
                "WHERE m.group_id=i.group_id AND m.id>(SELECT coalesce(max(source.id),0) FROM conversation_messages source "
                "WHERE source.group_id=i.group_id AND source.line_message_id=i.source_message_id)",
                (issue_id,),
            ).fetchone()
            return int(row["count"])

    def last_conversation_assistant_notification(self, group_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT max(last_notified_at) AS notified FROM conversation_issues WHERE group_id=?", (group_id,)).fetchone()
            return row["notified"] if row else None

    def last_conversation_assistant_topic(self, group_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT topic FROM conversation_issues WHERE group_id=? AND last_notified_at IS NOT NULL "
                "ORDER BY last_notified_at DESC LIMIT 1",
                (group_id,),
            ).fetchone()
            return row["topic"] if row else None

    def daily_api_usage(self, date_jst: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM daily_api_usage WHERE date_jst=?", (date_jst,)).fetchone()
        if row:
            return dict(row)
        return {
            "date_jst": date_jst,
            "cost_usd": 0.0,
            "cost_jpy": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "request_count": 0,
            "web_search_count": 0,
            "models": "",
            "updated_at": "",
        }

    def add_api_usage(
        self,
        date_jst: str,
        *,
        model: str,
        cost_usd: float = 0.0,
        cost_jpy: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        request_count: int = 0,
        web_search_count: int = 0,
    ) -> dict:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO daily_api_usage(date_jst,cost_usd,cost_jpy,input_tokens,output_tokens,cached_input_tokens,"
                "request_count,web_search_count,models,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(date_jst) DO UPDATE SET "
                "cost_usd=cost_usd+excluded.cost_usd,cost_jpy=cost_jpy+excluded.cost_jpy,"
                "input_tokens=input_tokens+excluded.input_tokens,output_tokens=output_tokens+excluded.output_tokens,"
                "cached_input_tokens=cached_input_tokens+excluded.cached_input_tokens,"
                "request_count=request_count+excluded.request_count,web_search_count=web_search_count+excluded.web_search_count,"
                "models=CASE WHEN instr(','||models||',', ','||excluded.models||',') > 0 THEN models "
                "WHEN models='' THEN excluded.models ELSE models||','||excluded.models END,updated_at=excluded.updated_at",
                (
                    date_jst,
                    cost_usd,
                    cost_jpy,
                    input_tokens,
                    output_tokens,
                    cached_input_tokens,
                    request_count,
                    web_search_count,
                    model,
                    now_iso(),
                ),
            )
            return dict(conn.execute("SELECT * FROM daily_api_usage WHERE date_jst=?", (date_jst,)).fetchone())

    def claim_budget_notification(self, date_jst: str, group_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO api_budget_notifications(date_jst,group_id,notified_at) VALUES(?,?,?)",
                (date_jst, group_id, now_iso()),
            )
            return cur.rowcount == 1
