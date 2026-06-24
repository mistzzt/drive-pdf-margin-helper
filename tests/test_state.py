import pytest

from scribe_crop.state import Outcome, StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


def test_insert_and_get(store):
    store.upsert("papers/foo.pdf", "fp1", Outcome.SUCCESS)
    rec = store.get("papers/foo.pdf")
    assert rec is not None
    assert rec.relpath == "papers/foo.pdf"
    assert rec.fingerprint == "fp1"
    assert rec.outcome is Outcome.SUCCESS
    assert rec.created_at == rec.updated_at


def test_get_missing_returns_none(store):
    assert store.get("nope.pdf") is None


def test_upsert_updates_fingerprint_and_preserves_created(tmp_path):
    clock = [100.0]
    store = StateStore(tmp_path / "state.db", now=lambda: clock[0])
    store.upsert("a.pdf", "fp1", Outcome.SUCCESS)
    clock[0] = 200.0
    store.upsert("a.pdf", "fp2", Outcome.CONTENT_FAILURE)
    rec = store.get("a.pdf")
    assert rec.fingerprint == "fp2"
    assert rec.outcome is Outcome.CONTENT_FAILURE
    assert rec.created_at == 100.0
    assert rec.updated_at == 200.0
    store.close()


def test_list_all_sorted(store):
    store.upsert("b.pdf", "fp", Outcome.SUCCESS)
    store.upsert("a.pdf", "fp", Outcome.SUCCESS)
    paths = [r.relpath for r in store.list_all()]
    assert paths == ["a.pdf", "b.pdf"]


def test_delete(store):
    store.upsert("a.pdf", "fp", Outcome.SUCCESS)
    assert store.delete("a.pdf") is True
    assert store.get("a.pdf") is None
    assert store.delete("a.pdf") is False


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "state.db"
    s1 = StateStore(path)
    s1.upsert("x.pdf", "fp", Outcome.SUCCESS)
    s1.close()
    s2 = StateStore(path)
    assert s2.get("x.pdf").fingerprint == "fp"
    s2.close()
