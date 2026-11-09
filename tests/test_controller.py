import json
import sqlite3
import tracemalloc
import zlib

from controller import Controller, Store, caller_gap_is_significant


def test_event_ingestion_is_idempotent_and_restartable(tmp_path):
    db_path = str(tmp_path / "state.sqlite3")
    payload = {"event_id": 41, "type": "call", "workflow_id": "wf", "ts": "D0", "connected": True, "engaged": False}
    with Store(db_path) as store:
        assert store.save_chunk(7, json.dumps(payload))["indexed"] == 1
    with Store(db_path) as reopened:
        assert reopened.cursor() == 7
        assert reopened.save_chunk(7, json.dumps(payload))["duplicate_chunk"]
        assert reopened.db.execute("SELECT COUNT(*) FROM event_ids").fetchone()[0] == 1
        assert reopened.db.execute("SELECT event_id FROM event_ids").fetchone()[0] == 41
        archived = reopened.db.execute("SELECT compressed FROM event_chunks WHERE chunk=7").fetchone()[0]
        assert json.loads(zlib.decompress(archived).decode()) == payload


def test_separate_kpis_are_computed_without_blending(tmp_path):
    store = Store(str(tmp_path / "state.sqlite3"))
    rows = [
        {"event_id": 1, "type": "call", "workflow_id": "wf", "connected": True, "engaged": True},
        {"event_id": 2, "type": "call", "workflow_id": "wf", "connected": True, "engaged": False},
        {"event_id": 3, "type": "call", "workflow_id": "wf", "connected": False, "engaged": False},
    ]
    store.save_chunk(0, "\n".join(map(json.dumps, rows)))
    dials, connected, engaged = store.db.execute("SELECT dials,connected,engaged FROM workflow_kpis WHERE workflow_id=?", ("wf",)).fetchone()
    assert (dials, connected, engaged) == (3, 2, 1)
    assert connected / dials == 2 / 3
    assert engaged / connected == 1 / 2


def test_caller_outlier_requires_population_and_large_confidence_gap():
    assert not caller_gap_is_significant(20, 5, 20, 15)
    assert not caller_gap_is_significant(100, 45, 100, 55)
    assert caller_gap_is_significant(1000, 250, 2000, 1100)


def test_experiment_checks_primary_and_guardrail_independently(tmp_path):
    with Store(str(tmp_path / "experiment.sqlite3")) as store:
        controller = Controller("http://unused", store)
        wf = {"objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}}}
        result = controller._compare_experiment(wf, (400, 120, 108), (400, 200, 140))
        assert result["minimum_sample"]
        assert result["primary_wins"]
        assert not result["guardrail_passes"]
        small = controller._compare_experiment(wf, (49, 30, 24), (400, 200, 140))
        assert not small["minimum_sample"]
        assert not small["primary_wins"]
        guard_sample=controller._compare_experiment(
            {"objective":{"primary":"pickup_rate","guardrail":{"engagement_rate_max_drop":0.02}}},
            (400,80,40),(400,200,120))
        assert not guard_sample["minimum_sample"]
        corrected=controller._compare_experiment(wf,(400,200,180),(400,200,140),multiplicity=5)
        assert corrected["primary_threshold"]>2.58
        monitor,regressed=controller._promotion_monitor(wf,{"candidate":(1000,500,300)},(400,120,108),"t0","t1")
        assert monitor["primary_regression"] is False
        assert monitor["guardrail_regression"] is True
        assert regressed


def test_experiment_at_scenario_horizon_stays_undecided_and_audits_exposure(tmp_path):
    with Store(str(tmp_path/"horizon.sqlite3")) as store:
        store.db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("exp","wf","welcome_message","default","W1","W3",0.2,1,"active",
             "2027-01-12T16:00:00+05:30",None,None))
        for variant,dials,connected,engaged in (("W1",4068,1025,581),("W3",1032,268,180)):
            store.db.execute("INSERT INTO variant_hourly VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("wf","welcome_message","default","2027-01-13T00:00",variant,dials,connected,engaged,0,0))
        Controller("http://unused",store).audit_open_experiments_at_horizon("2027-01-14T00:00:00+05:30")
        assert store.db.execute("SELECT status FROM experiments WHERE experiment_id='exp'").fetchone()[0]=="active"
        entry=json.loads(store.db.execute("SELECT details FROM audit WHERE action='experiment_open_at_horizon'").fetchone()[0])
        assert entry["reason"]=="fixed_48h_horizon_not_elapsed"
        assert entry["elapsed_hours"]==32
        assert entry["candidate_exposure"][0]==1032


def test_supervisor_voice_edit_aborts_pending_test_without_rewriting_config(tmp_path):
    with Store(str(tmp_path / "supervisor.sqlite3")) as store:
        store.db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("exp", "wf", "voice", "default", "V1", "V2", 0.2, 1, "awaiting_start", "t0", None, None))
        store.db.execute("INSERT INTO event_index VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (8, 2, "config_changed", "wf", "t1", "supervisor", None, None, None, 2, '["voice"]'))
        Controller("http://unused", store).observe_supervisor_edits("t1")
        row=store.db.execute("SELECT status,decision FROM experiments WHERE experiment_id='exp'").fetchone()
        assert row == ("aborted_supervisor_edit", "supervisor changed voice configuration")
        assert store.db.execute("SELECT action FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0] == "supervisor_config_observed"


def test_confounded_experiment_terminalizes_after_abort_proposal_applies(tmp_path,monkeypatch):
    with Store(str(tmp_path/"abort.sqlite3")) as store:
        with store.db:
            store.db.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("exp","wf","voice","default","V1","V2",0.2,1,"abort_pending","2027-01-04T10:00:00+05:30",None,
                 json.dumps({"proposal_id":"abort"})))
            store.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("abort","wf",None,"applied",2,"auto:experiment_abort","exp",
                 json.dumps({"proposed_config":{"allocations":{"default":{"voice":{"V1":1.0}}}}}),"t0","t1"))
        ctl=Controller("http://unused",store)
        monkeypatch.setattr(ctl,"get_json",lambda *_a,**_k:{"version":2,"config":{"allocations":{"default":{"voice":{"V1":1.0}}}}})
        ctl.run_experiments([{"workflow_id":"wf","mid_flight":"locked","options":{}}],"2027-01-04T12:00:00+05:30")
        assert store.db.execute("SELECT status FROM experiments WHERE experiment_id='exp'").fetchone()[0]=="aborted_confounded"


def test_compact_storage_keeps_raw_audit_and_reports_size(tmp_path):
    db_path = str(tmp_path / "compact.sqlite3")
    events = [{"event_id":i,"type":"call","workflow_id":"wf","ts":"2027-01-04T10:00:00+05:30",
               "variant":{"voice":"V1","caller_id":"D1","prompt_version":"P1"},
               "connected":True,"engaged":False,"qa":{"hallucination_flag":False}} for i in range(1,501)]
    raw = "\n".join(json.dumps(e,separators=(",",":")) for e in events)
    with Store(db_path) as store:
        result = store.save_chunk(4,raw)
        metrics = store.storage_metrics()
        assert result["events"] == 500
        assert metrics["raw_event_bytes"] == len((raw+"\n").encode())
        assert metrics["compressed_chunk_bytes"] < metrics["raw_event_bytes"]
        assert store.db.execute("SELECT COUNT(*) FROM event_ids").fetchone()[0] == 500


def test_synthetic_ingest_is_iterated_with_bounded_python_memory(tmp_path):
    path = str(tmp_path / "synthetic.sqlite3")
    n = 6000
    def lines():
        for i in range(n):
            yield json.dumps({"event_id":i+1,"type":"call","workflow_id":"wf",
                "ts":"2027-01-04T10:00:00+05:30","variant":{"caller_id":"D1"},
                "connected":True,"engaged":False},separators=(",",":"))
    tracemalloc.start()
    with Store(path) as store:
        result=store.save_chunk_lines(1,"events",lines())
        metrics=store.storage_metrics()
        _,peak=tracemalloc.get_traced_memory()
        assert result["events"] == n
        assert metrics["events"] == n
        assert metrics["max_events_per_chunk"] == n
        assert metrics["raw_bytes_per_million_events"] > 0
        assert metrics["db_page_bytes"] < 8 * 1024 * 1024
        # The input generator and event dictionaries are processed one at a time.
        assert peak < 8 * 1024 * 1024
    tracemalloc.stop()


def test_legacy_database_migrates_without_losing_raw_events(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE events(event_id INTEGER PRIMARY KEY,chunk INTEGER,type TEXT,workflow_id TEXT,ts TEXT,payload TEXT);
      CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE workflow_kpis(workflow_id TEXT PRIMARY KEY,dials INTEGER,connected INTEGER,engaged INTEGER);
      CREATE TABLE caller_stats(workflow_id TEXT,caller_id TEXT,dials INTEGER,connected INTEGER,PRIMARY KEY(workflow_id,caller_id));
      INSERT INTO meta VALUES('chunk','0');
      INSERT INTO events VALUES(1,0,'call','wf','2027-01-04T10:00:00+05:30','{"event_id":1,"type":"call","workflow_id":"wf","ts":"2027-01-04T10:00:00+05:30","connected":true,"engaged":true}');
      INSERT INTO workflow_kpis VALUES('wf',1,1,1);""")
    db.commit(); db.close()
    with Store(path) as migrated:
        assert migrated.db.execute("SELECT dials,connected,engaged FROM workflow_kpis WHERE workflow_id='wf'").fetchone() == (1,1,1)
        assert migrated.db.execute("SELECT COUNT(*) FROM event_ids").fetchone()[0] == 1
        assert migrated.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='events'").fetchone()[0] == 0


class _Response:
    def __init__(self, payload=None, status=200):
        self.payload=payload or {}; self.status_code=status; self.is_success=status<400; self.text=json.dumps(self.payload)
    def json(self): return self.payload


def _rollback_fixture(path, monkeypatch, auto=True, fail_first=False):
    store=Store(str(path))
    wf={"workflow_id":"wf","mid_flight":"allowed","daily_batch":{"size":400},
        "objective":{"primary":"engagement_rate","guardrail":{"pickup_rate_max_drop":0.02}},
        "governance":{"max_requests_per_24h":4,"auto_apply":{"enabled":auto,
            "allowed_dims":["caller_id"] if auto else [],"max_shift":0.2}}}
    before={"allocations":{"default":{"caller_id":{"D1":0.8,"D2":0.2}}}}
    after={"allocations":{"default":{"caller_id":{"D1":1.0}}}}
    live={"version":5,"config":after}
    ctl=Controller("http://unused",store)
    def get_json(path,**kwargs):
        if path=="/workflows/wf/config": return json.loads(json.dumps(live))
        if path=="/approvals": return []
        if path=="/approvals/app1": return {"approval_id":"app1","status":"rejected"}
        raise AssertionError(f"unexpected GET {path}")
    attempts={"put":0}
    def request(method,path,**kwargs):
        if method=="PUT" and path=="/workflows/wf/config":
            attempts["put"]+=1
            if fail_first and attempts["put"]==1: raise RuntimeError("simulated connection loss")
            payload=kwargs["json"]
            live["config"]=payload["proposed_config"]; live["version"]+=1
            return _Response({"version":live["version"],"config":live["config"]})
        if method=="POST" and path=="/approvals":
            return _Response({"approval_id":"app1","status":"pending"},201)
        raise AssertionError(f"unexpected {method} {path}")
    monkeypatch.setattr(ctl,"get_json",get_json)
    monkeypatch.setattr(ctl,"request",request)
    start="2027-01-04T10:00:00+05:30"
    details={"caller_id":"D2","pre_pickup":0.30,"pre_engagement":0.60,
        "pre_population_pickup":0.50,"pre_population_engagement":0.60,
        "before_allocation":{"D1":0.8,"D2":0.2},"after_allocation":{"D1":1.0},"applied_at":start}
    with store.db:
        store.db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?)",
            ("caller-id:wf:D2","wf","caller_id_degradation","verify",start,json.dumps(details),start))
    calls=[{"event_id":i+1,"type":"call","workflow_id":"wf","ts":f"2027-01-04T{11+i//100:02d}:00:00+05:30",
        "variant":{"caller_id":"D1"},"connected":i%5<2,"engaged":i%20==0} for i in range(300)]
    store.save_chunk(1,"\n".join(map(json.dumps,calls)))
    return store,ctl,wf,live,before,after,attempts


def test_caller_rollback_is_applied_verified_and_idempotent_after_restart(tmp_path,monkeypatch):
    store,ctl,wf,live,before,after,attempts=_rollback_fixture(tmp_path/"rollback.sqlite3",monkeypatch)
    ts="2027-01-04T15:00:00+05:30"
    ctl.verify_caller_id_incidents([wf],ts)
    ctl.reconcile_caller_id_incidents(ts)
    incident=store.db.execute("SELECT status,details FROM incidents WHERE incident_id='caller-id:wf:D2'").fetchone()
    assert incident[0]=="rolled_back"
    assert live["config"]==before
    details=json.loads(incident[1]); proposal_id=details["rollback_proposal_id"]
    proposal=store.db.execute("SELECT status,body FROM proposals WHERE proposal_id=?",(proposal_id,)).fetchone()
    assert proposal[0]=="applied"
    body=json.loads(proposal[1]); assert body["proposed_config"]==before
    actions=[r[0] for r in store.db.execute("SELECT action FROM audit")]
    assert "caller_id_incident_degraded" in actions
    assert "proposal_prepared" in actions and "proposal_applied" in actions
    assert "caller_id_incident_rolled_back" in actions
    store.db.close()
    reopened=Store(str(tmp_path/"rollback.sqlite3")); ctl2=Controller("http://unused",reopened)
    monkeypatch.setattr(ctl2,"get_json",ctl.get_json)
    ctl2.reconcile_caller_id_incidents(ts)
    assert reopened.db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]==1
    assert reopened.db.execute("SELECT status FROM incidents WHERE incident_id='caller-id:wf:D2'").fetchone()[0]=="rolled_back"
    assert attempts["put"]==1


def test_caller_rollback_retries_uncertain_apply_after_restart(tmp_path,monkeypatch):
    store,ctl,wf,live,before,after,attempts=_rollback_fixture(tmp_path/"retry.sqlite3",monkeypatch,fail_first=True)
    ts="2027-01-04T15:00:00+05:30"
    ctl.verify_caller_id_incidents([wf],ts)
    ctl.reconcile_caller_id_incidents(ts)
    proposal_id=json.loads(store.db.execute("SELECT details FROM incidents WHERE incident_id='caller-id:wf:D2'").fetchone()[0])["rollback_proposal_id"]
    assert store.db.execute("SELECT status FROM proposals WHERE proposal_id=?",(proposal_id,)).fetchone()[0]=="uncertain"
    store.db.close()
    reopened=Store(str(tmp_path/"retry.sqlite3")); ctl2=Controller("http://unused",reopened)
    monkeypatch.setattr(ctl2,"get_json",ctl.get_json); monkeypatch.setattr(ctl2,"request",ctl.request)
    ctl2.reconcile_proposals({"wf":wf},"2027-01-04T16:00:00+05:30")
    ctl2.reconcile_caller_id_incidents("2027-01-04T16:00:00+05:30")
    assert live["config"]==before
    assert reopened.db.execute("SELECT status FROM proposals WHERE proposal_id=?",(proposal_id,)).fetchone()[0]=="applied"
    assert reopened.db.execute("SELECT status FROM incidents WHERE incident_id='caller-id:wf:D2'").fetchone()[0]=="rolled_back"
    assert attempts["put"]==2


def test_caller_rollback_obeys_approval_rejection_and_stale_version(tmp_path,monkeypatch):
    store,ctl,wf,live,before,after,_=_rollback_fixture(tmp_path/"approval.sqlite3",monkeypatch,auto=False)
    ts="2027-01-04T15:00:00+05:30"
    ctl.verify_caller_id_incidents([wf],ts)
    details=json.loads(store.db.execute("SELECT details FROM incidents WHERE incident_id='caller-id:wf:D2'").fetchone()[0])
    pid=details["rollback_proposal_id"]
    assert live["config"]==after
    assert store.db.execute("SELECT status FROM proposals WHERE proposal_id=?",(pid,)).fetchone()[0]=="pending"
    ctl.reconcile_proposals({"wf":wf},ts); ctl.reconcile_caller_id_incidents(ts)
    assert store.db.execute("SELECT status FROM proposals WHERE proposal_id=?",(pid,)).fetchone()[0]=="rejected"
    assert store.db.execute("SELECT status FROM incidents WHERE incident_id='caller-id:wf:D2'").fetchone()[0]=="rollback_rejected"

    # A second, approved rollback must lapse as stale if the live version changes first.
    live["version"]+=1; live["config"]={"allocations":{"default":{"caller_id":{"D1":1.0},"voice":{"V2":1.0}}}}
    with store.db: store.db.execute("UPDATE proposals SET status='approved' WHERE proposal_id=?",(pid,))
    monkeypatch.setattr(ctl,"get_json",lambda path,**kw: {"approval_id":"app1","status":"approved"} if path=="/approvals/app1" else live)
    ctl.reconcile_proposals({"wf":wf},"2027-01-04T16:00:00+05:30")
    assert live["config"]["allocations"]["default"]["voice"]=={"V2":1.0}
    assert store.db.execute("SELECT status FROM proposals WHERE proposal_id=?",(pid,)).fetchone()[0]=="stale"


def test_prompt_rollback_proposal_advances_incident_only_after_config_matches(tmp_path,monkeypatch):
    with Store(str(tmp_path/"prompt.sqlite3")) as store:
        body={"proposal_id":"rollback","workflow_id":"wf","base_version":4,
            "proposed_config":{"prompt_version":"P1"},"rationale":"restore validated prompt"}
        with store.db:
            store.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("rollback","wf","app1","applied",4,"incident_prompt_rollback","P1",json.dumps(body),"t0","t1"))
            details={"rollback_proposal_id":"rollback","safe_prompt":"P1"}
            store.db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?)",
                ("incident","wf","supervisor_prompt_change","rollback_pending","t0",json.dumps(details),"t1"))
        ctl=Controller("http://unused",store)
        monkeypatch.setattr(ctl,"get_json",lambda *_a,**_k:{"config":{"prompt_version":"P1"}})
        ctl.verify_prompt_changes([{"workflow_id":"wf"}],"t2")
        assert store.db.execute("SELECT status FROM incidents WHERE incident_id='incident'").fetchone()[0]=="rolled_back"
        assert store.db.execute("SELECT action FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0]=="prompt_incident_rolled_back"
