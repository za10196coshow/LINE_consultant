import sqlite3
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
 timestamp INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_group ON conversation_messages(group_id, id DESC);
CREATE TABLE IF NOT EXISTS processed_messages (
 event_key TEXT PRIMARY KEY, message_id TEXT, processed_at TEXT NOT NULL
);
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
                fields.append("status=?"); params.append(decision.event_status.value)
            if decision.event_summary:
                fields.append("summary=?"); params.append(decision.event_summary)
            if fields:
                params.extend([now_iso(), event_id])
                conn.execute(f"UPDATE events SET {','.join(fields)},updated_at=? WHERE id=?", params)

    def add_message(self, group_id: str, event_id: int | None, user_id: str, display_name: str, text: str, message_id: str, timestamp: int):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO conversation_messages(group_id,event_id,line_user_id,display_name,message_text,line_message_id,timestamp,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (group_id, event_id, user_id, display_name, text, message_id, timestamp, now_iso()),
            )
            conn.execute("DELETE FROM conversation_messages WHERE group_id=? AND id NOT IN (SELECT id FROM conversation_messages WHERE group_id=? ORDER BY id DESC LIMIT 50)", (group_id, group_id))

    def context(self, group_id: str) -> dict:
        event = self.active_event(group_id)
        if not event:
            return {"event": None, "availability": [], "preferences": {}, "recent_messages": []}
        with self.connect() as conn:
            availability = [dict(r) for r in conn.execute(
                "SELECT p.display_name,d.candidate_date,d.availability,d.note FROM date_availability d JOIN participants p ON p.id=d.participant_id WHERE d.event_id=? ORDER BY d.candidate_date,p.display_name", (event["id"],)
            )]
            preferences = conn.execute("SELECT * FROM event_preferences WHERE event_id=?", (event["id"],)).fetchone()
            recent = [dict(r) for r in conn.execute(
                "SELECT display_name,message_text,timestamp FROM conversation_messages WHERE group_id=? ORDER BY id DESC LIMIT 10", (group_id,)
            )][::-1]
        return {"event": dict(event), "availability": availability, "preferences": dict(preferences) if preferences else {}, "recent_messages": recent}

