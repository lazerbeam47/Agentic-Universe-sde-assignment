"""Restartable HTTP controller for the outcome-first simulator."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
from statistics import NormalDist
import time
import uuid
import zlib
from datetime import datetime, timedelta
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
    """Compressed immutable event archive, normalized index and transactional aggregates."""
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA cache_size=-16384")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, workflow_id TEXT,
            action TEXT NOT NULL, details TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS event_chunks(chunk INTEGER PRIMARY KEY, kind TEXT NOT NULL,
            event_count INTEGER NOT NULL, raw_bytes INTEGER NOT NULL, compressed BLOB NOT NULL,
            first_event_ts TEXT,last_event_ts TEXT);
          CREATE TABLE IF NOT EXISTS event_ids(event_id INTEGER PRIMARY KEY,chunk INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS event_index(
            event_id INTEGER PRIMARY KEY, chunk INTEGER NOT NULL, type TEXT NOT NULL, workflow_id TEXT,
            ts TEXT, actor TEXT, approval_id TEXT, proposal_id TEXT, status TEXT, version INTEGER, changed_dims TEXT);
          CREATE TABLE IF NOT EXISTS workflow_kpis(workflow_id TEXT PRIMARY KEY, dials INTEGER NOT NULL,
            connected INTEGER NOT NULL, engaged INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS variant_hourly(workflow_id TEXT, dimension TEXT, cohort_id TEXT,
            hour_key TEXT, variant TEXT, dials INTEGER NOT NULL, connected INTEGER NOT NULL,
            engaged INTEGER NOT NULL, qa_n INTEGER NOT NULL, hallucinations INTEGER NOT NULL,
            PRIMARY KEY(workflow_id,dimension,cohort_id,hour_key,variant));
          CREATE TABLE IF NOT EXISTS proposals(proposal_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
            approval_id TEXT, status TEXT NOT NULL, base_version INTEGER NOT NULL, action TEXT NOT NULL,
            target TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS experiments(experiment_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
            dimension TEXT NOT NULL, cohort_id TEXT NOT NULL, control TEXT NOT NULL, candidate TEXT NOT NULL,
            allocation REAL NOT NULL, base_version INTEGER NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT, decision TEXT);
          CREATE TABLE IF NOT EXISTS incidents(incident_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, first_seen TEXT NOT NULL, details TEXT NOT NULL,
            updated_at TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS event_index_workflow_type_ts ON event_index(workflow_id,type,ts);
          CREATE INDEX IF NOT EXISTS event_index_proposal ON event_index(proposal_id);
          CREATE INDEX IF NOT EXISTS proposals_status ON proposals(status,workflow_id);
          CREATE INDEX IF NOT EXISTS variant_window ON variant_hourly(workflow_id,dimension,hour_key);
        """)
        self.db.commit()
        cols={r[1] for r in self.db.execute("PRAGMA table_info(event_chunks)")}
        with self.db:
            if "first_event_ts" not in cols: self.db.execute("ALTER TABLE event_chunks ADD COLUMN first_event_ts TEXT")
            if "last_event_ts" not in cols: self.db.execute("ALTER TABLE event_chunks ADD COLUMN last_event_ts TEXT")
        self._migrate_wide_event_index()
        self._migrate_legacy_events()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.db.close()

    def cursor(self) -> int:
        row = self.db.execute("SELECT value FROM meta WHERE key='chunk'").fetchone()
        return int(row[0]) if row else -1

    @staticmethod
    def _hour(ts: str) -> str:
        if isinstance(ts,datetime): return ts.strftime("%Y-%m-%dT%H:00")
        return ts[:13] + ":00" if len(ts) >= 13 else "unknown"

    def _index_event(self, chunk: int, ev: dict) -> int:
        variant, qa = ev.get("variant") or {}, ev.get("qa") or {}
        changed = ev.get("changed_dims")
        event_id=ev.get("event_id")
        if event_id is None: raise ValueError("simulator event missing required event_id")
        cur=self.db.execute("INSERT OR IGNORE INTO event_ids VALUES(?,?)",(event_id,chunk))
        if not cur.rowcount: return 0
        event_type=ev.get("type","unknown")
        if event_type in ("config_changed","approval_decided","campaign_created"):
            self.db.execute("""INSERT OR IGNORE INTO event_index VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (event_id,chunk,event_type,ev.get("workflow_id"),ev.get("ts"),ev.get("actor"),ev.get("approval_id"),
               ev.get("proposal_id"),ev.get("status"),ev.get("version"),
               json.dumps(changed,separators=(",",":")) if changed is not None else None))
        if event_type == "call" and ev.get("workflow_id"):
            self._aggregate_call(ev, variant, qa)
        return cur.rowcount

    def _aggregate_call(self, ev: dict, variant: dict, qa: dict) -> None:
        wid = ev["workflow_id"]
        connected, engaged = int(bool(ev.get("connected"))), int(bool(ev.get("engaged")))
        hour = self._hour(ev.get("ts", ""))
        self.db.execute("""INSERT INTO workflow_kpis VALUES(?,?,?,?)
          ON CONFLICT(workflow_id) DO UPDATE SET dials=dials+1,
          connected=connected+excluded.connected,engaged=engaged+excluded.engaged""",
          (wid, 1, connected, engaged))
        cid = variant.get("caller_id")
        cohort = ev.get("cohort_id") or "default"
        dimensions = {"voice":variant.get("voice"),"welcome_message":variant.get("welcome_message"),
                      "prompt_version":variant.get("prompt_version"),"caller_id":cid}
        for dimension, value in dimensions.items():
            if value is None: continue
            qa_n = int(connected and "hallucination_flag" in qa)
            flags = int(connected and bool(qa.get("hallucination_flag")))
            self.db.execute("""INSERT INTO variant_hourly VALUES(?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(workflow_id,dimension,cohort_id,hour_key,variant) DO UPDATE SET
              dials=dials+1,connected=connected+excluded.connected,engaged=engaged+excluded.engaged,
              qa_n=qa_n+excluded.qa_n,hallucinations=hallucinations+excluded.hallucinations""",
              (wid,dimension,cohort,hour,str(value),1,connected,engaged,qa_n,flags))

    def save_chunk_lines(self, chunk: int, kind: str, lines) -> dict:
        compressor = zlib.compressobj(level=6)
        compressed_parts, count, raw_bytes, indexed = [], 0, 0, 0
        first_ts=last_ts=None
        with self.db:
            if self.db.execute("SELECT 1 FROM event_chunks WHERE chunk=?", (chunk,)).fetchone():
                return {"events":0,"indexed":0,"raw_bytes":0,"compressed_bytes":0,"duplicate_chunk":True}
            for line in lines:
                if isinstance(line, bytes): line = line.decode("utf-8")
                if not line.strip(): continue
                raw = (line+"\n").encode("utf-8")
                raw_bytes += len(raw)
                output = compressor.compress(raw)
                if output: compressed_parts.append(output)
                ev=json.loads(line)
                if ev.get("ts"):
                    first_ts=first_ts or ev["ts"]
                    last_ts=ev["ts"]
                indexed += self._index_event(chunk, ev)
                count += 1
            compressed_parts.append(compressor.flush())
            blob = b"".join(compressed_parts)
            self.db.execute("INSERT INTO event_chunks(chunk,kind,event_count,raw_bytes,compressed,first_event_ts,last_event_ts) VALUES(?,?,?,?,?,?,?)",
                            (chunk,kind,count,raw_bytes,blob,first_ts,last_ts))
            self.db.execute("INSERT INTO meta VALUES('chunk',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (str(max(chunk,self.cursor())),))
        return {"events":count,"indexed":indexed,"raw_bytes":raw_bytes,"compressed_bytes":len(blob),
                "first_event_ts":first_ts,"last_event_ts":last_ts}

    def save_chunk(self, chunk: int, text: str, kind="events") -> dict:
        return self.save_chunk_lines(chunk,kind,iter(text.splitlines()))

    def _migrate_legacy_events(self) -> None:
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "events" not in tables: return
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        if not {"chunk","payload"}.issubset(columns): return
        with self.db:
            self.db.execute("DELETE FROM workflow_kpis")
            self.db.execute("DROP TABLE IF EXISTS caller_stats")
        source = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            chunks = [r[0] for r in source.execute("SELECT DISTINCT chunk FROM events ORDER BY chunk")]
            for chunk in chunks:
                if self.db.execute("SELECT 1 FROM event_chunks WHERE chunk=?", (chunk,)).fetchone(): continue
                rows = source.execute("SELECT payload FROM events WHERE chunk=? ORDER BY event_id", (chunk,))
                self.save_chunk_lines(chunk,"legacy",(r[0] for r in rows))
        finally:
            source.close()
        with self.db: self.db.execute("DROP TABLE events")

    def _migrate_wide_event_index(self):
        cols={r[1] for r in self.db.execute("PRAGMA table_info(event_index)")}
        if "features" not in cols: return
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS event_ids(event_id INTEGER PRIMARY KEY,chunk INTEGER NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO event_ids SELECT event_id,chunk FROM event_index")
            self.db.execute("ALTER TABLE event_index RENAME TO event_index_wide")
            self.db.execute("""CREATE TABLE event_index(event_id INTEGER PRIMARY KEY,chunk INTEGER NOT NULL,type TEXT NOT NULL,
              workflow_id TEXT,ts TEXT,actor TEXT,approval_id TEXT,proposal_id TEXT,status TEXT,version INTEGER,changed_dims TEXT)""")
            self.db.execute("""INSERT INTO event_index SELECT event_id,chunk,type,workflow_id,ts,actor,approval_id,proposal_id,
              status,version,changed_dims FROM event_index_wide WHERE type IN ('config_changed','approval_decided','campaign_created')""")
            self.db.execute("DROP TABLE event_index_wide")
            self.db.execute("DROP TABLE IF EXISTS caller_hourly")
            self.db.execute("DROP TABLE IF EXISTS caller_stats")
        self.db.execute("CREATE INDEX IF NOT EXISTS event_index_workflow_type_ts ON event_index(workflow_id,type,ts)")
        self.db.execute("CREATE INDEX IF NOT EXISTS event_index_proposal ON event_index(proposal_id)")
        self.db.commit()

    def storage_metrics(self) -> dict:
        pages = self.db.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        raw, compressed, chunks, events, max_events, max_raw = self.db.execute("""SELECT COALESCE(SUM(raw_bytes),0),
          COALESCE(SUM(length(compressed)),0),COUNT(*),COALESCE(SUM(event_count),0),COALESCE(MAX(event_count),0),
          COALESCE(MAX(raw_bytes),0) FROM event_chunks""").fetchone()
        try: tables={name:size for name,size in self.db.execute("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name")}
        except sqlite3.OperationalError: tables={}
        db_bytes=pages*page_size
        return {"db_page_bytes":db_bytes,"raw_event_bytes":raw,"compressed_chunk_bytes":compressed,
                "event_index_and_overhead_bytes":max(0,db_bytes-compressed),
                "bytes_per_million_events":db_bytes*1_000_000/events if events else 0,
                "raw_bytes_per_million_events":raw*1_000_000/events if events else 0,
                "chunks":chunks,"events":events,"max_events_per_chunk":max_events,
                "max_raw_bytes_per_chunk":max_raw,"table_bytes":tables}

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

    def consume(self, chunks: list[dict], kind="events") -> list[dict]:
        metrics=[]
        for item in sorted(chunks, key=lambda c: c["chunk"]):
            number = int(item["chunk"])
            if number <= self.store.cursor():
                continue
            with self.http.stream("GET", f"/events/chunks/{number}", params={"kind": kind}) as response:
                response.raise_for_status()
                chunk_metrics = self.store.save_chunk_lines(number, kind, response.iter_lines())
            metrics.append(chunk_metrics)
            self.store.audit(chunk_metrics.get("last_event_ts", ""), None, "chunk_ingested", {"kind": kind, "chunk": number, **chunk_metrics})
        return metrics

    def bootstrap(self) -> None:
        # History and event chunks share a global chunk sequence; ingest history before advancing.
        chunks = self.get_json("/events/chunks", params={"kind": "history"})
        self.consume(chunks, "history")

    @staticmethod
    def _changed_dimensions(old: dict, new: dict) -> set[str]:
        changed = set()
        if old.get("prompt_version") != new.get("prompt_version"): changed.add("prompt_version")
        if old.get("schedule") != new.get("schedule"): changed.add("schedule")
        if old.get("cohorts") != new.get("cohorts"): changed.add("cohorts")
        dimensions = {d for group in (old.get("allocations",{}),new.get("allocations",{}))
                      for values in group.values() for d in values}
        for dim in dimensions:
            if any(old.get("allocations",{}).get(g,{}).get(dim) != new.get("allocations",{}).get(g,{}).get(dim)
                   for g in set(old.get("allocations",{})) | set(new.get("allocations",{}))):
                changed.add(dim)
        return changed

    @staticmethod
    def _normalized(weights: dict | None) -> dict:
        weights = weights or {}
        total = sum(max(0,float(v)) for v in weights.values())
        return {k:max(0,float(v))/total for k,v in weights.items()} if total else {}

    @staticmethod
    def _project_kpis(distribution: dict, samples: dict[str,tuple[int,int,int]]) -> tuple[float,float]:
        dist=Controller._normalized(distribution)
        pickup=sum(weight*samples[cid][1]/samples[cid][0] for cid,weight in dist.items() if cid in samples and samples[cid][0])
        engaged_per_dial=sum(weight*samples[cid][2]/samples[cid][0] for cid,weight in dist.items() if cid in samples and samples[cid][0])
        engagement=engaged_per_dial/pickup if pickup else 0.0
        return pickup,engagement

    def _auto_apply_permitted(self, wf: dict, old: dict, new: dict) -> tuple[bool,str]:
        policy = wf.get("governance",{}).get("auto_apply",{})
        dims = self._changed_dimensions(old,new)
        if not policy.get("enabled") or not dims or not dims.issubset(set(policy.get("allowed_dims",[]))):
            return False,"dimension not permitted by auto-apply policy"
        limit = float(policy.get("max_shift",0))
        for dimension in dims & {"voice","welcome_message","caller_id"}:
            groups = set(old.get("allocations",{})) | set(new.get("allocations",{}))
            for group in groups:
                before = old.get("allocations",{}).get(group,{}).get(dimension)
                after = new.get("allocations",{}).get(group,{}).get(dimension)
                if before is None and group != "default": before=old.get("allocations",{}).get("default",{}).get(dimension)
                if after is None and group != "default": after=new.get("allocations",{}).get("default",{}).get(dimension)
                a,b=self._normalized(before),self._normalized(after)
                tv=0.5*sum(abs(a.get(k,0)-b.get(k,0)) for k in set(a)|set(b))
                if tv > limit + 1e-9:
                    return False,f"{dimension} total-variation shift {tv:.4f} exceeds {limit:.4f}"
        return True,"permitted"

    def submit_change(self, wf: dict, current: dict, proposed: dict, action: str, target: str,
                      rationale: str, ts: str, evidence: dict | None = None,
                      summary: str | None = None, dry_run: dict | None = None,
                      impact_score: float = 0.25, affected_leads: int | None = None) -> str | None:
        wid = wf.get("workflow_id",wf.get("id")); base=current["version"]
        if wf.get("mid_flight") != "allowed" and any(d in self._changed_dimensions(current["config"],proposed)
            for d in {"voice","welcome_message","caller_id","cohorts"}):
            self.store.audit(ts,wid,"change_deferred_campaign_locked",{"action":action,"target":target})
            return None
        pid=str(uuid.uuid5(uuid.NAMESPACE_URL,f"{wid}:{base}:{action}:{target}"))
        existing=self.store.db.execute("SELECT status FROM proposals WHERE proposal_id=?",(pid,)).fetchone()
        if existing:
            return pid
        dims=self._changed_dimensions(current["config"],proposed)
        diff={}
        for d in sorted(dims):
            if d in {"prompt_version","schedule","cohorts"}:
                diff[d]={"before":current["config"].get(d),"after":proposed.get(d)}
            else:
                groups=set(current["config"].get("allocations",{}))|set(proposed.get("allocations",{}))
                diff[d]={g:{"before":current["config"].get("allocations",{}).get(g,{}).get(d),
                            "after":proposed.get("allocations",{}).get(g,{}).get(d)} for g in sorted(groups)
                          if current["config"].get("allocations",{}).get(g,{}).get(d)!=proposed.get("allocations",{}).get(g,{}).get(d)}
        auto,reason=self._auto_apply_permitted(wf,current["config"],proposed)
        action_name=action[5:] if action.startswith("auto:") else action
        proposal_action=("auto:"+action_name) if auto else action_name
        body={"proposal_id":pid,"workflow_id":wid,"base_version":base,"proposed_config":proposed,
              "rationale":rationale}
        if auto:
            body["_mode"]="auto"
        else:
            body.update({"change_summary":summary or f"{action}: {target}","diff":diff,
                         "affected_leads":max(0,int(affected_leads if affected_leads is not None else wf.get("daily_batch",{}).get("size",0))),
                         "dry_run":dry_run or {"method":"no historical replay available","result":"not estimated"},
                         "impact_score":max(0,min(1,float(impact_score))),"rationale":rationale})
            governance=wf.get("governance",{})
            if governance.get("proposal_ttl_hours"):
                body["expires_in_hours"]=int(governance["proposal_ttl_hours"])
            if evidence: body["evidence"]=evidence
        stored={k:v for k,v in body.items() if k!="_mode"}
        with self.store.db:
            self.store.db.execute("""INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (pid,wid,None,"prepared",base,proposal_action,target,json.dumps(stored,sort_keys=True),ts,ts))
        self.store.audit(ts,wid,"proposal_prepared",{"proposal_id":pid,"action":action,"target":target,
            "dimensions":sorted(dims),"mode":"auto" if auto else "approval","policy_check":reason})
        if auto:
            self._apply_auto(pid,wid,base,action,target,stored,ts)
        else:
            cutoff=(datetime.fromisoformat(ts)-timedelta(hours=24)).isoformat()
            sent=self.store.db.execute("""SELECT COUNT(*) FROM proposals WHERE workflow_id=? AND action NOT LIKE 'auto:%'
              AND status NOT IN ('prepared','stale','rejected','expired','lapsed','denied') AND created_at>=?""",(wid,cutoff)).fetchone()[0]
            maximum=int(wf.get("governance",{}).get("max_requests_per_24h",1))
            if sent>=maximum:
                self._update_proposal(pid,"deferred_fatigue",ts,details={"requests_in_24h":sent,"limit":maximum})
            else:
                self._request_approval(pid,wid,base,action,target,stored,ts,wf)
        return pid

    def _update_proposal(self,pid,status,ts,approval_id=None,details=None):
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status=?,approval_id=COALESCE(?,approval_id),updated_at=? WHERE proposal_id=?",
                                  (status,approval_id,ts,pid))
        row=self.store.db.execute("SELECT workflow_id,action,target FROM proposals WHERE proposal_id=?",(pid,)).fetchone()
        self.store.audit(ts,row[0] if row else None,"proposal_"+status,{"proposal_id":pid,"action":row[1] if row else None,
            "target":row[2] if row else None,**(details or {})})

    def _apply_auto(self,pid,wid,base,action,target,body,ts):
        try:
            response=self.request("PUT",f"/workflows/{wid}/config",json={k:v for k,v in body.items() if k!="workflow_id"})
            if response.is_success: self._update_proposal(pid,"applied",ts,details=response.json())
            else: self._update_proposal(pid,"denied",ts,details={"http_status":response.status_code,"response":response.text})
        except (httpx.HTTPError,RuntimeError) as exc:
            self._update_proposal(pid,"uncertain",ts,details={"error":str(exc)})

    def _approval_list(self,wid):
        result=self.get_json("/approvals",params={"workflow_id":wid})
        if isinstance(result,list): return result
        for key in ("approvals","items","results"):
            if isinstance(result.get(key),list): return result[key]
        return []

    def _request_approval(self,pid,wid,base,action,target,body,ts,wf=None):
        hours=(wf or {}).get("governance",{}).get("approver_hours")
        if hours:
            try: hour=datetime.fromisoformat(ts).hour
            except (ValueError,TypeError): hour=-1
            if not (int(hours[0])<=hour<int(hours[1])):
                self.store.audit(ts,wid,"proposal_waiting_approver_hours",{"proposal_id":pid,"approver_hours":hours})
                return
        matches=[a for a in self._approval_list(wid) if a.get("proposal_id")==pid]
        if matches:
            a=matches[0]; self._update_proposal(pid,a.get("status","pending"),ts,a.get("approval_id"),{"reconciled":True}); return
        request_body=dict(body)
        try:
            response=self.request("POST","/approvals",json=request_body)
            if response.is_success:
                result=response.json(); self._update_proposal(pid,result.get("status","pending"),ts,result.get("approval_id"),result)
            elif response.status_code==409:
                matches=[a for a in self._approval_list(wid) if a.get("proposal_id")==pid]
                if matches:
                    a=matches[0]; self._update_proposal(pid,a.get("status","pending"),ts,a.get("approval_id"),{"reconciled_duplicate":True})
                else: self._update_proposal(pid,"stale",ts,details={"http_status":409,"response":response.text})
            else: self._update_proposal(pid,"rejected",ts,details={"http_status":response.status_code,"response":response.text})
        except (httpx.HTTPError,RuntimeError) as exc:
            self._update_proposal(pid,"uncertain",ts,details={"error":str(exc)})

    def reconcile_proposals(self, workflows: dict[str,dict], ts: str):
        rows=self.store.db.execute("SELECT proposal_id,workflow_id,approval_id,status,base_version,action,target,body FROM proposals WHERE status IN ('prepared','pending','approved','uncertain')").fetchall()
        for pid,wid,aid,status,base,action,target,body_json in rows:
            wf=workflows.get(wid,{})
            body=json.loads(body_json)
            try:
                current=self.get_json(f"/workflows/{wid}/config")
                if status in ("prepared","uncertain") and action.startswith("auto:"):
                    if current["version"]>base and current.get("config")==body.get("proposed_config"):
                        self._update_proposal(pid,"applied",ts,details={"reconciled_config_version":current["version"]}); continue
                    if current["version"]!=base:
                        self._update_proposal(pid,"stale",ts,details={"current_version":current["version"]}); continue
                    self._apply_auto(pid,wid,base,action,target,body,ts); continue
                if status in ("prepared","uncertain") and not aid:
                    self._request_approval(pid,wid,base,action,target,body,ts,wf); continue
                if not aid: continue
                approval=self.get_json(f"/approvals/{aid}"); state=approval.get("status","unknown")
                if state in ("rejected","expired","lapsed"):
                    self._update_proposal(pid,state,ts,aid,approval); continue
                if state=="approved":
                    if current["version"]!=base:
                        # If the current version is the proposed config, reconcile a lost apply response.
                        if current.get("config")==body.get("proposed_config"):
                            self._update_proposal(pid,"applied",ts,aid,{"reconciled_config_version":current["version"]})
                        else: self._update_proposal(pid,"stale",ts,aid,{"current_version":current["version"]})
                        continue
                    response=self.request("POST",f"/workflows/{wid}/apply",json={"approval_id":aid})
                    if response.is_success: self._update_proposal(pid,"applied",ts,aid,response.json())
                    elif response.status_code==409: self._update_proposal(pid,"stale",ts,aid,{"response":response.text})
                    elif response.status_code==410: self._update_proposal(pid,"lapsed",ts,aid,{"response":response.text})
                    else: self._update_proposal(pid,"apply_error",ts,aid,{"status":response.status_code,"response":response.text})
                elif state in ("pending","approved") and state!=status:
                    self._update_proposal(pid,state,ts,aid,approval)
            except (httpx.HTTPError,RuntimeError,ValueError,KeyError) as exc:
                self.store.audit(ts,wid,"proposal_reconcile_error",{"proposal_id":pid,"error":str(exc)})

    def _variant_window(self,wid,dimension,start,end,variant=None):
        query="""SELECT COALESCE(SUM(dials),0),COALESCE(SUM(connected),0),COALESCE(SUM(engaged),0),
          COALESCE(SUM(qa_n),0),COALESCE(SUM(hallucinations),0) FROM variant_hourly
          WHERE workflow_id=? AND dimension=? AND hour_key>=? AND hour_key<?"""
        params=[wid,dimension,Store._hour(start),Store._hour(end)]
        if variant is not None: query+=" AND variant=?"; params.append(str(variant))
        return self.store.db.execute(query,params).fetchone()

    @staticmethod
    def _rate_test(control_n,control_x,treatment_n,treatment_x,min_gap=0.015,z=3.3):
        if min(control_n,treatment_n)<50: return False
        p0=control_x/control_n; p1=treatment_x/treatment_n
        se=math.sqrt(p0*(1-p0)/control_n+p1*(1-p1)/treatment_n)
        return p1-p0>=min_gap and p1-p0>z*se

    @staticmethod
    def _rate_delta(control_n,control_x,treatment_n,treatment_x):
        p0=control_x/control_n if control_n else 0
        p1=treatment_x/treatment_n if treatment_n else 0
        se=math.sqrt(p0*(1-p0)/control_n+p1*(1-p1)/treatment_n) if control_n and treatment_n else float("inf")
        return p1-p0,se

    def _experiment_counts(self,wid,dimension,cohort,variant,start,end):
        return self.store.db.execute("""SELECT COALESCE(SUM(dials),0),COALESCE(SUM(connected),0),
          COALESCE(SUM(engaged),0) FROM variant_hourly WHERE workflow_id=? AND dimension=? AND cohort_id=?
          AND variant=? AND hour_key>=? AND hour_key<?""",
          (wid,dimension,cohort,str(variant),Store._hour(start),Store._hour(end))).fetchone()

    def _record_experiment(self,experiment_id,status,ts,decision=None):
        with self.store.db:
            self.store.db.execute("UPDATE experiments SET status=?,ended_at=CASE WHEN ? IN ('active','promoted') THEN ended_at ELSE ? END,decision=COALESCE(?,decision) WHERE experiment_id=?",
                                  (status,status,ts,json.dumps(decision,sort_keys=True) if decision is not None else None,experiment_id))
        row=self.store.db.execute("SELECT workflow_id,dimension,cohort_id,candidate FROM experiments WHERE experiment_id=?",(experiment_id,)).fetchone()
        if row: self.store.audit(ts,row[0],"experiment_"+status,{"experiment_id":experiment_id,"dimension":row[1],"cohort_id":row[2],"candidate":row[3],"decision":decision})

    def _submit_experiment_allocation(self,exp,wf,current,new_distribution,action,ts,reason):
        cfg=json.loads(json.dumps(current["config"]))
        cfg.setdefault("allocations",{}).setdefault(exp["cohort_id"],{})[exp["dimension"]]=new_distribution
        pid=self.submit_change(wf,current,cfg,action,f"{exp['experiment_id']}:{action}",reason,ts,
            summary=reason,dry_run={"control":exp["control"],"candidate":exp["candidate"],"allocation":new_distribution,
                                  "historical_replay":"not causal; decision uses contemporaneous randomized exposure"},
            impact_score=0.25,affected_leads=int(wf.get("daily_batch",{}).get("size",0)))
        return pid

    def _new_experiment(self,wf,current,ts):
        wid=wf.get("workflow_id",wf.get("id")); cfg=current["config"]
        if wf.get("mid_flight")!="allowed": return
        # Do not explore while a supervisor prompt edit is under safety investigation.
        active_incident=self.store.db.execute("SELECT 1 FROM incidents WHERE workflow_id=? AND kind='supervisor_prompt_change' AND status NOT IN ('verified','proposal_rejected','proposal_expired','proposal_lapsed') LIMIT 1",(wid,)).fetchone()
        if active_incident: return
        for dimension in ("voice","welcome_message"):
            options=wf.get("options",{}).get(dimension,[])
            if len(options)<2: continue
            supervisor_change=self.store.db.execute("""SELECT 1 FROM event_index WHERE workflow_id=? AND type='config_changed'
              AND actor='supervisor' AND changed_dims LIKE ? LIMIT 1""",(wid,f'%"{dimension}"%')).fetchone()
            if supervisor_change: continue
            group="default"
            cohorts=cfg.get("cohorts",[])
            if cohorts:
                volumes=[]
                for cohort in cohorts:
                    cid=cohort.get("id")
                    n=self.store.db.execute("""SELECT COALESCE(SUM(dials),0) FROM variant_hourly WHERE workflow_id=?
                      AND dimension=? AND cohort_id=?""",(wid,dimension,cid)).fetchone()[0]
                    volumes.append((n,cid))
                if not volumes: continue
                group=max(volumes,key=lambda x:(x[0],x[1]))[1]
            dist=cfg.get("allocations",{}).get(group,{}).get(dimension)
            if dist is None: dist=cfg.get("allocations",{}).get("default",{}).get(dimension)
            weights=self._normalized(dist)
            active=[k for k,v in weights.items() if v>0.999]
            if len(active)!=1: continue
            control=active[0]
            prior={r[0] for r in self.store.db.execute("SELECT candidate FROM experiments WHERE workflow_id=? AND dimension=? AND cohort_id=?",(wid,dimension,group))}
            candidate=next((str(v) for v in options if str(v)!=str(control) and str(v) not in prior),None)
            if candidate is None: continue
            exp_id=str(uuid.uuid5(uuid.NAMESPACE_URL,f"experiment:{wid}:{dimension}:{group}:{control}:{candidate}:{current['version']}"))
            with self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (exp_id,wid,dimension,group,str(control),candidate,0.2,current["version"],"prepared",ts,None,None))
            distribution={str(control):0.8,candidate:0.2}
            exp={"experiment_id":exp_id,"workflow_id":wid,"dimension":dimension,"cohort_id":group,
                 "control":str(control),"candidate":candidate,"allocation":0.2}
            pid=self._submit_experiment_allocation(exp,wf,current,distribution,"experiment_start",ts,
                f"Start a 20% randomized {dimension} challenger against {control} for cohort {group}.")
            if pid:
                self._record_experiment(exp_id,"awaiting_start",ts,{"proposal_id":pid})
                return

    def run_experiments(self,workflows,ts):
        now=datetime.fromisoformat(ts)
        by_id={w.get("workflow_id",w.get("id")):w for w in workflows}
        rows=self.store.db.execute("SELECT experiment_id,workflow_id,dimension,cohort_id,control,candidate,allocation,base_version,status,started_at,decision FROM experiments WHERE status IN ('awaiting_start','active','promotion_pending','rollback_pending','promoted','confounded','abort_pending') ORDER BY started_at").fetchall()
        handled=set()
        for row in rows:
            eid,wid,dim,cohort,control,candidate,allocation,base,status,started,decision_raw=row
            handled.add(wid); wf=by_id.get(wid,{})
            decision=json.loads(decision_raw) if decision_raw else {}
            current=self.get_json(f"/workflows/{wid}/config")
            if status=="abort_pending":
                pid=decision.get("proposal_id")
                proposal=self.store.db.execute("SELECT status FROM proposals WHERE proposal_id=?",(pid,)).fetchone()
                if proposal and proposal[0]=="applied":
                    self._record_experiment(eid,"aborted_confounded",ts,{"proposal_id":pid,"reason":"supervisor prompt change invalidated experiment"})
                elif proposal and proposal[0] in ("rejected","expired","lapsed","stale","denied","deferred_fatigue","apply_error"):
                    self._record_experiment(eid,"abort_failed",ts,{"proposal_id":pid,"proposal_status":proposal[0]})
                continue
            if status=="awaiting_start":
                pid=decision.get("proposal_id")
                proposal=self.store.db.execute("SELECT status,updated_at FROM proposals WHERE proposal_id=?",(pid,)).fetchone()
                if proposal and proposal[0]=="applied":
                    conf_event=self.store.db.execute("SELECT ts,version FROM event_index WHERE proposal_id=? AND type='config_changed' ORDER BY event_id DESC LIMIT 1",(pid,)).fetchone()
                    start_at=conf_event[0] if conf_event else ts
                    with self.store.db:
                        self.store.db.execute("UPDATE experiments SET status='active',started_at=?,base_version=? WHERE experiment_id=?",(start_at,current["version"],eid))
                    self._record_experiment(eid,"active",ts,{"actual_start":start_at})
                elif proposal and proposal[0] not in ("prepared","pending","approved","uncertain"):
                    self._record_experiment(eid,"start_failed",ts,{"proposal_status":proposal[0]})
                continue
            if status=="confounded":
                # Restore only the controller's experiment dimension; preserve all
                # supervisor-owned dimensions from the latest full configuration.
                cfg=json.loads(json.dumps(current["config"]))
                cfg.setdefault("allocations",{}).setdefault(cohort,{})[dim]={control:1.0}
                exp={"experiment_id":eid,"workflow_id":wid,"dimension":dim,"cohort_id":cohort,"control":control,"candidate":candidate}
                pid=self.submit_change(wf,current,cfg,"auto:experiment_abort",eid,
                    "Restore the pre-test allocation because a supervisor changed shared call behavior during measurement.",ts,
                    summary="End the confounded experiment and restore its control allocation.",
                    dry_run={"reason":"supervisor config change invalidated the test window","dimension":dim,"control":control},
                    impact_score=0.1,affected_leads=int(wf.get("daily_batch",{}).get("size",0)))
                self._record_experiment(eid,"abort_pending",ts,{"proposal_id":pid,"reason":"supervisor prompt edit confounded measurement"})
                continue
            if status in ("promotion_pending","rollback_pending"):
                pid=decision.get("proposal_id"); p=self.store.db.execute("SELECT status FROM proposals WHERE proposal_id=?",(pid,)).fetchone()
                if p and p[0]=="applied":
                    if status=="promotion_pending":
                        metrics=self._experiment_counts(wid,dim,cohort,candidate,started,ts)
                        control_metrics=self._experiment_counts(wid,dim,cohort,control,started,ts)
                        baseline=self._compare_experiment(wf,metrics,control_metrics)
                        self._record_experiment(eid,"promoted",ts,{"proposal_id":pid,"baseline":baseline,"promoted_at":ts})
                    else: self._record_experiment(eid,"rolled_back",ts,{"proposal_id":pid})
                elif p and p[0] in ("rejected","expired","lapsed","stale","denied","deferred_fatigue"):
                    self._record_experiment(eid,"promotion_failed",ts,{"proposal_status":p[0]})
                continue
            if status=="promoted":
                if decision.get("monitoring_result"): continue
                baseline=decision.get("baseline",{}); promoted_at=decision.get("promoted_at",started)
                monitor_end=datetime.fromisoformat(promoted_at)+timedelta(hours=24)
                if now<monitor_end: continue
                post=self._experiment_counts(wid,dim,cohort,candidate,promoted_at,monitor_end.isoformat())
                if post[0]<300 or (wf.get("objective",{}).get("primary")=="engagement_rate" and post[1]<100):
                    self._record_experiment(eid,"monitoring_inconclusive",ts,{"monitoring_result":"minimum_sample_not_met","dials":post[0],"window_hours":24})
                    continue
                current_kpis=self._arm_rates(wf,post)
                details,regressed=self._promotion_monitor(wf,baseline,post,promoted_at,monitor_end.isoformat())
                details.update({"post_promotion":current_kpis,"dials":post[0],"monitor_window_hours":24})
                if regressed:
                    primary=wf.get("objective",{}).get("primary")
                    cfg=json.loads(json.dumps(current["config"]))
                    cfg.setdefault("allocations",{}).setdefault(cohort,{})[dim]={control:1.0}
                    exp={"experiment_id":eid,"workflow_id":wid,"dimension":dim,"cohort_id":cohort,"control":control,"candidate":candidate}
                    pid=self.submit_change(wf,current,cfg,"experiment_rollback",eid,
                        f"Promoted {candidate} has degraded on {primary} by {delta:.3f} against its promotion baseline; restore {control}.",ts,
                        summary=f"Roll back degraded {dim} {candidate} to {control}.",dry_run=details,impact_score=0.7,
                        affected_leads=int(wf.get("daily_batch",{}).get("size",0)))
                    self._record_experiment(eid,"rollback_pending",ts,{"proposal_id":pid,"verification":details})
                else:
                    self._record_experiment(eid,"monitoring_passed",ts,{"monitoring_result":"no_material_degradation","verification":details,"window_hours":24})
                continue
            if status!="active": continue
            elapsed=(now-datetime.fromisoformat(started)).total_seconds()/3600
            if elapsed<48: continue
            candidate_metrics=self._experiment_counts(wid,dim,cohort,candidate,started,ts)
            control_metrics=self._experiment_counts(wid,dim,cohort,control,started,ts)
            options=wf.get("options",{})
            multiplicity=sum(max(0,len(options.get(d,[]))-1) for d in ("voice","welcome_message"))
            comparison=self._compare_experiment(wf,candidate_metrics,control_metrics,multiplicity)
            if not comparison.get("minimum_sample"):
                decision={"result":"inconclusive_minimum_sample","candidate":candidate_metrics,"control":control_metrics}
                self._finish_experiment_to_control(eid,wf,current,control,candidate,dim,cohort,ts,decision)
                continue
            primary=wf.get("objective",{}).get("primary")
            if comparison.get("primary_wins") and comparison.get("guardrail_passes"):
                cfg=json.loads(json.dumps(current["config"]))
                cfg.setdefault("allocations",{}).setdefault(cohort,{})[dim]={candidate:1.0}
                rationale=(f"In a fixed 48-hour 80/20 randomized test, {candidate} beat {control} on primary {primary} "
                           f"by {comparison['primary_delta']:.3f} (z={comparison['primary_z']:.2f}); the independent guardrail passed.")
                pid=self.submit_change(wf,current,cfg,"experiment_promote",f"{dim}:{cohort}:{candidate}",rationale,ts,
                    summary=f"Promote {candidate} for {cohort} after a statistically supported test.",
                    dry_run=comparison,impact_score=min(1,max(0,comparison["primary_delta"]*2)),
                    affected_leads=int(wf.get("daily_batch",{}).get("size",0)))
                self._record_experiment(eid,"promotion_pending",ts,{"proposal_id":pid,"comparison":comparison})
            else:
                decision={"result":"not_promoted","comparison":comparison}
                self._finish_experiment_to_control(eid,wf,current,control,candidate,dim,cohort,ts,decision)
        for wid,wf in by_id.items():
            active=self.store.db.execute("SELECT 1 FROM experiments WHERE workflow_id=? AND status IN ('awaiting_start','active','promotion_pending','rollback_pending','promoted','confounded','abort_pending') LIMIT 1",(wid,)).fetchone()
            if not active:
                current=self.get_json(f"/workflows/{wid}/config")
                self._new_experiment(wf,current,ts)

    def _arm_rates(self,wf,metrics):
        dials,connected,engaged=metrics
        return {"pickup_rate":connected/dials if dials else 0,
                "engagement_rate":engaged/connected if connected else 0}

    def _promotion_monitor(self,wf,baseline,post,start,end):
        """Take one fixed 24h post-promotion look; compare primary and guardrail separately."""
        objective=wf.get("objective",{}); primary=objective.get("primary")
        base=baseline.get("candidate",(0,0,0)); bd,bc,be=base
        pd,pc,pe=post
        def pair(metric,counts):
            d,c,e=counts
            return (d,c) if metric=="pickup_rate" else (c,e)
        bn,bx=pair(primary,base); pn,px=pair(primary,post)
        primary_delta,primary_se=self._rate_delta(bn,bx,pn,px)
        guard=objective.get("guardrail",{}); guard_name=next(iter(guard),None)
        guard_metric=guard_name.replace("_max_drop","") if guard_name else None
        if guard_metric:
            gbn,gbx=pair(guard_metric,base); gpn,gpx=pair(guard_metric,post)
            guard_delta,guard_se=self._rate_delta(gbn,gbx,gpn,gpx)
            guard_lower=guard_delta-1.96*guard_se
            guard_failed=guard_lower < -float(guard[guard_name])
        else:
            guard_delta=0.0; guard_lower=0.0; guard_failed=False
        primary_upper=primary_delta+1.96*primary_se
        primary_failed=primary_upper < -0.02
        details={"monitor_start":start,"monitor_end":end,"primary":primary,
            "primary_delta":primary_delta,"primary_upper_95":primary_upper,
            "primary_degradation_threshold":-0.02,"guardrail":guard_name,
            "guardrail_delta":guard_delta,"guardrail_lower_95":guard_lower,
            "guardrail_limit":float(guard.get(guard_name,0)) if guard_name else None,
            "primary_regression":primary_failed,"guardrail_regression":guard_failed}
        return details,primary_failed or guard_failed

    def _compare_experiment(self,wf,candidate,control,multiplicity=1):
        cd,cc,ce=candidate; bd,bc,be=control
        objective=wf.get("objective",{}); primary=objective.get("primary")
        if primary=="pickup_rate": p_n,p_x,b_n,b_x=cd,cc,bd,bc
        else: p_n,p_x,b_n,b_x=cc,ce,bc,be
        primary_delta,primary_se=self._rate_delta(b_n,b_x,p_n,p_x)
        primary_z=primary_delta/primary_se if primary_se and math.isfinite(primary_se) else 0
        guard=objective.get("guardrail",{}); guard_name=next(iter(guard),None)
        guard_metric=guard_name.replace("_max_drop","") if guard_name else None
        if guard_metric=="pickup_rate": gn,gx,bgn,bgx=cd,cc,bd,bc
        elif guard_metric=="engagement_rate": gn,gx,bgn,bgx=cc,ce,bc,be
        else: gn=gx=bgn=bgx=0
        gd,gse=self._rate_delta(bgn,bgx,gn,gx) if gn and bgn else (0,float("inf"))
        limit=float(guard.get(guard_name,0)) if guard_name else 0
        # One fixed look after 48h. Bonferroni controls planned variant comparisons.
        primary_threshold=NormalDist().inv_cdf(1-0.005/max(1,int(multiplicity)))
        needs_connected=primary=="engagement_rate" or guard_metric=="engagement_rate"
        minimum=cd>=300 and bd>=300 and (not needs_connected or min(cc,bc)>=100)
        guardrail_passes=(not guard_name) or (gd-1.96*gse>=-limit)
        return {"minimum_sample":minimum,"candidate_dials":cd,"control_dials":bd,
                "candidate_connected":cc,"control_connected":bc,"candidate_engaged":ce,"control_engaged":be,
                "primary":primary,"primary_delta":primary_delta,"primary_z":primary_z,
                "primary_threshold":primary_threshold,
                "primary_wins":minimum and primary_delta>=0.02 and primary_z>=primary_threshold,
                "guardrail":guard_name,"guardrail_delta":gd,"guardrail_limit":limit,"guardrail_passes":guardrail_passes,
                "candidate_kpis":self._arm_rates(wf,candidate),"control_kpis":self._arm_rates(wf,control)}

    def _finish_experiment_to_control(self,eid,wf,current,control,candidate,dimension,cohort,ts,decision):
        cfg=json.loads(json.dumps(current["config"]))
        cfg.setdefault("allocations",{}).setdefault(cohort,{})[dimension]={control:1.0}
        pid=self.submit_change(wf,current,cfg,"auto:experiment_stop",eid,
            "End the fixed-window experiment without promoting the candidate and restore its control allocation.",ts,
            summary=f"End the {dimension} test and restore {control}.",dry_run=decision,impact_score=0.1,
            affected_leads=int(wf.get("daily_batch",{}).get("size",0)))
        self._record_experiment(eid,"rollback_pending",ts,{"proposal_id":pid,"decision":decision})

    def observe_supervisor_edits(self,ts):
        marker_row=self.store.db.execute("SELECT value FROM meta WHERE key='supervisor_event_cursor'").fetchone()
        last=int(marker_row[0]) if marker_row else 0
        rows=self.store.db.execute("""SELECT event_id,workflow_id,ts,version,changed_dims FROM event_index
          WHERE type='config_changed' AND actor='supervisor' AND event_id>? ORDER BY event_id""",(last,)).fetchall()
        max_id=last
        for event_id,wid,when,version,dimensions in rows:
            max_id=max(max_id,event_id); dims=json.loads(dimensions or "[]")
            self.store.audit(when,wid,"supervisor_config_observed",{"event_id":event_id,"version":version,"changed_dims":dims})
            if "voice" in dims:
                with self.store.db:
                    self.store.db.execute("""UPDATE experiments SET status='aborted_supervisor_edit',ended_at=?,
                      decision='supervisor changed voice configuration' WHERE workflow_id=? AND dimension='voice'
                      AND status IN ('active','awaiting_start','promotion_pending')""",(when,wid))
            if "prompt_version" in dims:
                with self.store.db:
                    self.store.db.execute("""UPDATE experiments SET status='confounded',decision=?
                      WHERE workflow_id=? AND status IN ('active','promotion_pending')""",
                      (json.dumps({"reason":"supervisor prompt change confounded KPI comparison","event_id":event_id}),wid))
                details={"event_id":event_id,"version":version,"changed_dims":dims}
                iid=f"supervisor-prompt:{wid}:{event_id}"
                with self.store.db:
                    self.store.db.execute("INSERT OR IGNORE INTO incidents VALUES(?,?,?,?,?,?,?)",
                      (iid,wid,"supervisor_prompt_change","observing",when,json.dumps(details),when))
        if max_id>last:
            with self.store.db:
                self.store.db.execute("INSERT INTO meta VALUES('supervisor_event_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(max_id),))

    def detect_prompt_incidents(self,workflows,ts):
        now=datetime.fromisoformat(ts)
        for wf in workflows:
            wid=wf.get("workflow_id",wf.get("id"))
            if not wid: continue
            edit=self.store.db.execute("""SELECT event_id,ts,version FROM event_index WHERE workflow_id=?
              AND type='config_changed' AND actor='supervisor' AND changed_dims LIKE '%prompt_version%' ORDER BY event_id DESC LIMIT 1""",(wid,)).fetchone()
            if not edit: continue
            eid,edited_at,version=edit
            iid=f"supervisor-prompt:{wid}:{eid}"
            incident=self.store.db.execute("SELECT status,details FROM incidents WHERE incident_id=?",(iid,)).fetchone()
            if not incident or incident[0] not in ("observing","detected"): continue
            current=self.get_json(f"/workflows/{wid}/config")
            prompt=current["config"].get("prompt_version")
            if not prompt: continue
            start=datetime.fromisoformat(edited_at)
            if now-start<timedelta(hours=1): continue
            base_start=start-timedelta(hours=72)
            baseline_rows=self.store.db.execute("""SELECT variant,SUM(qa_n),SUM(hallucinations),SUM(dials),
              SUM(connected),SUM(engaged) FROM variant_hourly WHERE workflow_id=? AND dimension='prompt_version'
              AND hour_key>=? AND hour_key<? GROUP BY variant HAVING SUM(qa_n)>0 ORDER BY SUM(qa_n) DESC,variant""",
              (wid,Store._hour(base_start),Store._hour(start))).fetchall()
            baseline=next((r for r in baseline_rows if str(r[0])!=str(prompt)),None)
            if not baseline: continue
            candidate=self._variant_window(wid,"prompt_version",edited_at,ts,prompt)
            cqa,cflags=baseline[1],baseline[2]
            tn,tc,te,tqa,tflags=candidate
            base_rate=cflags/cqa if cqa else 0
            current_rate=tflags/tqa if tqa else 0
            if tqa<50 or not self._rate_test(cqa,cflags,tqa,tflags): continue
            details={"event_id":eid,"config_version":version,"prompt":prompt,"baseline_prompt":baseline[0],
                     "baseline_qa_n":cqa,"baseline_flags":cflags,"baseline_rate":base_rate,
                     "current_qa_n":tqa,"current_flags":tflags,"current_rate":current_rate,
                     "observed_at":ts}
            with self.store.db:
                self.store.db.execute("UPDATE incidents SET status='detected',details=?,updated_at=? WHERE incident_id=?",
                                      (json.dumps(details,sort_keys=True),ts,iid))
            self.store.audit(ts,wid,"prompt_hallucination_incident_detected",details)

    def handle_prompt_incidents(self,workflows,ts):
        by_id={w.get("workflow_id",w.get("id")):w for w in workflows}
        rows=self.store.db.execute("SELECT incident_id,workflow_id,details FROM incidents WHERE kind='supervisor_prompt_change' AND status='detected'").fetchall()
        for iid,wid,raw in rows:
            wf=by_id.get(wid,{})
            details=json.loads(raw); state=self.get_json("/sim/state"); credits=int(state.get("credits_remaining",0))
            if credits<5:
                self.store.audit(ts,wid,"prompt_fix_skipped_credit_reserve",{"credits_remaining":credits,"incident_id":iid}); continue
            problem=(f"The current workflow prompt has a connected-call hallucination flag rate {details['current_rate']:.4f} "
                     f"across {details['current_qa_n']} QA-scored calls, compared with {details['baseline_rate']:.4f} "
                     f"across {details['baseline_qa_n']} historical calls. Suggest grounded lower-risk prompt versions; "
                     "do not trade away required disclosures or invent policy facts.")
            try:
                response=self.request("POST","/llm/suggest-fixes",json={"workflow_id":wid,"problem":problem})
                if not response.is_success:
                    self.store.audit(ts,wid,"prompt_suggestion_failed",{"status":response.status_code,"response":response.text}); continue
                suggestion=response.json()
            except (RuntimeError,httpx.HTTPError) as exc:
                self.store.audit(ts,wid,"prompt_suggestion_failed",{"error":str(exc)}); continue
            options=set(wf.get("options",{}).get("prompt_version",[]))
            choices=suggestion.get("suggestions",suggestion.get("fixes",[])) if isinstance(suggestion,dict) else []
            normalized=[]
            for item in choices:
                if isinstance(item,str): normalized.append({"prompt_version":item,"summary":"Suggested prompt"})
                elif isinstance(item,dict): normalized.append(item)
            candidates=sorted({str(x.get("prompt_version",x.get("version",""))) for x in normalized if str(x.get("prompt_version",x.get("version",""))) in options and str(x.get("prompt_version",x.get("version","")))!=str(details["prompt"])})
            current=self.get_json(f"/workflows/{wid}/config")
            minimum=float(wf.get("governance",{}).get("min_validation_pass_rate",0.97))
            evidence_candidates=[]
            for target in candidates[:3]:
                credits=int(self.get_json("/sim/state").get("credits_remaining",0))
                if credits<2: break
                try:
                    vr=self.request("POST","/validation/runs",json={"workflow_id":wid,"prompt_version":target,"n":200})
                    if not vr.is_success:
                        self.store.audit(ts,wid,"prompt_validation_failed",{"target":target,"status":vr.status_code,"response":vr.text}); continue
                    validation=vr.json()
                    if float(validation.get("pass_rate",0))>=minimum:
                        evidence_candidates.append((float(validation["pass_rate"]),target,validation))
                except (RuntimeError,httpx.HTTPError,KeyError,ValueError) as exc:
                    self.store.audit(ts,wid,"prompt_validation_failed",{"target":target,"error":str(exc)})
            if not evidence_candidates:
                with self.store.db:
                    self.store.db.execute("UPDATE incidents SET status='no_validated_fix',updated_at=? WHERE incident_id=?",(ts,iid))
                self.store.audit(ts,wid,"prompt_fix_no_validated_candidate",{"incident_id":iid,"suggestions":suggestion}); continue
            pass_rate,target,validation=max(evidence_candidates,key=lambda x:(x[0],x[1]))
            proposed=json.loads(json.dumps(current["config"])); proposed["prompt_version"]=target
            dry={"historical_window_hours":72,"baseline_prompt":details["baseline_prompt"],
                 "baseline_qa_n":details["baseline_qa_n"],"baseline_hallucination_rate":details["baseline_rate"],
                 "incident_prompt":details["prompt"],"incident_qa_n":details["current_qa_n"],
                 "incident_hallucination_rate":details["current_rate"],"synthetic_validation_n":200,
                 "synthetic_pass_rate":pass_rate,"engagement_effect":"not established by validation"}
            affected=int(wf.get("daily_batch",{}).get("size",0))
            score=min(1,max(0,(details["current_rate"]-details["baseline_rate"])/max(details["current_rate"],0.01)))
            rationale=(f"A supervisor prompt edit was followed by {details['current_flags']}/{details['current_qa_n']} "
                       f"hallucination flags versus {details['baseline_flags']}/{details['baseline_qa_n']} before the edit. "
                       f"The suggested {target} passed synthetic safety validation at {pass_rate:.3f}; validation says nothing "
                       "about engagement, so this change requires human approval and post-apply KPI verification.")
            pid=self.submit_change(wf,current,proposed,"incident_prompt_fix",target,rationale,ts,
                evidence={"validation_run_id":validation.get("validation_run_id")},
                summary="Replace a prompt associated with elevated hallucination flags.",dry_run=dry,
                impact_score=score,affected_leads=affected)
            if pid:
                details.update({"proposal_id":pid,"safe_prompt":details["baseline_prompt"],"target_prompt":target,
                                "validation_run_id":validation.get("validation_run_id"),"validation_pass_rate":pass_rate})
                with self.store.db:
                    self.store.db.execute("UPDATE incidents SET status='approval_pending',details=?,updated_at=? WHERE incident_id=?",
                                          (json.dumps(details,sort_keys=True),ts,iid))
                self.store.audit(ts,wid,"prompt_fix_proposed",details)

    def verify_prompt_changes(self,workflows,ts):
        by_id={w.get("workflow_id",w.get("id")):w for w in workflows}
        rows=self.store.db.execute("SELECT incident_id,workflow_id,details,status FROM incidents WHERE kind='supervisor_prompt_change' AND status IN ('approval_pending','verify','rollback_pending')").fetchall()
        for iid,wid,raw,status in rows:
            details=json.loads(raw); wf=by_id.get(wid,{})
            if status=="rollback_pending":
                rollback=self.store.db.execute("SELECT status,body FROM proposals WHERE proposal_id=?",(details.get("rollback_proposal_id"),)).fetchone()
                if not rollback: continue
                rstatus,rbody=rollback
                if rstatus=="applied":
                    current=self.get_json(f"/workflows/{wid}/config")
                    expected=json.loads(rbody).get("proposed_config",{}).get("prompt_version")
                    observed=current.get("config",{}).get("prompt_version")
                    new_status="rolled_back" if str(observed)==str(expected)==str(details.get("safe_prompt")) else "rollback_config_mismatch"
                elif rstatus in ("rejected","expired","lapsed","stale","denied","deferred_fatigue","apply_error"):
                    new_status="rollback_"+("failed" if rstatus=="apply_error" else rstatus)
                else:
                    continue
                details["rollback_status"]=rstatus
                details["rollback_observed_prompt"]=observed if rstatus=="applied" else None
                with self.store.db:
                    self.store.db.execute("UPDATE incidents SET status=?,details=?,updated_at=? WHERE incident_id=?",
                        (new_status,json.dumps(details,sort_keys=True),ts,iid))
                self.store.audit(ts,wid,"prompt_incident_"+new_status,{"incident_id":iid,
                    "rollback_proposal_id":details.get("rollback_proposal_id"),"proposal_status":rstatus,
                    "observed_prompt":details.get("rollback_observed_prompt")})
                continue
            proposal=self.store.db.execute("SELECT status,updated_at FROM proposals WHERE proposal_id=?",(details.get("proposal_id"),)).fetchone()
            if not proposal: continue
            pstatus,updated=proposal
            if status=="approval_pending":
                if pstatus=="applied":
                    details["applied_at"]=updated
                    with self.store.db:
                        self.store.db.execute("UPDATE incidents SET status='verify',details=?,updated_at=? WHERE incident_id=?",(json.dumps(details,sort_keys=True),ts,iid))
                    self.store.audit(ts,wid,"prompt_fix_applied_begin_verification",details)
                elif pstatus in ("rejected","expired","lapsed","stale","denied","deferred_fatigue"):
                    with self.store.db:
                        self.store.db.execute("UPDATE incidents SET status=?,updated_at=? WHERE incident_id=?",("proposal_"+pstatus,ts,iid))
                continue
            try: applied=datetime.fromisoformat(details["applied_at"])
            except (ValueError,KeyError): continue
            current=self.get_json(f"/workflows/{wid}/config")
            target=current["config"].get("prompt_version")
            if str(target)!=str(details.get("target_prompt")): continue
            result=self._variant_window(wid,"prompt_version",details["applied_at"],ts,target)
            dials,connected,engaged,qa_n,flags=result
            if connected<100 or qa_n<50: continue
            pickup=connected/dials if dials else 0
            engagement=engaged/connected if connected else 0
            # Incident-period metrics provide the immediate pre-change comparison.
            before=self._variant_window(wid,"prompt_version",details.get("observed_at",ts),details.get("applied_at",ts),details.get("prompt"))
            b_dials,b_conn,b_eng,b_qa,b_flags=before
            before_pickup=b_conn/b_dials if b_dials else 0
            before_eng=b_eng/b_conn if b_conn else 0
            objective=wf.get("objective",{}); primary=objective.get("primary")
            guard=objective.get("guardrail",{}); guard_name=next(iter(guard),None)
            delta={"pickup_rate":pickup-before_pickup,"engagement_rate":engagement-before_eng}
            guard_delta=delta.get(guard_name.replace("_max_drop",""),0) if guard_name else 0
            guard_limit=float(guard.get(guard_name,0)) if guard_name else 0
            safety_bad=self._rate_test(int(details.get("baseline_qa_n",0)),int(details.get("baseline_flags",0)),
                                       qa_n,flags,min_gap=0.015)
            kpi_bad=delta.get(primary,0)<-0.02 or guard_delta < -guard_limit
            verification={"post_apply_dials":dials,"post_apply_connected":connected,"post_apply_engaged":engaged,
                "post_apply_qa_n":qa_n,"post_apply_flags":flags,"post_apply_hallucination_rate":flags/qa_n,
                "pre_change_hallucination_rate":b_flags/b_qa if b_qa else None,"pickup_rate":pickup,
                "engagement_rate":engagement,"pre_change_pickup_rate":before_pickup,
                "pre_change_engagement_rate":before_eng,"kpi_deltas":delta,"guardrail":guard,
                "safety_regression":safety_bad,"kpi_guardrail_failure":kpi_bad}
            if safety_bad or kpi_bad:
                safe_prompt=details.get("safe_prompt")
                credits=int(self.get_json("/sim/state").get("credits_remaining",0))
                if not safe_prompt or credits<2:
                    self.store.audit(ts,wid,"prompt_fix_verification_failed_no_rollback_credit",verification); continue
                vr=self.request("POST","/validation/runs",json={"workflow_id":wid,"prompt_version":safe_prompt,"n":200})
                if not vr.is_success or float(vr.json().get("pass_rate",0))<float(wf.get("governance",{}).get("min_validation_pass_rate",0.97)):
                    self.store.audit(ts,wid,"prompt_rollback_validation_failed",{"verification":verification,"response":vr.text}); continue
                rollback=json.loads(json.dumps(current["config"])); rollback["prompt_version"]=safe_prompt
                pid=self.submit_change(wf,current,rollback,"incident_prompt_rollback",safe_prompt,
                    "Post-apply verification found a safety or KPI guardrail regression; restore the previously observed prompt after validating it.",ts,
                    evidence={"validation_run_id":vr.json().get("validation_run_id")},
                    summary="Restore the previous prompt after post-change safety verification failed.",
                    dry_run=verification,impact_score=0.5,affected_leads=int(wf.get("daily_batch",{}).get("size",0)))
                if pid:
                    details["rollback_proposal_id"]=pid
                    with self.store.db:
                        self.store.db.execute("UPDATE incidents SET status='rollback_pending',details=?,updated_at=? WHERE incident_id=?",(json.dumps(details,sort_keys=True),ts,iid))
                continue
            # A successful verify requires a meaningful amount of post-change traffic.
            details["verification"]=verification
            with self.store.db:
                self.store.db.execute("UPDATE incidents SET status='verified',details=?,updated_at=? WHERE incident_id=?",(json.dumps(details,sort_keys=True),ts,iid))
            self.store.audit(ts,wid,"prompt_fix_verified",verification)

    def detect_caller_id_incidents(self, workflows: list[dict], ts: str) -> None:
        """Compare caller IDs within the recent window and honor dimension-specific human edits."""
        for wf in workflows:
            wid=wf.get("workflow_id",wf.get("id"))
            if not wid or wf.get("mid_flight")!="allowed": continue
            human_caller_edit=self.store.db.execute("""SELECT 1 FROM event_index WHERE workflow_id=?
              AND type='config_changed' AND actor='supervisor' AND
              (changed_dims LIKE '%caller_id%' OR changed_dims LIKE '%allocations%' OR changed_dims LIKE '%cohorts%') LIMIT 1""",(wid,)).fetchone()
            if human_caller_edit: continue
            policy=wf.get("governance",{}).get("auto_apply",{})
            if "caller_id" not in policy.get("allowed_dims",[]): continue
            try: cutoff=(datetime.fromisoformat(ts)-timedelta(hours=24)).strftime("%Y-%m-%dT%H:00")
            except (ValueError,TypeError): continue
            by_id=self.store.db.execute("""SELECT variant,SUM(dials),SUM(connected),SUM(engaged) FROM variant_hourly
              WHERE workflow_id=? AND dimension='caller_id' AND hour_key>=? GROUP BY variant""",(wid,cutoff)).fetchall()
            sampled=[r for r in by_id if r[1]>=100]
            if len(sampled)<2 or sum(r[1] for r in sampled)<300: continue
            rates={r[0]:(r[2] or 0)/r[1] for r in sampled}
            worst=min(sampled,key=lambda r:rates[r[0]])
            other=[r for r in sampled if r[0]!=worst[0]]
            other_n=sum(r[1] for r in other); other_success=sum(r[2] or 0 for r in other)
            if not caller_gap_is_significant(worst[1],worst[2] or 0,other_n,other_success): continue
            incident_id=f"caller-id:{wid}:{worst[0]}"
            existing=self.store.db.execute("SELECT status FROM incidents WHERE incident_id=?",(incident_id,)).fetchone()
            if existing and existing[0] in ("proposal_pending","verify","verified"): continue
            current=self.get_json(f"/workflows/{wid}/config"); cfg=json.loads(json.dumps(current["config"]))
            allocations=cfg.get("allocations",{})
            if any("caller_id" in values for group,values in allocations.items() if group!="default"): continue
            distribution=allocations.get("default",{}).get("caller_id",{})
            if worst[0] not in distribution or len(distribution)<2: continue
            if any(cid not in {str(r[0]) for r in sampled} for cid in distribution): continue
            move=min(float(distribution[worst[0]]),0.2)
            others=[k for k,v in distribution.items() if k!=worst[0] and v>0]
            recipient_total=sum(distribution[k] for k in others)
            if move<=0 or recipient_total<=0: continue
            before_dist=dict(distribution)
            after_dist=dict(distribution)
            for key in others: after_dist[key]+=move*distribution[key]/recipient_total
            after_dist[worst[0]]-=move
            samples={str(cid):(n,connected or 0,engaged or 0) for cid,n,connected,engaged in sampled}
            before_pickup,before_engagement=self._project_kpis(before_dist,samples)
            after_pickup,after_engagement=self._project_kpis(after_dist,samples)
            deltas={"pickup_rate":after_pickup-before_pickup,"engagement_rate":after_engagement-before_engagement}
            primary=wf.get("objective",{}).get("primary")
            guard=wf.get("objective",{}).get("guardrail",{})
            # Operational remediation cannot buy a primary-KPI drop or violate the
            # independently declared guardrail, even when the caller is a clear outlier.
            primary_delta=deltas.get(primary,0)
            guard_name=next(iter(guard),None)
            guard_delta=deltas.get(guard_name.replace("_max_drop",""),0) if guard_name else 0
            guard_limit=float(guard.get(guard_name,0)) if guard_name else 0
            if primary_delta < -0.02 or guard_delta < -guard_limit:
                self.store.audit(ts,wid,"caller_id_rotation_blocked_guardrail",{"caller_id":worst[0],"primary":primary,
                    "deltas":deltas,"guardrail":guard,"projected_primary_delta":primary_delta})
                continue
            for key in others: distribution[key]+=move*distribution[key]/recipient_total
            distribution[worst[0]]-=move
            cfg["allocations"]["default"]["caller_id"]={k:round(v,8) for k,v in distribution.items() if v>1e-8}
            rationale=(f"In the latest 24 simulated hours, caller ID {worst[0]} connected {rates[worst[0]]:.3f} of {worst[1]} calls; "
                       f"other sampled IDs connected {other_success/other_n:.3f} of {other_n}. A 0.2 allocation step stays within policy max shift.")
            proposal_id=self.submit_change(wf,current,cfg,"auto:caller_id_rotation",str(worst[0]),rationale,ts,
                summary="Reduce traffic sent from a caller ID with sustained pickup degradation.",
                dry_run={"window_hours":24,"candidate_dials":worst[1],"candidate_pickup":rates[worst[0]],
                         "comparison_dials":other_n,"comparison_pickup":other_success/other_n,"allocation_step":move},
                impact_score=min(1,max(0,(other_success/other_n-rates[worst[0]])*2)),
                affected_leads=int(wf.get("daily_batch",{}).get("size",0)))
            if proposal_id:
                details={"caller_id":str(worst[0]),"pre_window_hours":24,"pre_dials":int(worst[1]),
                    "pre_pickup":rates[worst[0]],"comparison_dials":int(other_n),
                    "comparison_pickup":other_success/other_n,"pre_engagement":before_engagement,"proposal_id":proposal_id,
                    "pre_population_pickup":before_pickup,"pre_population_engagement":before_engagement,
                    "before_allocation":before_dist,"after_allocation":cfg["allocations"]["default"]["caller_id"],
                    "applied_at":ts,"post_dials":0,"post_connected":0}
                with self.store.db:
                    self.store.db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET status=excluded.status,details=excluded.details,updated_at=excluded.updated_at",
                      (incident_id,wid,"caller_id_degradation","verify",ts,json.dumps(details,sort_keys=True),ts))
                self.store.audit(ts,wid,"caller_id_incident_detected",details)

    def verify_caller_id_incidents(self, workflows: list[dict], ts: str) -> None:
        """Verify rotations after 300 post-change calls, using pickup and engagement separately."""
        for iid,wid,raw in self.store.db.execute("SELECT incident_id,workflow_id,details FROM incidents WHERE kind='caller_id_degradation' AND status='verify'").fetchall():
            details=json.loads(raw); start=details.get("applied_at")
            if not start: continue
            rows=self.store.db.execute("""SELECT SUM(dials),SUM(connected),SUM(engaged) FROM variant_hourly
              WHERE workflow_id=? AND dimension='caller_id' AND hour_key>=?""",(wid,self.store._hour(start))).fetchone()
            if not rows or (rows[0] or 0)<300: continue
            dials,connected,engaged=(int(v or 0) for v in rows)
            pickup=connected/dials if dials else 0
            engagement=engaged/connected if connected else 0
            wf=next((w for w in workflows if w.get('workflow_id',w.get('id'))==wid),{})
            primary=wf.get('objective',{}).get('primary','pickup_rate')
            guard=wf.get('objective',{}).get('guardrail',{})
            guard_name=next(iter(guard),None)
            guard_limit=float(guard.get(guard_name,0)) if guard_name else 0
            guard_metric=guard_name.replace('_max_drop','') if guard_name else None
            pre_primary=details.get('pre_population_pickup',details.get('pre_pickup',0)) if primary=='pickup_rate' else details.get('pre_population_engagement',details.get('pre_engagement',0))
            observed_primary=pickup if primary=='pickup_rate' else engagement
            guard_value=pickup if guard_metric=='pickup_rate' else engagement
            details.update({"post_dials":dials,"post_connected":connected,"post_engaged":engaged,
                "post_pickup":pickup,"post_engagement":engagement,"verified_at":ts})
            guard_baseline=details.get("pre_population_pickup",details.get("pre_pickup",0)) if guard_metric=="pickup_rate" else details.get("pre_population_engagement",details.get("pre_engagement",0))
            if observed_primary < pre_primary-0.02 or (guard_metric and guard_value < guard_baseline-guard_limit):
                details["verification_result"]="degraded"
                wf_config=self.get_json(f"/workflows/{wid}/config")
                proposed=json.loads(json.dumps(wf_config["config"]))
                proposed.setdefault("allocations",{}).setdefault("default",{})["caller_id"]=details["before_allocation"]
                wf=next((w for w in workflows if w.get('workflow_id',w.get('id'))==wid),{})
                rollback_id=self.submit_change(wf,wf_config,proposed,"incident_caller_id_rollback",iid,
                    "Post-change verification found a primary KPI or guardrail regression; restore the previous caller allocation.",ts,
                    summary="Restore the previous caller allocation after a verified KPI regression.",
                    dry_run={"verification":details,"restores_prior_allocation":True},impact_score=0.5,
                    affected_leads=int(wf.get("daily_batch",{}).get("size",0)))
                details["rollback_proposal_id"]=rollback_id
                status="rollback_pending" if rollback_id else "rollback_blocked_by_policy"
            else:
                details["verification_result"]="passed"
                status="verified"
            with self.store.db:
                self.store.db.execute("UPDATE incidents SET status=?,details=?,updated_at=? WHERE incident_id=?",
                    (status,json.dumps(details,sort_keys=True),ts,iid))
            self.store.audit(ts,wid,"caller_id_incident_"+details["verification_result"],details)

    def reconcile_caller_id_incidents(self, ts: str) -> None:
        """Advance incident status from the durable rollback proposal lifecycle."""
        rows=self.store.db.execute("SELECT incident_id,workflow_id,details FROM incidents WHERE kind='caller_id_degradation' AND status='rollback_pending'").fetchall()
        for iid,wid,raw in rows:
            details=json.loads(raw); pid=details.get("rollback_proposal_id")
            proposal=self.store.db.execute("SELECT status,body FROM proposals WHERE proposal_id=?",(pid,)).fetchone() if pid else None
            if not proposal: continue
            pstatus,body=proposal
            if pstatus in ("rejected","expired","lapsed","stale","denied","deferred_fatigue","apply_error"):
                status="rollback_"+("failed" if pstatus=="apply_error" else pstatus)
            elif pstatus=="applied":
                try:
                    current=self.get_json(f"/workflows/{wid}/config")
                    expected=json.loads(body).get("proposed_config")
                except (RuntimeError,httpx.HTTPError,ValueError,KeyError) as exc:
                    self.store.audit(ts,wid,"caller_id_rollback_config_check_failed",{"incident_id":iid,"proposal_id":pid,"error":str(exc)})
                    continue
                status="rolled_back" if current.get("config")==expected else "rollback_config_mismatch"
                self.store.audit(ts,wid,"caller_id_incident_rollback_applied" if status=="rolled_back" else "caller_id_rollback_config_mismatch",
                    {"incident_id":iid,"proposal_id":pid,"expected_config":expected,"observed_config":current.get("config")})
            else:
                continue
            details["rollback_status"]=pstatus
            with self.store.db:
                self.store.db.execute("UPDATE incidents SET status=?,details=?,updated_at=? WHERE incident_id=?",
                    (status,json.dumps(details,sort_keys=True),ts,iid))
            self.store.audit(ts,wid,"caller_id_incident_"+status,{"incident_id":iid,"proposal_id":pid,"proposal_status":pstatus})

    def audit_open_experiments_at_horizon(self, ts: str) -> None:
        rows=self.store.db.execute("SELECT experiment_id,workflow_id,dimension,cohort_id,control,candidate,status,started_at,decision FROM experiments WHERE status IN ('awaiting_start','active','promotion_pending','rollback_pending','promoted','confounded','abort_pending')").fetchall()
        final=datetime.fromisoformat(ts)
        for eid,wid,dim,cohort,control,candidate,status,started,raw_decision in rows:
            decision=json.loads(raw_decision) if raw_decision else {}
            horizon_start=decision.get("promoted_at",started) if status=="promoted" else started
            age=max(0,(final-datetime.fromisoformat(horizon_start)).total_seconds()/3600)
            if status=="active":
                candidate_stats=self._experiment_counts(wid,dim,cohort,candidate,started,ts)
                control_stats=self._experiment_counts(wid,dim,cohort,control,started,ts)
                reason="fixed_48h_horizon_not_elapsed" if age<48 else "evaluation_not_completed_before_simulator_end"
            else:
                candidate_stats=self._experiment_counts(wid,dim,cohort,candidate,horizon_start,ts)
                control_stats=self._experiment_counts(wid,dim,cohort,control,horizon_start,ts)
                reason=("post_promotion_24h_monitoring_horizon_not_elapsed" if status=="promoted"
                    else "proposal_or_abort_lifecycle_pending_at_simulator_end")
            self.store.audit(ts,wid,"experiment_open_at_horizon",{"experiment_id":eid,"dimension":dim,
                "cohort_id":cohort,"control":control,"candidate":candidate,"status":status,
                "elapsed_hours":age,"reason":reason,"candidate_exposure":candidate_stats,
                "control_exposure":control_stats,"decision":"no promotion or rejection; retain observed status"})

    def loop(self) -> None:
        wall_start=time.monotonic()
        state = self.get_json("/sim/state")
        sim_start=state.get("ts","")
        workflows = self.get_json("/workflows")
        LOG.info("starting with %d workflows at %s; durable chunk cursor %d", len(workflows), state.get("ts"), self.store.cursor())
        self.store.audit(str(state.get("ts", "")), None, "controller_started", {"workflows": len(workflows)})
        if self.store.cursor() < 0:
            bootstrap_start=time.monotonic()
            before_chunks=self.store.db.execute("SELECT COUNT(*) FROM event_chunks").fetchone()[0]
            before_events=self.store.db.execute("SELECT COALESCE(SUM(event_count),0) FROM event_chunks").fetchone()[0]
            self.bootstrap()
            bootstrap_metrics=self.store.storage_metrics()
            self.store.audit(str(state.get("ts","")),None,"history_bootstrap_complete",{
                "wall_seconds":time.monotonic()-bootstrap_start,
                "chunks":bootstrap_metrics["chunks"]-before_chunks,
                "events":bootstrap_metrics["events"]-before_events,
                "raw_bytes":bootstrap_metrics["raw_event_bytes"],
                "compressed_bytes":bootstrap_metrics["compressed_chunk_bytes"]})
        # Recover gaps by asking for every chunk after the last durable cursor.
        for kind in ("history", "events"):
            self.consume(self.get_json("/events/chunks", params={"kind": kind, "after": self.store.cursor()}), kind)
        for wf in workflows:
            wid = wf.get("workflow_id", wf.get("id"))
            if wid:
                cfg = self.get_json(f"/workflows/{wid}/config")
                self.store.audit(str(state.get("ts", "")), wid, "workflow_observed", {"version": cfg.get("version"), "primary": wf.get("objective", {}).get("primary"), "guardrail": wf.get("objective", {}).get("guardrail")})
        workflow_map={w.get("workflow_id",w.get("id")):w for w in workflows}
        self.observe_supervisor_edits(str(state.get("ts","")))
        self.reconcile_proposals(workflow_map,str(state.get("ts","")))
        while True:
            body = self.advance_once()
            if body.get("finished"):
                break
            ts = body.get("ts", "")
            batch_metrics=self.consume(body.get("new_chunks", []))
            now=datetime.fromisoformat(ts)
            lags=[]
            for item in batch_metrics:
                if item.get("first_event_ts"):
                    lags.append(max(0,(now-datetime.fromisoformat(item["first_event_ts"])).total_seconds()/60))
            batch_events=sum(x.get("events",0) for x in batch_metrics)
            self.observe_supervisor_edits(ts)
            self.reconcile_proposals(workflow_map,ts)
            self.detect_prompt_incidents(workflows,ts)
            self.handle_prompt_incidents(workflows,ts)
            self.verify_prompt_changes(workflows,ts)
            self.detect_caller_id_incidents(workflows, ts)
            self.verify_caller_id_incidents(workflows, ts)
            self.reconcile_caller_id_incidents(ts)
            self.run_experiments(workflows,ts)
            # Record separate population KPIs for each observed workflow, never combine them.
            for wf in workflows:
                wid = wf.get("workflow_id", wf.get("id"))
                if not wid: continue
                metrics = self.store.db.execute("SELECT dials,connected,engaged FROM workflow_kpis WHERE workflow_id=?", (wid,)).fetchone()
                if metrics:
                    dials, connected, engaged = metrics
                    self.store.audit(ts, wid, "population_kpis", {"dials": dials, "connected": connected or 0, "engaged": engaged or 0,
                      "pickup_rate": (connected or 0) / dials, "engagement_rate": (engaged or 0) / connected if connected else None})
            self.store.audit(ts, None, "loop_checkpoint", {"cursor": self.store.cursor(), "finished": body.get("finished", False),
                "events_ingested":batch_events,"chunk_count":len(batch_metrics),"max_decision_lag_minutes":max(lags,default=0),
                "raw_bytes":sum(x.get("raw_bytes",0) for x in batch_metrics),
                "compressed_bytes":sum(x.get("compressed_bytes",0) for x in batch_metrics)})
            if body.get("finished"):
                break
        final_ts = str(self.get_json("/sim/state").get("ts", ""))
        total_wall=time.monotonic()-wall_start
        try: simulated_days=max(0,(datetime.fromisoformat(final_ts)-datetime.fromisoformat(sim_start)).total_seconds()/86400)
        except ValueError: simulated_days=0
        self.audit_open_experiments_at_horizon(final_ts)
        metrics=self.store.storage_metrics()
        self.store.audit(final_ts, None, "controller_finished", {"last_chunk": self.store.cursor(),"wall_seconds":total_wall,
            "simulated_days":simulated_days,"wall_seconds_per_simulated_day":total_wall/simulated_days if simulated_days else None,
            "events_per_second":metrics["events"]/total_wall if total_wall else None,"storage":metrics})
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
