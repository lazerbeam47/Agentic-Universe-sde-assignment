import json
import sqlite3
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
