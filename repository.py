from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from .models import utc_now, Room

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
 id INTEGER PRIMARY KEY, campus TEXT NOT NULL, building TEXT NOT NULL,
 room_number TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(campus,building,room_number));
CREATE TABLE IF NOT EXISTS user_bindings (
 id INTEGER PRIMARY KEY, platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
 user_id TEXT NOT NULL, room_id INTEGER NOT NULL REFERENCES rooms(id),
 origin TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(platform_id,group_id,user_id));
CREATE TABLE IF NOT EXISTS query_runs (
 id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
 trigger_type TEXT NOT NULL CHECK(trigger_type IN ('manual','scheduled','binding','llm')),
 requested_by INTEGER REFERENCES user_bindings(id) ON DELETE SET NULL,
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL
 CHECK(status IN ('running','success','failed','interrupted')),
 error_code TEXT, parser_version TEXT, cash_balance_yuan TEXT,
 subsidy_kwh TEXT, subsidy_balance_yuan TEXT, total_balance_yuan TEXT,
 meter_reading_kwh TEXT, unit_price_yuan TEXT,
 today_kwh TEXT, today_cost_yuan TEXT, today_point TEXT,
 monthly_kwh TEXT, monthly_cost_yuan TEXT, monthly_name TEXT);
CREATE TABLE IF NOT EXISTS query_responses (
 id INTEGER PRIMARY KEY, query_id INTEGER NOT NULL REFERENCES query_runs(id),
 sequence_no INTEGER NOT NULL, stage TEXT NOT NULL, received_at TEXT NOT NULL,
 http_status INTEGER, content_type TEXT, raw_body BLOB NOT NULL,
 UNIQUE(query_id,sequence_no));
CREATE TABLE IF NOT EXISTS subscriptions (
 binding_id INTEGER PRIMARY KEY REFERENCES user_bindings(id) ON DELETE CASCADE,
 platform_id TEXT NOT NULL, group_id TEXT NOT NULL, origin TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 low_balance_threshold TEXT NOT NULL DEFAULT '10',
 low_since TEXT, last_notified_day TEXT, last_notified_query_id INTEGER REFERENCES query_runs(id));
CREATE TABLE IF NOT EXISTS capture_schedule (
 room_id INTEGER NOT NULL REFERENCES rooms(id), day TEXT NOT NULL,
 slot TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
 retry_after REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(room_id,day,slot));
CREATE TABLE IF NOT EXISTS notifications (
 id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
 platform_id TEXT NOT NULL, group_id TEXT NOT NULL, origin TEXT NOT NULL,
 day TEXT NOT NULL, query_id INTEGER NOT NULL REFERENCES query_runs(id),
 binding_ids TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 retry_after REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
 UNIQUE(room_id,origin,day));
CREATE INDEX IF NOT EXISTS idx_room_time ON query_runs(room_id,started_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_bindings_room ON user_bindings(room_id);
CREATE TABLE IF NOT EXISTS capture_plans (
 room_id INTEGER NOT NULL REFERENCES rooms(id), day TEXT NOT NULL, slot TEXT NOT NULL,
 planned_at REAL NOT NULL, deadline REAL NOT NULL,
 query_id INTEGER REFERENCES query_runs(id), PRIMARY KEY(room_id,day,slot));
CREATE INDEX IF NOT EXISTS idx_capture_plan_query ON capture_plans(query_id);
CREATE TABLE IF NOT EXISTS capture_pacing (
 id INTEGER PRIMARY KEY CHECK(id=1), available_at REAL NOT NULL);
"""


class Repository:
    def __init__(self, path, *, recover=True):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5):
            raise ValueError("Unsupported electricity database version")
        self.db.executescript(SCHEMA)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if version==1:
                self.db.execute("ALTER TABLE capture_schedule RENAME TO capture_schedule_v1")
                self.db.execute("CREATE TABLE capture_schedule(room_id INTEGER NOT NULL REFERENCES rooms(id),day TEXT NOT NULL,slot TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,retry_after REAL NOT NULL DEFAULT 0,PRIMARY KEY(room_id,day,slot))")
                self.db.execute("INSERT INTO capture_schedule(room_id,day,slot,attempts,status) SELECT room_id,day,'legacy',attempts,status FROM capture_schedule_v1")
                self.db.execute("DROP TABLE capture_schedule_v1")
            if version in (1,2): self._migrate_global_bindings()
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bindings_user ON user_bindings(user_id)")
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(subscriptions)")}
            if "reminder_type" not in columns:
                self.db.execute("ALTER TABLE subscriptions ADD COLUMN reminder_type TEXT NOT NULL DEFAULT 'balance' CHECK(reminder_type IN ('balance','days'))")
            if "days_threshold" not in columns:
                self.db.execute("ALTER TABLE subscriptions ADD COLUMN days_threshold TEXT")
            run_cols = {r[1] for r in self.db.execute("PRAGMA table_info(query_runs)")}
            for col in ("today_kwh", "today_cost_yuan", "today_point", "monthly_kwh", "monthly_cost_yuan", "monthly_name"):
                if col not in run_cols:
                    self.db.execute(f"ALTER TABLE query_runs ADD COLUMN {col} TEXT")
            self.db.execute("PRAGMA user_version=5")
            if recover:
                self.db.execute("UPDATE query_runs SET status='interrupted',error_code='INTERRUPTED',finished_at=? WHERE status='running'", (utc_now(),))
                self.db.execute("UPDATE notifications SET status='uncertain' WHERE status='sending'")
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)

    def _migrate_global_bindings(self):
        columns={r[1] for r in self.db.execute("PRAGMA table_info(subscriptions)")}
        for column in ("platform_id","group_id","origin"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE subscriptions ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        self.db.execute("UPDATE subscriptions SET platform_id=(SELECT platform_id FROM user_bindings WHERE id=binding_id),group_id=(SELECT group_id FROM user_bindings WHERE id=binding_id),origin=(SELECT origin FROM user_bindings WHERE id=binding_id) WHERE origin=''")
        users={}
        for binding in self.all("SELECT * FROM user_bindings ORDER BY updated_at DESC,id DESC"):
            users.setdefault(binding["user_id"],[]).append(binding)
        remap={}
        for bindings in users.values():
            winner=bindings[0]
            ids=[b["id"] for b in bindings]
            subs=[self.one("SELECT * FROM subscriptions WHERE binding_id=?",(b["id"],)) for b in bindings if b["room_id"]==winner["room_id"]]
            subs=[sub for sub in subs if sub]
            chosen=subs[0] if subs else None
            self.db.executemany("DELETE FROM subscriptions WHERE binding_id=?",[(i,) for i in ids])
            if chosen:
                route=chosen["origin"]
                last_days=[sub["last_notified_day"] for sub in subs if sub["last_notified_day"]]
                self.db.execute("INSERT INTO subscriptions(binding_id,platform_id,group_id,origin,enabled,low_balance_threshold,low_since,last_notified_day,last_notified_query_id) VALUES(?,?,?,?,?,?,?,?,?)",(winner["id"],chosen["platform_id"],chosen["group_id"],route,chosen["enabled"],chosen["low_balance_threshold"],chosen["low_since"],max(last_days) if last_days else None,chosen["last_notified_query_id"]))
            for binding in bindings:
                remap[binding["id"]]=winner["id"]
                if binding["id"]!=winner["id"]:
                    self.db.execute("UPDATE query_runs SET requested_by=? WHERE requested_by=?",(winner["id"],binding["id"]))
                    self.db.execute("DELETE FROM user_bindings WHERE id=?",(binding["id"],))
        # Notification destinations need an origin key: private recipients share no group ID.
        notes=self.all("SELECT * FROM notifications")
        self.db.execute("ALTER TABLE notifications RENAME TO notifications_v2")
        self.db.execute("CREATE TABLE notifications(id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL REFERENCES rooms(id),platform_id TEXT NOT NULL,group_id TEXT NOT NULL,origin TEXT NOT NULL,day TEXT NOT NULL,query_id INTEGER NOT NULL REFERENCES query_runs(id),binding_ids TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,retry_after REAL NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',UNIQUE(room_id,origin,day))")
        for note in notes:
            ids=list(dict.fromkeys(remap.get(i,i) for i in json.loads(note["binding_ids"])))
            origin=note["origin"]
            status="superseded" if note["status"]=="pending" else "uncertain" if note["status"]=="sending" else note["status"]
            self.db.execute("INSERT INTO notifications(id,room_id,platform_id,group_id,origin,day,query_id,binding_ids,attempts,retry_after,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(note["id"],note["room_id"],note["platform_id"],note["group_id"],origin,note["day"],note["query_id"],json.dumps(ids),note["attempts"],note["retry_after"],status))
        self.db.execute("DROP TABLE notifications_v2")

    @staticmethod
    def route(platform, group, user):
        return f"{platform}:GroupMessage:{group}" if group else f"{platform}:FriendMessage:{user}"

    def close(self):
        self.db.close()

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def room(self, room):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO rooms(campus,building,room_number,created_at) VALUES(?,?,?,?)", (room.campus, room.building, room.room_number, utc_now()))
        return self.one("SELECT * FROM rooms WHERE campus=? AND building=? AND room_number=?", (room.campus,room.building,room.room_number))

    def resolve_room(self, row, building):
        if row["building"]==building: return row
        canonical=self.room(Room(row["campus"],building,row["room_number"]))
        # This resolution happens before a new alias is bound to any user.
        if self.one("SELECT id FROM user_bindings WHERE room_id=?",(row["id"],)):
            raise ValueError("Unexpected bound alias")
        with self.db:
            self.db.execute("UPDATE query_runs SET room_id=? WHERE room_id=?",(canonical["id"],row["id"]))
        return canonical

    def binding(self, identity):
        return self.one("SELECT b.*,r.campus,r.building,r.room_number FROM user_bindings b JOIN rooms r ON r.id=b.room_id WHERE user_id=?", (str(identity[-1]),))

    def bind(self, identity, room_id, origin):
        with self.db:
            old = self.binding(identity)
            if old and old["room_id"] != room_id:
                self.db.execute("DELETE FROM subscriptions WHERE binding_id=?", (old["id"],))
            self.db.execute("INSERT INTO user_bindings(platform_id,group_id,user_id,room_id,origin,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET platform_id=excluded.platform_id,group_id=excluded.group_id,room_id=excluded.room_id,origin=excluded.origin,updated_at=excluded.updated_at", (*identity,room_id,origin,utc_now(),utc_now()))
        return self.binding(identity)

    def unbind(self, identity):
        with self.db:
            self.db.execute("DELETE FROM user_bindings WHERE user_id=?", (str(identity[-1]),))

    def subscribe(self, binding_id, enabled, threshold, *, platform_id=None, group_id=None, origin=None, reminder_type="balance"):
        binding=self.one("SELECT * FROM user_bindings WHERE id=?",(binding_id,))
        if not binding: raise ValueError("Missing binding")
        old=self.one("SELECT * FROM subscriptions WHERE binding_id=?",(binding_id,))
        platform=binding["platform_id"] if platform_id is None else platform_id
        group=binding["group_id"] if group_id is None else group_id
        origin=origin or self.route(platform,group,binding["user_id"])
        if old and not enabled:
            platform,group,origin=old["platform_id"],old["group_id"],old["origin"]
        if reminder_type not in {"balance", "days"}: raise ValueError("Invalid reminder type")
        if old and not enabled:
            with self.db:
                self.db.execute("UPDATE subscriptions SET enabled=0,low_since=NULL WHERE binding_id=?", (binding_id,))
            return
        amount = str(threshold) if reminder_type == "balance" else (old["low_balance_threshold"] if old else "10")
        days = str(threshold) if reminder_type == "days" else None
        with self.db:
            self.db.execute("INSERT INTO subscriptions(binding_id,platform_id,group_id,origin,enabled,low_balance_threshold,reminder_type,days_threshold) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(binding_id) DO UPDATE SET platform_id=excluded.platform_id,group_id=excluded.group_id,origin=excluded.origin,enabled=excluded.enabled,low_balance_threshold=excluded.low_balance_threshold,reminder_type=excluded.reminder_type,days_threshold=excluded.days_threshold,low_since=NULL",(binding_id,platform,group,origin,int(enabled),amount,reminder_type,days))

    def subscription(self, binding_id):
        return self.one("SELECT * FROM subscriptions WHERE binding_id=?", (binding_id,))

    def begin(self, room_id, trigger, requested_by=None):
        with self.db:
            return self.db.execute("INSERT INTO query_runs(room_id,trigger_type,requested_by,started_at,status) VALUES(?,?,?,?,'running')", (room_id,trigger,requested_by,utc_now())).lastrowid

    def response(self, query_id, seq, stage, status, content_type, body):
        with self.db:
            self.db.execute("INSERT INTO query_responses(query_id,sequence_no,stage,received_at,http_status,content_type,raw_body) VALUES(?,?,?,?,?,?,?)", (query_id,seq,stage,utc_now(),status,content_type,body))

    def finish(self, query_id, reading=None, error=None):
        with self.db:
            if reading:
                fields = reading.fields()
                self.db.execute("UPDATE query_runs SET status='success',finished_at=?,parser_version='0.1.7'," + ",".join(k+"=?" for k in fields) + " WHERE id=?", (utc_now(),*fields.values(),query_id))
            else:
                self.db.execute("UPDATE query_runs SET status='failed',finished_at=?,error_code=? WHERE id=?", (utc_now(),error,query_id))

    def latest(self, room_id, status="success"):
        return self.one("SELECT * FROM query_runs WHERE room_id=? AND status=? ORDER BY started_at DESC,id DESC LIMIT 1", (room_id,status))

    def recent_runs(self, room_id, limit=2):
        return self.all("SELECT * FROM query_runs WHERE room_id=? AND status='success' ORDER BY started_at DESC,id DESC LIMIT ?", (room_id,limit))

    def completed_runs(self, room_id, *, now=None, days=30):
        now=now or datetime.now(timezone.utc)
        cutoff=(now-timedelta(days=days)).isoformat()
        return self.all("SELECT *, (SELECT slot FROM capture_plans p WHERE p.query_id=query_runs.id LIMIT 1) AS capture_slot FROM query_runs WHERE room_id=? AND finished_at IS NOT NULL "
                        "AND julianday(finished_at)>=julianday(?) ORDER BY finished_at,id",(room_id,cutoff))

    def calendar_runs(self, room_id, *, now, start=None, end=None):
        """Calendar-only reader: no 30-day cutoff, no future observations, no writes.

        A monthly range includes the next day's end sample and one predecessor
        for detecting a balance increase at the left edge.
        """
        sql = ("SELECT q.*, (SELECT slot FROM capture_plans p WHERE p.query_id=q.id LIMIT 1) AS capture_slot "
               "FROM query_runs q WHERE q.room_id=? AND q.status='success' "
               "AND q.finished_at IS NOT NULL AND julianday(q.finished_at)<=julianday(?)")
        args = [room_id, now.isoformat()]
        if start is not None:
            sql += (" AND (julianday(q.finished_at)>=julianday(?) OR q.id=(SELECT id FROM query_runs "
                    "WHERE room_id=? AND status='success' AND julianday(finished_at)<julianday(?) "
                    "ORDER BY julianday(finished_at) DESC,id DESC LIMIT 1))")
            args += [start.isoformat(), room_id, start.isoformat()]
        if end is not None:
            sql += " AND julianday(q.finished_at)<julianday(?)"
            args.append(end.isoformat())
        return self.all(sql + " ORDER BY julianday(q.finished_at),q.id", args)

    def history(self, room_id, days):
        # Include every successful capture in Beijing calendar days, plus the
        # nearest earlier sample solely to calculate the first interval's usage.
        today = datetime.now(timezone(timedelta(hours=8))).date()
        cutoff = datetime.combine(today-timedelta(days=days-1), datetime.min.time(),tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat()
        previous = self.one("SELECT * FROM query_runs WHERE room_id=? AND status='success' AND finished_at<? ORDER BY finished_at DESC,id DESC LIMIT 1", (room_id,cutoff))
        # Keep the preceding day's captures so the overview can select its
        # 14:00 snapshot. Detail intervals still use the nearest earlier sample.
        if previous:
            local=datetime.fromisoformat(previous["finished_at"]).astimezone(timezone(timedelta(hours=8)))
            cutoff=local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
        rows = self.all("SELECT *, (SELECT slot FROM capture_plans p WHERE p.query_id=query_runs.id LIMIT 1) AS capture_slot FROM query_runs WHERE room_id=? AND status='success' AND finished_at>=? ORDER BY finished_at,id", (room_id,cutoff))
        return [{**row,"day":datetime.fromisoformat(row["finished_at"]).astimezone(timezone(timedelta(hours=8))).date().isoformat()} for row in rows]

    def monitored_rooms(self):
        return self.all("SELECT DISTINCT r.* FROM rooms r JOIN user_bindings b ON r.id=b.room_id")

    def room_subscribers(self, room_id):
        return self.all("SELECT b.id,b.user_id,b.room_id,s.platform_id,s.group_id,s.origin,s.low_balance_threshold,s.reminder_type,s.days_threshold,s.low_since,s.last_notified_day FROM user_bindings b JOIN subscriptions s ON s.binding_id=b.id WHERE b.room_id=? AND s.enabled=1", (room_id,))

    def capture_attempt(self, room_id, day, slot, retry_after):
        with self.db:
            self.db.execute("INSERT INTO capture_schedule(room_id,day,slot,attempts,status,retry_after) VALUES(?,?,?,1,'started',?) ON CONFLICT(room_id,day,slot) DO UPDATE SET attempts=attempts+1,status='started',retry_after=excluded.retry_after", (room_id,day,slot,retry_after))

    def capture_done(self, room_id, day, slot, success):
        with self.db:
            self.db.execute("UPDATE capture_schedule SET status=? WHERE room_id=? AND day=? AND slot=?", ("done" if success else "failed",room_id,day,slot))

    def reset_recovered(self, room_id, balance):
        with self.db:
            for sub in self.room_subscribers(room_id):
                if sub["reminder_type"] == "balance" and balance >= Decimal(sub["low_balance_threshold"]):
                    self.db.execute("UPDATE subscriptions SET low_since=NULL WHERE binding_id=?", (sub["id"],))

    def queue_notification(self, room_id, platform, group, origin, day, query_id, ids):
        with self.db:
            self.db.execute("INSERT INTO notifications(room_id,platform_id,group_id,origin,day,query_id,binding_ids) VALUES(?,?,?,?,?,?,?) ON CONFLICT(room_id,origin,day) DO UPDATE SET query_id=excluded.query_id,binding_ids=excluded.binding_ids,status='pending',attempts=CASE WHEN notifications.status IN ('sent','cancelled') THEN 0 ELSE notifications.attempts END,retry_after=CASE WHEN notifications.status IN ('sent','cancelled') THEN 0 ELSE notifications.retry_after END WHERE notifications.status NOT IN ('sending','uncertain')", (room_id,platform,group,origin,day,query_id,json.dumps(ids)))

    def cancel_notification(self, note_id):
        with self.db:
            self.db.execute("UPDATE notifications SET status='cancelled' WHERE id=? AND status='pending'", (note_id,))

    def pending_notifications(self, now):
        return self.all("SELECT * FROM notifications WHERE status='pending' AND attempts<3 AND retry_after<=?", (now,))

    def sending(self, note_id, now):
        with self.db:
            self.db.execute("UPDATE notifications SET status='sending',attempts=attempts+1,retry_after=? WHERE id=?", (now+300,note_id))

    def notification_result(self, note, success, retry=False):
        with self.db:
            self.db.execute("UPDATE notifications SET status=? WHERE id=?", ("sent" if success else "pending" if retry else "uncertain",note["id"]))
            if success:
                for binding_id in json.loads(note["binding_ids"]):
                    self.db.execute("UPDATE subscriptions SET last_notified_day=?,last_notified_query_id=? WHERE binding_id=? AND binding_id IN (SELECT id FROM user_bindings WHERE room_id=?)", (note["day"],note["query_id"],binding_id,note["room_id"]))
