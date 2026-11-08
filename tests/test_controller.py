import json

from controller import Store, caller_gap_is_significant


def test_event_ingestion_is_idempotent_and_restartable(tmp_path):
    db_path = str(tmp_path / "state.sqlite3")
    payload = {"event_id": 41, "type": "call", "workflow_id": "wf", "ts": "D0", "connected": True, "engaged": False}
    with Store(db_path) as store:
        assert store.save_chunk(7, json.dumps(payload)) == 1
    with Store(db_path) as reopened:
        assert reopened.cursor() == 7
        assert reopened.save_chunk(7, json.dumps(payload)) == 0
        assert reopened.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert reopened.db.execute("SELECT event_id FROM events").fetchone()[0] == 41


def test_separate_kpis_are_computed_without_blending(tmp_path):
    store = Store(str(tmp_path / "state.sqlite3"))
    rows = [
        {"event_id": 1, "type": "call", "workflow_id": "wf", "connected": True, "engaged": True},
        {"event_id": 2, "type": "call", "workflow_id": "wf", "connected": True, "engaged": False},
        {"event_id": 3, "type": "call", "workflow_id": "wf", "connected": False, "engaged": False},
    ]
    store.save_chunk(0, "\n".join(map(json.dumps, rows)))
    dials, connected, engaged = store.db.execute("""SELECT COUNT(*), SUM(json_extract(payload,'$.connected')),
      SUM(json_extract(payload,'$.engaged')) FROM events WHERE workflow_id=?
      AND json_extract(payload,'$.type')='call'""", ("wf",)).fetchone()
    assert (dials, connected, engaged) == (3, 2, 1)
    assert connected / dials == 2 / 3
    assert engaged / connected == 1 / 2


def test_caller_outlier_requires_population_and_large_confidence_gap():
    assert not caller_gap_is_significant(20, 5, 20, 15)
    assert not caller_gap_is_significant(100, 45, 100, 55)
    assert caller_gap_is_significant(1000, 250, 2000, 1100)
