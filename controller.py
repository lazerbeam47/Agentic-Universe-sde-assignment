"""Restartable HTTP controller for the outcome-first simulator."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from typing import Any

import httpx

LOG = logging.getLogger("controller")


def caller_gap_is_significant(low_n: int, low_connected: int, other_n: int,
                              other_connected: int, minimum_gap: float = 0.08) -> bool:
    """Require an 8pp effect and a conservative ~99.9% normal-approx CI separation."""
    if min(low_n, other_n) < 100:
        return False
    low_rate = low_connected / low_n
    other_rate = other_connected / other_n
    gap = other_rate - low_rate
    standard_error = math.sqrt(low_rate * (1 - low_rate) / low_n +
                               other_rate * (1 - other_rate) / other_n)
    # The least-performing ID is selected from a small family, so use a conservative
    # z threshold to limit false alarms from that selection.
    return gap >= minimum_gap and gap > 3.3 * standard_error


class Store:
    """Small durable event ledger and cursor; event_id is the deduplication key."""
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS events(event_id INTEGER PRIMARY KEY, chunk INTEGER NOT NULL, type TEXT NOT NULL,
            workflow_id TEXT, ts TEXT, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, workflow_id TEXT,
            action TEXT NOT NULL, details TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS workflow_kpis(workflow_id TEXT PRIMARY KEY, dials INTEGER NOT NULL,
            connected INTEGER NOT NULL, engaged INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS caller_stats(workflow_id TEXT NOT NULL, caller_id TEXT NOT NULL,
            dials INTEGER NOT NULL, connected INTEGER NOT NULL, PRIMARY KEY(workflow_id,caller_id));
          CREATE INDEX IF NOT EXISTS events_workflow_type ON events(workflow_id,type);
        """)
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.db.close()

    def cursor(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='chunk'").fetchone()
        return int(row[0]) if row else -1

    def save_chunk(self, chunk: int, text: str) -> int:
        inserted = 0
        with self.db:
            for line in text.splitlines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                cur = self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                    (ev["event_id"], chunk, ev.get("type", "unknown"), ev.get("workflow_id"),
                     ev.get("ts"), json.dumps(ev, separators=(",", ":"))))
                inserted += cur.rowcount
                if cur.rowcount and ev.get("type") == "call" and ev.get("workflow_id"):
                    wid = ev["workflow_id"]
                    connected, engaged = int(bool(ev.get("connected"))), int(bool(ev.get("engaged")))
                    self.db.execute("""INSERT INTO workflow_kpis VALUES(?,?,?,?)
                      ON CONFLICT(workflow_id) DO UPDATE SET dials=dials+1,
                      connected=connected+excluded.connected, engaged=engaged+excluded.engaged""",
                      (wid, 1, connected, engaged))
                    caller_id = ev.get("variant", {}).get("caller_id")
                    if caller_id is not None:
                        self.db.execute("""INSERT INTO caller_stats VALUES(?,?,1,?)
                          ON CONFLICT(workflow_id,caller_id) DO UPDATE SET dials=dials+1,
                          connected=connected+excluded.connected""", (wid, str(caller_id), connected))
            self.db.execute("INSERT INTO meta VALUES('chunk',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(max(chunk, self.cursor())),))
        return inserted

    def audit(self, ts: str, workflow: str | None, action: str, details: Any) -> None:
        with self.db:
            self.db.execute("INSERT INTO audit(ts,workflow_id,action,details) VALUES(?,?,?,?)",
                            (ts, workflow, action, json.dumps(details, sort_keys=True)))


class Controller:
    def __init__(self, url: str, store: Store, advance_minutes: int = 60):
        self.http = httpx.Client(base_url=url.rstrip("/"), timeout=httpx.Timeout(120, connect=5),
                                 headers={"User-Agent": "outcome-first-controller/1.0"})
        self.store = store
        if advance_minutes < 15 or advance_minutes % 15:
            raise ValueError("advance interval must be a positive multiple of 15 minutes")
        self.advance_minutes = advance_minutes

    def request(self, method: str, path: str, **kwargs):
        last = None
        for attempt in range(4):
            try:
                response = self.http.request(method, path, **kwargs)
                if response.status_code >= 500:
                    response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last = exc
                if attempt < 3:
                    time.sleep(0.25 * (2 ** attempt))
            except httpx.HTTPStatusError as exc:
                # The API client raises only for 5xx here; ordinary 4xx responses
                # are returned so the caller can reconcile policy/stale outcomes.
                last = exc
                if attempt < 3:
                    time.sleep(0.25 * (2 ** attempt))
        raise RuntimeError(f"simulator request failed after retries: {method} {path}: {last}")

    def get_json(self, path: str, **kwargs):
        r = self.request("GET", path, **kwargs); r.raise_for_status(); return r.json()

    def advance_once(self) -> dict:
        """Advance without blind retries; reconcile an uncertain POST from simulator state."""
        before = self.get_json("/sim/state").get("ts")
        try:
            response = self.http.post("/sim/advance", json={"minutes": self.advance_minutes})
            if response.status_code == 409:
                return {"finished": True, "new_chunks": [], "ts": before}
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            state = self.get_json("/sim/state")
            after = state.get("ts")
            if after != before:
                chunks = self.get_json("/events/chunks", params={"kind": "events", "after": self.store.cursor()})
                self.consume(chunks)
                self.store.audit(str(after), None, "advance_reconciled", {"before": before, "after": after, "error": str(exc)})
                return {"finished": state.get("finished", False), "new_chunks": [], "ts": after}
            raise RuntimeError(f"advance failed without simulator progress: {exc}") from exc

    def consume(self, chunks: list[dict], kind="events") -> None:
        for item in sorted(chunks, key=lambda c: c["chunk"]):
            number = int(item["chunk"])
            if number <= self.store.cursor():
                continue
            r = self.request("GET", f"/events/chunks/{number}", params={"kind": kind})
            r.raise_for_status()
            count = self.store.save_chunk(number, r.text)
            self.store.audit("", None, "chunk_ingested", {"kind": kind, "chunk": number, "new_events": count})

    def bootstrap(self) -> None:
        # History and event chunks share a global chunk sequence; ingest history before advancing.
        chunks = self.get_json("/events/chunks", params={"kind": "history"})
        self.consume(chunks, "history")

    def detect_caller_id_incidents(self, workflows: list[dict], ts: str) -> None:
        """Rotate only a strongly sampled caller-ID outlier, and only via explicit policy."""
        for wf in workflows:
            wid = wf.get("workflow_id", wf.get("id"))
            if not wid:
                continue
            if wf.get("mid_flight") != "allowed":
                continue
            supervisor_edited = self.store.db.execute("""SELECT 1 FROM events WHERE workflow_id=?
              AND type='config_changed' AND json_extract(payload,'$.actor')='supervisor' LIMIT 1""", (wid,)).fetchone()
            if supervisor_edited:
                # Keep human ownership after a direct supervisor intervention.
                continue
            prior_auto_change = self.store.db.execute("""SELECT 1 FROM events WHERE workflow_id=?
              AND type='config_changed' AND json_extract(payload,'$.actor')='controller_auto'
              AND json_extract(payload,'$.changed_dims') LIKE '%caller_id%' LIMIT 1""", (wid,)).fetchone()
            if prior_auto_change:
                # A previous auto-apply may have succeeded even if its response was
                # lost. Do not issue a second operation before reconciliation tooling exists.
                continue
            policy = wf.get("governance", {}).get("auto_apply", {})
            if not policy.get("enabled") or "caller_id" not in policy.get("allowed_dims", []):
                continue
            # Caller ID evidence is based on calls actually run, aggregated across the workflow.
            by_id = self.store.db.execute("SELECT caller_id,dials,connected FROM caller_stats WHERE workflow_id=?", (wid,)).fetchall()
            if len(by_id) < 2:
                continue
            total_n = sum(row[1] for row in by_id)
            if total_n < 300:
                continue
            sufficiently_sampled = [r for r in by_id if r[1] >= 100]
            if len(sufficiently_sampled) < 2:
                continue
            rates = {str(cid): (connected or 0) / n for cid, n, connected in sufficiently_sampled}
            worst = min(sufficiently_sampled, key=lambda row: rates[str(row[0])])
            marker = f"caller_rotation:{wid}:{worst[0]}"
            if self.store.db.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone():
                continue
            others = [r for r in sufficiently_sampled if r[0] != worst[0]]
            rest_n = sum(r[1] for r in others)
            rest_rate = sum(r[2] or 0 for r in others) / rest_n if rest_n else 0
            worst_rate = rates[str(worst[0])]
            if rest_n < 100 or not caller_gap_is_significant(worst[1], worst[2] or 0, rest_n,
                                                            sum(r[2] or 0 for r in others)):
                continue
            current = self.get_json(f"/workflows/{wid}/config")
            cfg = current["config"]
            allocations = cfg.get("allocations", {})
            if any("caller_id" in values for cohort, values in allocations.items() if cohort != "default"):
                # Workflow-wide performance cannot safely identify which cohort's
                # override should change, so defer rather than damage a subgroup.
                continue
            default = cfg.get("allocations", {}).get("default", {}).get("caller_id", {})
            if worst[0] not in default or len(default) <= 1:
                continue
            bad_weight = default[worst[0]]
            if bad_weight <= 0:
                continue
            # Preserve all remaining relative weights. Moving a small fraction per decision
            # keeps the change inside typical max_shift limits and supports gradual rotation.
            move = min(bad_weight, 0.1)
            recipients = [k for k, v in default.items() if k != worst[0] and v > 0]
            if not recipients:
                continue
            total = sum(default[k] for k in recipients)
            for key in recipients:
                default[key] += move * default[key] / total
            default[worst[0]] -= move
            cfg["allocations"]["default"]["caller_id"] = {k: round(v, 6) for k, v in default.items() if v > 1e-6}
            pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{wid}:{current['version']}:caller:{worst[0]}:{self.store.cursor()}"))
            payload = {"proposal_id": pid, "base_version": current["version"], "proposed_config": cfg,
                       "rationale": f"Caller ID {worst[0]} pickup rate {worst_rate:.3f} trails other sampled IDs ({rest_rate:.3f}) by at least 8 points over populations of at least 100 calls."}
            try:
                result = self.request("PUT", f"/workflows/{wid}/config", json=payload)
                if result.is_success:
                    with self.store.db:
                        self.store.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (marker, str(current["version"])))
                    self.store.audit(ts, wid, "caller_id_rotation_applied", {"proposal_id": pid, "version": result.json(),
                        "bad_caller_id": worst[0], "bad_pickup_rate": worst_rate, "comparison_pickup_rate": rest_rate,
                        "sample_size": worst[1] + rest_n, "delta_weight": move})
                else:
                    self.store.audit(ts, wid, "caller_id_rotation_denied", {"proposal_id": pid, "status": result.status_code, "response": result.text})
            except (httpx.HTTPError, RuntimeError) as exc:
                self.store.audit(ts, wid, "caller_id_rotation_error", {"proposal_id": pid, "error": str(exc)})

    def loop(self) -> None:
        state = self.get_json("/sim/state")
        workflows = self.get_json("/workflows")
        LOG.info("starting with %d workflows at %s; durable chunk cursor %d", len(workflows), state.get("ts"), self.store.cursor())
        self.store.audit(str(state.get("ts", "")), None, "controller_started", {"workflows": len(workflows)})
        if self.store.cursor() < 0:
            self.bootstrap()
        # Recover gaps by asking for every chunk after the last durable cursor.
        for kind in ("history", "events"):
            self.consume(self.get_json("/events/chunks", params={"kind": kind, "after": self.store.cursor()}), kind)
        for wf in workflows:
            wid = wf.get("workflow_id", wf.get("id"))
            if wid:
                cfg = self.get_json(f"/workflows/{wid}/config")
                self.store.audit(str(state.get("ts", "")), wid, "workflow_observed", {"version": cfg.get("version"), "primary": wf.get("objective", {}).get("primary"), "guardrail": wf.get("objective", {}).get("guardrail")})
        while True:
            body = self.advance_once()
            if body.get("finished"):
                break
            ts = body.get("ts", "")
            self.consume(body.get("new_chunks", []))
            self.detect_caller_id_incidents(workflows, ts)
            # Record separate population KPIs for each observed workflow, never combine them.
            for wf in workflows:
                wid = wf.get("workflow_id", wf.get("id"))
                if not wid: continue
                metrics = self.store.db.execute("SELECT dials,connected,engaged FROM workflow_kpis WHERE workflow_id=?", (wid,)).fetchone()
                if metrics:
                    dials, connected, engaged = metrics
                    self.store.audit(ts, wid, "population_kpis", {"dials": dials, "connected": connected or 0, "engaged": engaged or 0,
                      "pickup_rate": (connected or 0) / dials, "engagement_rate": (engaged or 0) / connected if connected else None})
            self.store.audit(ts, None, "loop_checkpoint", {"cursor": self.store.cursor(), "finished": body.get("finished", False)})
            if body.get("finished"):
                break
        final_ts = str(self.get_json("/sim/state").get("ts", ""))
        self.store.audit(final_ts, None, "controller_finished", {"last_chunk": self.store.cursor()})
        LOG.info("finished at %s; durable chunk cursor %d", final_ts, self.store.cursor())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "controller.sqlite3"))
    parser.add_argument("--advance-minutes", type=int, default=60,
                        help="simulated minutes per poll (15-minute multiple; default 60)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    Controller(args.url, Store(args.db), args.advance_minutes).loop()


if __name__ == "__main__":
    main()
