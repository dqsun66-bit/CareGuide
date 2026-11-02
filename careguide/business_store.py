"""BusinessStore lookup and guarded-write helpers for CareGuide.

SQLite stores local consultation handoffs, never hospital bookings or dispatches.
Creation is transactional; open records are reused for the same actor/session.
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager


def _as_utc(value, name):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f'{name} must be timezone-aware')
    return value.astimezone(timezone.utc)


class BusinessStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS handoffs (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, conversation TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS open_handoff
                ON handoffs(actor, conversation) WHERE status IN ('open', 'in_progress');
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS map_parking_queries (
                    id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    conversation TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS map_parking_query_scope
                ON map_parking_queries(actor, conversation, expires_at);
            ''')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_handoff(self, actor, conversation, request_id, message, evidence, tool_history, urgency):
        with self._lock, self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                "SELECT payload FROM handoffs WHERE actor=? AND conversation=? AND status IN ('open','in_progress')",
                (actor, conversation)).fetchone()
            if existing:
                return json.loads(existing['payload']), True
            timestamp = datetime.now(timezone.utc).isoformat()
            item = {'id': 'CG-'+uuid.uuid4().hex[:12].upper(), 'actor_user_id': actor,
                    'conversation_id': conversation, 'request_id': request_id, 'status': 'open',
                    'summary': message, 'evidence': evidence, 'tool_history': tool_history,
                    'urgency': urgency, 'created_at': timestamp, 'updated_at': timestamp,
                    'events': [{'status': 'open', 'at': timestamp}],
                    'destination': 'local_consultation_queue', 'hospital_dispatch_performed': False}
            connection.execute('INSERT INTO handoffs VALUES (?,?,?,?,?)',
                (item['id'], actor, conversation, 'open', json.dumps(item, ensure_ascii=False)))
            return item, False

    def get_handoff(self, identity, actor):
        with self.connect() as connection:
            item = connection.execute('SELECT payload FROM handoffs WHERE id=? AND actor=?', (identity, actor)).fetchone()
            return json.loads(item['payload']) if item else None

    def list_handoffs(self, actor):
        with self.connect() as connection:
            return [json.loads(item['payload']) for item in connection.execute(
                'SELECT payload FROM handoffs WHERE actor=? ORDER BY rowid DESC LIMIT 100', (actor,))]

    def transition_handoff(self, identity, actor, status, note=''):
        transitions = {'open': {'in_progress', 'cancelled'}, 'in_progress': {'resolved', 'cancelled'}}
        with self._lock, self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT payload FROM handoffs WHERE id=? AND actor=?', (identity, actor)).fetchone()
            if row is None:
                raise LookupError('Consultation record not found')
            item = json.loads(row['payload'])
            if status not in transitions.get(item['status'], set()):
                raise ValueError('Invalid consultation status transition')
            item['status'] = status
            item['updated_at'] = datetime.now(timezone.utc).isoformat()
            item['events'].append({'status': status, 'at': item['updated_at'], 'note': note})
            connection.execute('UPDATE handoffs SET status=?,payload=? WHERE id=?',
                (status, json.dumps(item, ensure_ascii=False), identity))
            return item

    def create_map_parking_query(
            self, actor, conversation, query, city, hospitals, *, expires_at, now=None):
        now = _as_utc(now if now is not None else datetime.now(timezone.utc), 'now')
        expires_at = _as_utc(expires_at, 'expires_at')
        query_id = 'MAP-' + uuid.uuid4().hex
        item = {
            'query_id': query_id,
            'candidate_version': 1,
            'actor': actor,
            'conversation': conversation,
            'query': query,
            'city': city,
            'hospitals': hospitals,
            'created_at': now.isoformat(),
            'expires_at': expires_at.isoformat(),
        }
        with self._lock, self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                'DELETE FROM map_parking_queries WHERE expires_at<=?',
                (now.isoformat(),),
            )
            connection.execute(
                'INSERT INTO map_parking_queries VALUES (?,?,?,?,?,?)',
                (query_id, actor, conversation, 1, item['expires_at'],
                 json.dumps(item, ensure_ascii=False)),
            )
        return item

    def select_map_hospital(
            self, query_id, actor, conversation, version, hospital_id, *, now=None):
        now = _as_utc(now if now is not None else datetime.now(timezone.utc), 'now')
        with self.connect() as connection:
            row = connection.execute(
                'SELECT actor, conversation, version, expires_at, payload '
                'FROM map_parking_queries WHERE id=?',
                (query_id,),
            ).fetchone()
        if row is None:
            raise LookupError('Map parking query not found')
        if row['actor'] != actor or row['conversation'] != conversation:
            raise PermissionError('Map parking query belongs to another scope')
        if row['version'] != version:
            raise ValueError('Map parking query version mismatch')
        expires_at = _as_utc(
            datetime.fromisoformat(row['expires_at']), 'Stored expires_at')
        if expires_at <= now:
            raise TimeoutError('Map parking query expired')

        item = json.loads(row['payload'])
        for hospital in item['hospitals']:
            if hospital.get('hospital_id') == hospital_id:
                return hospital
        raise ValueError('Hospital is not a candidate for this query')
