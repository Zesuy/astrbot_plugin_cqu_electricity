from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import asyncio
import hashlib
import json
import time
from contextlib import AsyncExitStack
from .messages import alert
from .reminders import reminder_snapshot, qualifies

BEIJING = timezone(timedelta(hours=8))
MIN_GAP_SECONDS = 10


def spread_plan(room_ids, day, slot):
    """One bounded jitter per evenly spaced stratum; stable across restarts."""
    key = lambda room_id: hashlib.sha256(f"{day}/{slot}/{room_id}".encode()).digest()
    ordered = sorted(set(room_ids), key=key)
    step = max(20.0, 300.0 / max(1, len(ordered)))
    return [(room_id, index * step + (int.from_bytes(key(room_id)[:4], "big") % 11 if len(ordered)>1 else 0))
            for index, room_id in enumerate(ordered)]


def daily_time(value):
    parts=str(value).split(":")
    if len(parts)!=2 or not all(p.isdigit() for p in parts): raise ValueError("采集时间应为 HH:MM")
    hour,minute=map(int,parts)
    if not 0<=hour<=23 or not 0<=minute<=59: raise ValueError("采集时间应为 HH:MM")
    return hour,minute


def parse_capture_times(values):
    if not isinstance(values,(list,tuple)) or not values:
        raise ValueError("采集时间应为非空列表，例如 09:00、14:00。")
    return sorted({daily_time(value) for value in values})


class Scheduler:
    def __init__(self, service, allowed, sender, *, capture_times=("09:00","14:00"), capture_window_minutes=15, reminder_enabled=True):
        self.service,self.repo,self.allowed,self.sender=service,service.repo,allowed,sender
        self.capture_times=parse_capture_times(capture_times)
        if isinstance(capture_window_minutes,bool) or not isinstance(capture_window_minutes,int) or not 1<=capture_window_minutes<=60:
            raise ValueError("采集补偿窗口应为 1～60 分钟。")
        self.capture_window_minutes=capture_window_minutes
        self.reminder_enabled=reminder_enabled
        self.next_delay=60
        self.lock=asyncio.Lock()

    async def tick(self, now=None):
        async with self.lock:
            await self._tick(now)

    def _is_client_configured(self, campus):
        client = getattr(self.service, "client", None)
        if client is None:
            return True
        configured_fn = getattr(client, "configured", None)
        if not callable(configured_fn):
            return True
        try:
            return bool(configured_fn(campus))
        except TypeError:
            return bool(configured_fn())

    def _eligible_rooms(self):
        rooms={}
        for room in self.repo.monitored_rooms():
            if not self._is_client_configured(room.get("campus")):
                continue
            bindings=self.repo.all("SELECT * FROM user_bindings WHERE room_id=?",(room["id"],))
            if any((self.allowed(b["platform_id"],b["group_id"],b["origin"]) or self.allowed(b["platform_id"],"",self.repo.route(b["platform_id"],"",b["user_id"]))) for b in bindings):
                rooms[room["id"]]=room
        return rooms

    def _plan(self, rooms, day, slot, due, now):
        plans=self.repo.all("SELECT * FROM capture_plans WHERE day=? AND slot=?",(day,slot))
        timeout=getattr(self.service.client,"timeout",40)
        if not plans:
            offsets=spread_plan(rooms,day,slot)
            horizon=max((offset for _,offset in offsets),default=0)
            window=max(self.capture_window_minutes*60,horizon+300+timeout+MIN_GAP_SECONDS)
            # Expire before the next batch/day; no catch-up requests at night.
            next_due=min([due.replace(hour=h,minute=m) for h,m in self.capture_times if (h,m)>(due.hour,due.minute)]
                         +[(due+timedelta(days=1)).replace(hour=0,minute=0)])
            deadline=min(due.timestamp()+window,next_due.timestamp())
            if now.timestamp()>=deadline:return
            with self.repo.db:
                self.repo.db.executemany("INSERT OR IGNORE INTO capture_plans(room_id,day,slot,planned_at,deadline) VALUES(?,?,?,?,?)",
                                        [(rid,day,slot,due.timestamp()+offset,deadline) for rid,offset in offsets])
        elif now.timestamp()<max(p["deadline"] for p in plans):
            # New bindings join the tail without moving anyone's persisted slot.
            known={p["room_id"] for p in plans}
            tail=max(p["planned_at"] for p in plans)
            deadline=max(p["deadline"] for p in plans)
            with self.repo.db:
                for rid in sorted(set(rooms)-known):
                    tail=max(tail+20,now.timestamp())
                    if tail>=deadline:break
                    self.repo.db.execute("INSERT INTO capture_plans(room_id,day,slot,planned_at,deadline) VALUES(?,?,?,?,?)",
                                         (rid,day,slot,tail,deadline))

    def _pending(self, day, rooms, now):
        pending=[]
        for plan in self.repo.all("SELECT * FROM capture_plans WHERE day=?",(day,)):
            if plan["room_id"] not in rooms or plan["slot"] not in {f"{h:02d}:{m:02d}" for h,m in self.capture_times} or now>=plan["deadline"]:continue
            state=self.repo.one("SELECT * FROM capture_schedule WHERE room_id=? AND day=? AND slot=?",(plan["room_id"],day,plan["slot"]))
            if state and (state["status"]=="done" or state["attempts"]>=2):continue
            pending.append((max(plan["planned_at"],state["retry_after"] if state else 0),plan))
        return sorted(pending,key=lambda item:(item[0],item[1]["slot"],item[1]["room_id"]))

    async def _tick(self, now=None):
        fixed_now=now
        now=(now or datetime.now(BEIJING)).astimezone(BEIJING)
        day=now.date().isoformat()
        self.next_delay=60
        rooms=self._eligible_rooms()
        if not rooms:
            return
        for hour,minute in self.capture_times:
            due=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
            slot=f"{hour:02d}:{minute:02d}"
            horizon=max((offset for _,offset in spread_plan(rooms,day,slot)),default=0)
            if 0<=(now-due).total_seconds()<max(self.capture_window_minutes*60,horizon+350):
                self._plan(rooms,day,slot,due,now)
        pending=self._pending(day,rooms,now.timestamp())
        pacing=self.repo.one("SELECT available_at FROM capture_pacing WHERE id=1")
        available=pacing["available_at"] if pacing else 0
        if pending and max(pending[0][0],available)<=now.timestamp():
            _,plan=pending[0]
            room=rooms[plan["room_id"]]
            hour,minute=daily_time(plan["slot"])
            due=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
            self.repo.capture_attempt(room["id"],day,plan["slot"],now.timestamp()+300)
            latest=self.repo.latest(room["id"])
            fresh=latest and datetime.fromisoformat(latest["started_at"])>=due.astimezone(timezone.utc)
            if fresh:result=self.service.result(room,True)
            else:
                # Persist a conservative lease before I/O, then 10s from completion.
                with self.repo.db:
                    self.repo.db.execute("INSERT OR REPLACE INTO capture_pacing VALUES(1,?)",(now.timestamp()+getattr(self.service.client,"timeout",40)+MIN_GAP_SECONDS,))
                try:result=await self.service.query(room,force=True,trigger="scheduled")
                finally:
                    finished=fixed_now or datetime.now(BEIJING)
                    available=finished.timestamp()+MIN_GAP_SECONDS
                    with self.repo.db:self.repo.db.execute("INSERT OR REPLACE INTO capture_pacing VALUES(1,?)",(available,))
            if not result["ok"]:
                retry_at=(fixed_now or datetime.now(BEIJING)).timestamp()+300
                with self.repo.db:
                    self.repo.db.execute("UPDATE capture_schedule SET retry_after=? WHERE room_id=? AND day=? AND slot=?",(retry_at,plan["room_id"],day,plan["slot"]))
            self.repo.capture_done(plan["room_id"],day,plan["slot"],result["ok"])
            captured=self.repo.latest(room["id"])
            if result["ok"] and captured:
                with self.repo.db:
                    self.repo.db.execute("UPDATE capture_plans SET query_id=? WHERE room_id=? AND day=? AND slot=?",(captured["id"],plan["room_id"],day,plan["slot"]))
        moment=fixed_now or datetime.now(BEIJING)
        pending=self._pending(day,rooms,moment.timestamp())
        if pending:self.next_delay=max(1,min(60,max(pending[0][0],available)-moment.timestamp()))
        else:
            # Wake at the batch boundary rather than up to 59s after it.
            future=[moment.replace(hour=h,minute=m,second=0,microsecond=0) for h,m in self.capture_times]
            future=[due if due>moment else due+timedelta(days=1) for due in future]
            self.next_delay=max(1,min(60,(min(future)-moment).total_seconds()))
        if self.reminder_enabled:
            for room in self.repo.monitored_rooms(): self.queue_low_balance(room,day,now=moment)
            await self.deliver(now=moment if fixed_now else None)

    def queue_low_balance(self, room, day, *, now=None):
        now=now or datetime.now(BEIJING)
        if now.astimezone(BEIJING).date().isoformat()!=day: return
        subscribers=self.repo.room_subscribers(room["id"])
        if not subscribers: return
        state=reminder_snapshot(self.repo,room,now=now,window_minutes=self.capture_window_minutes)
        if state is None: return
        self.repo.reset_recovered(room["id"],Decimal(state["actual_balance"]))
        groups={}
        for sub in subscribers:
            if not qualifies(sub,state) or sub["last_notified_day"]==day: continue
            if not self.allowed(sub["platform_id"],sub["group_id"],sub["origin"]): continue
            groups.setdefault((sub["platform_id"],sub["group_id"],sub["origin"]),{"origin":sub["origin"],"ids":[]})["ids"].append(sub["id"])
        for (platform,group,origin),group_data in groups.items():
            self.repo.queue_notification(room["id"],platform,group,group_data["origin"],day,state["query_id"],group_data["ids"])

    async def deliver(self, *, now=None):
        if not self.reminder_enabled: return
        for note in self.repo.pending_notifications(now.timestamp() if now else time.time()):
            if not self.allowed(note["platform_id"],note["group_id"],note["origin"]):
                self.repo.cancel_notification(note["id"])
                continue
            room=self.repo.one("SELECT * FROM rooms WHERE id=?",(note["room_id"],))
            # Hold the same caller locks used by commands/tools across sending.
            # Re-read after acquiring: a binding or setting may have changed.
            candidates=[s for s in self.repo.room_subscribers(room["id"])
                        if s["origin"]==note["origin"] and s["id"] in json.loads(note["binding_ids"])]
            async with AsyncExitStack() as locks:
                for user in sorted({s["user_id"] for s in candidates}):
                    await locks.enter_async_context(self.service.identity_lock((None,None,user)))
                moment=now or datetime.now(BEIJING)
                if moment.astimezone(BEIJING).date().isoformat()!=note["day"]:
                    self.repo.cancel_notification(note["id"]);continue
                state=reminder_snapshot(self.repo,room,now=moment,window_minutes=self.capture_window_minutes)
                eligible=[s for s in self.repo.room_subscribers(room["id"])
                          if s["platform_id"]==note["platform_id"] and s["group_id"]==note["group_id"]
                          and s["origin"]==note["origin"] and (s["id"],s["user_id"]) in {(c["id"],c["user_id"]) for c in candidates}
                          and s["last_notified_day"]!=note["day"] and qualifies(s,state)]
                if not eligible or not self.allowed(note["platform_id"],note["group_id"],note["origin"]):
                    self.repo.cancel_notification(note["id"]);continue
                text=alert(room,state,include_days=any(s["reminder_type"]=="days" for s in eligible))
                note={**note,"binding_ids":json.dumps([s["id"] for s in eligible]),"query_id":state["query_id"]}
                with self.repo.db:
                    self.repo.db.execute("UPDATE notifications SET binding_ids=?,query_id=? WHERE id=?",(note["binding_ids"],note["query_id"],note["id"]))
                self.repo.sending(note["id"],moment.timestamp())
                try:
                    sent=await self.sender(note["origin"],[s["user_id"] for s in eligible],text)
                except Exception:
                    # Delivery may already have occurred. Never blindly replay uncertain sends.
                    self.repo.notification_result(note,False)
                else:
                    self.repo.notification_result(note,sent,retry=not sent)
