"""Opt-in HTTPS outbox with explicit acknowledgement and idempotent item IDs."""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from datetime import timedelta

from .models import stamp, utcnow
from .storage import Store
from .workers import finish_io


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Sync redirects prohibited")


def post_batch(url: str, body: dict) -> dict:
    token = os.environ.get("GTBMS_SYNC_TOKEN")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=json.dumps(body, allow_nan=False).encode(), headers=headers, method="POST")
    with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
        payload = response.read(1_048_577)
        if len(payload) > 1_048_576:
            raise ValueError("Sync response too large")
        return json.loads(payload)


class SyncWorker:
    def __init__(self, store: Store, sender=post_batch):
        self.store, self.config, self.sender = store, store.config, sender
        self.state = "disabled" if not self.config.sync_enabled else "waiting"
        self.last_success_at = None

    def once(self):
        if not self.config.sync_enabled:
            self.state = "disabled"
            return
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM outbox WHERE next_attempt<=? AND (? OR kind!='gps') ORDER BY next_attempt LIMIT 100",
                                         (stamp(), int(self.config.sync_include_gps))).fetchall()
        if not rows:
            self.state = "idle"
            return
        batch = [{"id": r["id"], "kind": r["kind"], "payload": json.loads(r["payload"])} for r in rows]
        try:
            ack = self.sender(self.config.sync_url, {"schema_version": 1, "items": batch})
            ids = ack.get("acknowledged_ids")
            if not isinstance(ids, list) or not all(isinstance(v, str) for v in ids) or not set(ids) <= {r["id"] for r in rows}:
                raise ValueError("Invalid acknowledgement")
            with self.store.transaction() as db:
                db.executemany("DELETE FROM outbox WHERE id=?", [(v,) for v in ids])
                for r in rows:
                    if r["id"] not in ids:
                        self.reschedule(db, r)
            self.state = "online"
            self.last_success_at = stamp()
        except Exception as exc:
            with self.store.transaction() as db:
                for r in rows:
                    self.reschedule(db, r)
            self.state = "offline"
            self.store.event("sync_error", type(exc).__name__)

    @staticmethod
    def reschedule(db, row):
        seconds = min(3600, 15*2**min(row["attempts"], 8))
        db.execute("UPDATE outbox SET attempts=attempts+1,next_attempt=? WHERE id=?",
                   (stamp(utcnow()+timedelta(seconds=seconds)), row["id"]))

    async def run(self):
        while True:
            await finish_io(self.once)
            await asyncio.sleep(15)
