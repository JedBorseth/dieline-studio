"""Phase 1d: manifests, multi-part exports, whole-or-nothing publishing, recovery and reopening."""

import io
import json
import os
import socket
import time
from pathlib import Path

import pytest

from dieline_engine import exports, publish
from dieline_engine.designs import BoxParams
from dieline_engine.exports import ExportPart
from dieline_engine.settings import SettingsStore

from _moves import hook_moves

BOX = BoxParams(style="reg-glio-nx", length=14, width=8, depth=6)
LID = BoxParams(style="hsc-gl", length=14.5, width=8.5, depth=2)


def two(piece_a=None, piece_b=None):
    return [ExportPart("p1", "body", BOX, 2, piece_a), ExportPart("p2", "lid", LID, 2, piece_b)]


def files(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


class Crash(BaseException):
    """The app dying mid-export: nothing after it runs, not even the rollback."""


# ------------------------------------------------------------------ files and the manifest


def test_several_parts_get_one_file_each_and_one_manifest(tmp_path):
    out = exports.export_design(two(), tmp_path, "RL26-0100", ["cf2", "dxf"], design_id="d1", revision=3)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "RL26-0100-p1.cf2", "RL26-0100-p1.dxf", "RL26-0100-p2.cf2", "RL26-0100-p2.dxf", "RL26-0100.dieline.json"]
    record = json.loads((tmp_path / "RL26-0100.dieline.json").read_text())
    assert record["version"] == 2 and record["design_id"] == "d1" and record["revision"] == 3
    assert record["txn"] and record["sample_table"] is None
    for part in record["parts"]:
        assert part["quantity"] == 2 and part["units"] == "in" and part["cf2"].startswith("$BOF")
        for f in part["files"]:
            assert publish.file_digest(Path(f["path"])) == f["sha256"] and f["destination"] == "main"
    assert [f["part_id"] for f in out["files"]] == ["p1", "p1", "p2", "p2"]


def test_piece_numbers_name_the_files_and_must_differ(tmp_path):
    exports.export_design(two("101", "102"), tmp_path, "job")
    assert {p.name for p in tmp_path.glob("*.cf2")} == {"job-101.cf2", "job-102.cf2"}
    with pytest.raises(publish.PublishError, match="both be written"):
        exports.export_design(two("7", "7"), tmp_path / "b", "job")
    with pytest.raises(publish.PublishError, match="both be written"):  # Windows sees one name
        exports.export_design(two("A", "a"), tmp_path / "c", "job")
    assert not (tmp_path / "b").exists() and not (tmp_path / "c").exists()


def test_a_path_another_job_owns_is_never_touched(tmp_path):
    exports.export_design(two(), tmp_path, "A")
    before = files(tmp_path)
    with pytest.raises(publish.OwnedByAnother, match="belongs to the export A"):
        exports.export(BOX, tmp_path, "A-p1", overwrite=True)
    assert files(tmp_path) == before


def test_a_hand_edited_manifest_cant_pull_in_other_files(tmp_path):
    exports.export(BOX, tmp_path, "job")
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    for victim in (tmp_path / "other.cf2", elsewhere / "job.cf2"):
        victim.write_text("someone else's")
        m = tmp_path / "job.dieline.json"
        record = json.loads(m.read_text())
        record["parts"][0]["files"].append({"format": "cf2", "path": str(victim), "sha256": "0" * 64,
                                            "destination": "main"})
        m.write_text(json.dumps(record))
        with pytest.raises(publish.PublishError, match="edited or damaged"):
            exports.export(BOX, tmp_path, "job", overwrite=True)
        assert victim.read_text() == "someone else's"
        record["parts"][0]["files"].pop()
        m.write_text(json.dumps(record))
    assert not (elsewhere.parent / ".unrelated.dieline-pending").exists()  # not even locked


def test_a_file_changed_since_export_blocks_the_replacement(tmp_path):
    exports.export(BOX, tmp_path, "job", ["cf2", "dxf"])
    (tmp_path / "job.dxf").write_text("edited by hand")
    before = files(tmp_path)
    with pytest.raises(publish.ChangedOnDisk, match="job.dxf changed"):
        exports.export(BOX, tmp_path, "job", ["cf2"], overwrite=True)  # it would stay behind as stale
    assert files(tmp_path) == before


def test_unowned_files_need_the_designer(tmp_path):
    (tmp_path / "job.cf2").write_text("from before the manifests")
    with pytest.raises(publish.ExportExists) as exc:
        exports.export(BOX, tmp_path, "job")
    assert exc.value.names == ["job.cf2"]
    out = exports.export(BOX, tmp_path, "job", overwrite=True)
    assert any(p.endswith("job.cf2") for p in out["superseded"])
    assert (tmp_path / "job.cf2").read_text().startswith("$BOF")


def test_a_v1_set_is_replaced_only_when_confirmed(tmp_path):
    exports.export(BOX, tmp_path, "job")
    m = tmp_path / "job.dieline.json"
    rec = json.loads(m.read_text())
    m.write_text(json.dumps({"format": "dieline-studio-job", "version": 1, "params": rec["parts"][0]["params"],
                             "files": [str(tmp_path / "job.cf2")], "stem": "job"}))
    (tmp_path / "job.cf2").write_text("v1 file, no hash to compare")
    with pytest.raises(publish.ExportExists):
        exports.export(BOX, tmp_path, "job")
    exports.export(BOX, tmp_path, "job", overwrite=True)
    assert json.loads(m.read_text())["version"] == 2


# ------------------------------------------------------------------ the sample table


def test_one_new_sample_file_lands_at_once_and_nothing_else_appears_there(tmp_path):
    cad, samples = tmp_path / "cad", tmp_path / "samples"
    out = exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=samples)
    assert out["sample_table"]["status"] == "published"
    assert [p.name for p in samples.iterdir()] == ["job.dxf"]
    pending = publish.pending_dir(samples)
    # Beside the folder: only the ownership index is left (no lock, journal or staging).
    assert pending.parent == samples.parent and [p.name for p in pending.iterdir()] == [publish.INDEX]


def test_several_sample_files_wait_for_the_designer(tmp_path):
    cad, samples = tmp_path / "cad", tmp_path / "samples"
    seen = []
    out = exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=samples, on_pending=seen.append)
    st = out["sample_table"]
    assert st["status"] == "pending" and st["reason"] == "several files must arrive together"
    assert seen and seen[0]["txn"] == st["txn"] and seen[0]["job_path"] == out["job_path"]
    assert not list(samples.iterdir())  # nothing half-arrived for the table to pick up
    waiting = publish.pending_dir(samples) / st["txn"]
    assert sorted(p.name for p in waiting.iterdir()) == [publish.OWNER, "0-job-p1.dxf", "1-job-p2.dxf"]
    assert exports.open_job(json.loads(Path(out["job_path"]).read_text()))["set_status"] == "pending"
    done = exports.pickup(Path(out["job_path"]), st["txn"])
    assert done["status"] == "published" and sorted(p.name for p in samples.iterdir()) == ["job-p1.dxf",
                                                                                          "job-p2.dxf"]
    record = json.loads(Path(out["job_path"]).read_text())
    assert record["sample_table"]["status"] == "published"
    assert exports.open_job(record)["set_status"] == "complete"
    assert exports.pickup(Path(out["job_path"]), st["txn"])["status"] == "published"  # a repeat is harmless
    assert not (publish.pending_dir(samples) / st["txn"]).exists()


def test_a_newer_export_drops_a_pickup_still_waiting(tmp_path):
    cad, samples = tmp_path / "cad", tmp_path / "samples"
    first = exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=samples)
    second = exports.export_design(two(), cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=samples)
    assert not (publish.pending_dir(samples) / first["sample_table"]["txn"]).exists()
    with pytest.raises(publish.PublishError, match="replaced by a newer export"):
        exports.pickup(Path(first["job_path"]), first["sample_table"]["txn"])
    exports.pickup(Path(second["job_path"]), second["sample_table"]["txn"])
    assert len(list(samples.glob("*.dxf"))) == 2


def test_a_pickup_rechecks_the_table_folder(tmp_path):
    cad, samples = tmp_path / "cad", tmp_path / "samples"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=samples)
    out = exports.export(BOX.model_copy(update={"length": 16}), cad, "job", ["cf2", "dxf"], overwrite=True,
                         dxf_dir=samples)
    assert out["sample_table"]["status"] == "pending"
    (samples / "job.dxf").write_text("someone edited it at the table")
    with pytest.raises(publish.ChangedOnDisk):
        exports.pickup(Path(out["job_path"]), out["sample_table"]["txn"])
    assert (samples / "job.dxf").read_text() == "someone edited it at the table"


def test_a_sample_folder_cant_be_a_drive_root(tmp_path):
    with pytest.raises(publish.PublishError, match="drive root"):
        publish.pending_dir(Path(tmp_path.anchor))


# ------------------------------------------------------------------ locks


def _lock(root: Path, **owner) -> None:
    root.mkdir(parents=True, exist_ok=True)
    where = publish.lock_dir(root)
    where.mkdir(parents=True, exist_ok=True)
    (where / publish.LOCK).write_text(json.dumps({"host": socket.gethostname(), "pid": os.getpid(),
                                                   "time": time.time(), "id": "x", **owner}))


def test_a_lock_another_workstation_holds_makes_the_export_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "LOCK_WAIT_S", 0.2)
    _lock(tmp_path, host="other-pc", pid=1)
    with pytest.raises(publish.LockHeld) as exc:
        exports.export(BOX, tmp_path, "job")
    assert not exc.value.stale and "other-pc" in str(exc.value)
    with pytest.raises(publish.LockHeld):  # a live lock is never broken
        exports.export(BOX, tmp_path, "job", break_lock="x")
    assert not (tmp_path / "job.cf2").exists()


def test_an_abandoned_lock_is_broken_only_when_the_designer_says(tmp_path):
    dead = 2**22 + 12345  # beyond any pid this machine has handed out
    _lock(tmp_path, pid=dead)
    with pytest.raises(publish.LockHeld) as exc:
        exports.export(BOX, tmp_path, "job")
    assert exc.value.stale and "abandoned" in str(exc.value) and exc.value.lock_id == "x"
    with pytest.raises(publish.LockHeld):  # the confirmation names one lock; it can't break another
        exports.export(BOX, tmp_path, "job", break_lock="some-other-lock")
    exports.export(BOX, tmp_path, "job", break_lock=exc.value.lock_id)
    assert (tmp_path / "job.cf2").exists() and not (publish.lock_dir(tmp_path) / publish.LOCK).exists()
    _lock(tmp_path, host="other-pc", pid=1, time=time.time() - 3600)
    with pytest.raises(publish.LockHeld) as exc:
        exports.export(BOX, tmp_path, "job2")
    assert exc.value.stale


def test_rpc_reports_a_lock_with_its_own_code(tmp_path, monkeypatch):
    from dieline_engine.rpc import EXPORT_LOCK_STALE, Server

    monkeypatch.setattr(publish, "LOCK_WAIT_S", 0.1)
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"output_dir": str(tmp_path / "out")})
    out = io.StringIO()
    server = Server(out, store)
    _lock(tmp_path / "out", pid=2**22 + 12345)
    server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "box.export",
                      "params": {"params": BOX.model_dump(), "filename": "job"}})
    err = json.loads(out.getvalue().splitlines()[-1])["error"]
    assert err["code"] == EXPORT_LOCK_STALE and err["data"]["stale"] is True and err["data"]["lock_id"] == "x"


# ------------------------------------------------------------------ crashes and recovery


def _crash_at_commit(monkeypatch, cad: Path):
    rename = publish.os.replace

    def replace(src, dst):
        if Path(dst) == cad / "job.dieline.json" and STAGING in str(src):
            raise Crash()
        rename(src, dst)

    def no_rollback(_moves):
        raise Crash()

    monkeypatch.setattr(publish.os, "replace", replace)
    monkeypatch.setattr(publish, "_rollback", no_rollback)
    return rename


STAGING = publish.STAGING


def test_a_crash_before_the_commit_is_undone(tmp_path, monkeypatch):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job", ["cf2", "dxf"])
    before = files(cad)
    real_rollback = publish._rollback
    rename = _crash_at_commit(monkeypatch, cad)
    with pytest.raises(Crash):
        exports.export(BOX.model_copy(update={"length": 16}), cad, "job", ["cf2", "dxf"], overwrite=True)
    monkeypatch.setattr(publish.os, "replace", rename)
    monkeypatch.setattr(publish, "_rollback", real_rollback)
    assert (cad / "job.dieline.pending.json").exists()  # what the crash left
    record = json.loads((cad / "job.dieline.json").read_text())
    assert exports.open_job(record)["set_status"] == "incomplete"
    notes = exports.recover_all(cad, None)
    assert notes == ["An interrupted export of job in the export folder was undone; the earlier files are back."]
    after = {p: b for p, b in files(cad).items() if "superseded" not in p.parts}
    assert after == {p: b for p, b in before.items()}
    assert exports.recover_all(cad, None) == []  # nothing left to do
    assert not (cad / STAGING).exists() or not list((cad / STAGING).iterdir())


def test_recovery_is_harmless_to_repeat(tmp_path, monkeypatch):
    cad = tmp_path / "cad"
    exports.export_design(two(), cad, "job")
    before = files(cad)
    real_rollback, real_undo = publish._rollback, publish._undo_copy
    rename = _crash_at_commit(monkeypatch, cad)
    # p1 unchanged, p2 changed: an unchanged part's old and new files are byte-for-byte the same.
    with pytest.raises(Crash):
        exports.export_design([two()[0], ExportPart("p2", "lid", LID.model_copy(update={"length": 15}), 2)], cad,
                              "job", overwrite=True)
    monkeypatch.setattr(publish.os, "replace", rename)
    monkeypatch.setattr(publish, "_rollback", real_rollback)

    def crash(_journal):
        raise Crash()

    monkeypatch.setattr(publish, "_undo_copy", crash)  # dies after putting the files back, journal still there
    with pytest.raises(Crash):
        exports.recover_all(cad, None)
    assert (cad / "job.dieline.pending.json").exists()
    monkeypatch.setattr(publish, "_undo_copy", real_undo)
    notes = exports.recover_all(cad, None)
    assert notes and "undone" in notes[0]
    assert {p: b for p, b in files(cad).items() if "superseded" not in p.parts} == before


def test_a_journal_that_doesnt_fit_is_kept_and_blocks(tmp_path):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job")
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "file.txt").write_text("keep me")
    journal = cad / "job.dieline.pending.json"
    # A staging path in the journal is never used: the one this transaction would have is derived.
    journal.write_text(json.dumps({"txn": "0" * 16, "stem": "job", "sample": False, "label": "job x",
                                   "copied": None, "staging": str(victim), "moves": []}))
    exports.recover_all(cad, None)
    assert (victim / "file.txt").read_text() == "keep me" and not journal.exists()
    for body in (
        {"txn": "0" * 16, "stem": "job", "sample": False, "label": "job x", "copied": None,
         "moves": [{"kind": "install", "src": str(cad / ".dieline-staging" / ("0" * 16) / "0-job.cf2"),
                    "dst": str(victim / "file.txt"), "sha": "0" * 64}]},
        {"txn": None, "stem": "job", "sample": False, "label": "job x", "moves": []},
    ):
        journal.write_text(json.dumps(body))
        notes = exports.recover_all(cad, None)
        assert notes and "can't act on" in notes[0]
        assert journal.exists() and (victim / "file.txt").read_text() == "keep me"
        with pytest.raises(publish.Unresolved):
            exports.export(BOX, cad, "job", overwrite=True)
    journal.unlink()
    exports.export(BOX, cad, "job", overwrite=True)


def test_a_crash_after_the_commit_is_finished(tmp_path, monkeypatch):
    cad = tmp_path / "cad"
    written = []
    real = publish._write_journal

    def keep(t, body):
        real(t, body)
        written.append(t.journal.read_bytes())

    monkeypatch.setattr(publish, "_write_journal", keep)
    exports.export(BOX, cad, "job")
    exports.export(BOX.model_copy(update={"length": 16}), cad, "job", overwrite=True)
    after = files(cad)
    (cad / "job.dieline.pending.json").write_bytes(written[-1])  # the journal survived the crash
    notes = exports.recover_all(cad, None)
    assert notes == ["An interrupted export of job in the export folder was finished."]
    assert files(cad) == after


def test_the_next_export_recovers_first(tmp_path, monkeypatch):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job")
    real_rollback = publish._rollback
    rename = _crash_at_commit(monkeypatch, cad)
    with pytest.raises(Crash):
        exports.export(BOX.model_copy(update={"length": 16}), cad, "job", overwrite=True)
    monkeypatch.setattr(publish.os, "replace", rename)
    monkeypatch.setattr(publish, "_rollback", real_rollback)
    out = exports.export(BOX.model_copy(update={"length": 18}), cad, "job", overwrite=True)
    assert out["recovered"] and "undone" in out["recovered"][0]
    assert json.loads((cad / "job.dieline.json").read_text())["parts"][0]["params"]["length"] == 18


# ------------------------------------------------------------------ reopening


def test_open_as_exported_shows_the_snapshot(tmp_path):
    out = exports.export_design(two(), tmp_path, "job")
    record = json.loads(Path(out["job_path"]).read_text())
    shown = exports.open_job(record, "exported")
    assert shown["mode"] == "exported" and shown["set_status"] == "complete"
    assert [p["name"] for p in shown["parts"]] == ["job-p1.cf2", "job-p2.cf2"]
    assert shown["parts"][0]["cf2"].encode("ascii") == (tmp_path / "job-p1.cf2").read_bytes()
    (tmp_path / "job-p2.cf2").write_text("edited")
    again = exports.open_job(record, "exported")
    assert again["set_status"] == "incomplete" and "job-p2.cf2 changed since it was exported." in again["set_notes"]
    assert exports.open_job({**record, "folder": "/nowhere"}, "exported")["set_status"] == "unverified"


def test_regenerate_keeps_every_part_and_says_what_changed(tmp_path):
    out = exports.export_design(two(), tmp_path, "job")
    record = json.loads(Path(out["job_path"]).read_text())
    record["parts"][1]["summary"]["blank_in"] = {"width": 1, "height": 1}
    res = exports.open_job(record)
    assert [p["part_id"] for p in res["parts"]] == ["p1", "p2"] and res["parts"][1]["quantity"] == 2
    assert res["changes"] and all(c.startswith("lid: ") for c in res["changes"])


def test_reopened_parts_replace_the_session_design_keeping_ids(tmp_path):
    from dieline_engine.rpc import Server

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"output_dir": str(tmp_path / "out")})
    server = Server(io.StringIO(), store)
    out = exports.export_design(two("101", None), tmp_path / "out", "job")
    res = server.methods["job.open"]({"record": json.loads(Path(out["job_path"]).read_text())})
    shown = server.methods["design.replace"]({"session_id": "s1", "parts": res["parts"]})
    assert [(p["part_id"], p["label"], p["quantity"], p["piece_no"]) for p in shown["parts"]] == [
        ("p1", "body", 2, "101"), ("p2", "lid", 2, None)]
    plan = server.methods["export.plan"]({"session_id": "s1", "design_id": shown["design_id"],
                                          "revision": shown["revision"], "filename": "job2",
                                          "formats": ["cf2", "dxf"]})
    assert plan["ready"] and [Path(f["path"]).name for f in plan["files"]] == [
        "job2-101.cf2", "job2-101.dxf", "job2-p2.cf2", "job2-p2.dxf"]
    assert plan["replaces"] == [] and not plan["pickup"]
    plan = server.methods["export.plan"]({"session_id": "s1", "design_id": shown["design_id"],
                                          "revision": shown["revision"], "filename": "job"})
    assert "job.dieline.json" in plan["replaces"]
    res = server.methods["box.export"]({"session_id": "s1", "design_id": shown["design_id"],
                                        "revision": shown["revision"], "filename": "job2"})
    assert json.loads(Path(res["job_path"]).read_text())["design_id"] == shown["design_id"]


def test_a_pickup_moves_only_the_jobs_own_waiting_files(tmp_path):
    cad, samples = tmp_path / "cad", tmp_path / "samples"
    out = exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=samples)
    victim = tmp_path / "precious.txt"
    victim.write_text("keep me")
    job = Path(out["job_path"])
    record = json.loads(job.read_text())
    record["sample_table"]["moves"][0]["src"] = str(victim)
    job.write_text(json.dumps(record))
    with pytest.raises(publish.PublishError, match="edited or damaged"):
        exports.pickup(job, out["sample_table"]["txn"])
    assert victim.read_text() == "keep me" and not list(samples.iterdir())


# ------------------------------------------------------------------ from the Phase 1d reviews


def test_a_newer_pending_set_still_replaces_what_is_at_the_table(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    a = exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=table)
    exports.pickup(Path(a["job_path"]), a["sample_table"]["txn"])
    exports.export(BOX, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table)  # left waiting
    c = exports.export(BOX.model_copy(update={"length": 16}), cad, "job", ["cf2", "dxf"], overwrite=True,
                       dxf_dir=table)
    assert c["sample_table"]["status"] == "pending"
    assert set(c["sample_table"]["replaces"]) == {str(table / f"job-p{i}.dxf") for i in (1, 2)}
    exports.pickup(Path(c["job_path"]), c["sample_table"]["txn"])
    assert sorted(p.name for p in table.iterdir()) == ["job.dxf"]
    # A CF2-only re-export removes what's at the table too, even over a removal still waiting.
    d = exports.export(BOX, cad, "job", ["cf2"], overwrite=True, dxf_dir=table)
    e = exports.export(BOX, cad, "job", ["cf2"], overwrite=True, dxf_dir=table)
    assert d["sample_table"]["status"] == e["sample_table"]["status"] == "pending"
    exports.pickup(Path(e["job_path"]), e["sample_table"]["txn"])
    assert not list(table.iterdir())


def test_a_crash_after_the_main_commit_leaves_a_pickup_that_works(tmp_path, monkeypatch):
    cad, table = tmp_path / "cad", tmp_path / "table"

    def interrupted(*_):
        raise InterruptedError("sidecar stopped after main commit")

    monkeypatch.setattr(exports, "_drop_obsolete_pending", interrupted)
    with pytest.raises(InterruptedError):
        exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=table)
    monkeypatch.undo()
    job = cad / "job.dieline.json"
    record = json.loads(job.read_text())
    exports.recover_all(cad, table)
    assert exports.open_job(record)["set_status"] == "pending"
    exports.pickup(job, record["sample_table"]["txn"])
    assert len(list(table.iterdir())) == 2


def test_a_failed_main_export_takes_its_waiting_sample_files_with_it(tmp_path, monkeypatch):
    cad, table = tmp_path / "cad", tmp_path / "table"
    rename = publish.os.replace

    def fail(src, dst):
        if Path(dst) == cad / "job.dieline.json":
            raise PermissionError("injected")
        rename(src, dst)

    monkeypatch.setattr(publish.os, "replace", fail)
    with pytest.raises(PermissionError):
        exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=table)
    pending = publish.pending_dir(table)
    assert not [p for p in pending.iterdir() if publish.valid_txn(p.name)]
    assert not list(table.iterdir()) and not list(cad.iterdir())


def test_a_dxf_only_export_to_the_table_verifies(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    out = exports.export(BOX, cad, "job", ["dxf"], dxf_dir=table)
    record = json.loads(Path(out["job_path"]).read_text())
    assert exports.open_job(record, "exported")["set_status"] == "complete"


def test_a_half_written_lock_is_waited_for_not_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "LOCK_WAIT_S", 0.2)
    where = publish.lock_dir(tmp_path)
    where.mkdir(parents=True, exist_ok=True)
    (where / publish.LOCK).write_text("")  # another export between creating and writing it
    with pytest.raises(publish.LockHeld) as exc:
        exports.export(BOX, tmp_path, "job")
    assert not exc.value.stale
    with pytest.raises(publish.LockHeld):
        exports.export(BOX, tmp_path, "job", break_lock=exc.value.lock_id)
    assert (where / publish.LOCK).exists()


def test_one_folder_reached_two_ways_is_locked_once(tmp_path):
    cad, real = tmp_path / "cad", tmp_path / "samples"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=real)
    out = exports.export(BOX, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=link, confirm_pickup=True)
    assert out["sample_table"]["status"] == "published"


def test_reopening_keeps_groups_and_links(tmp_path):
    from dieline_engine.rpc import Server

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"output_dir": str(tmp_path / "out")})
    server = Server(io.StringIO(), store)
    out = exports.export_design(two(), tmp_path / "out", "job", structure={
        "groups": [{"id": "g1", "kind": "telescoping", "members": [{"ref": "p1", "role": "body", "quantity": 1},
                                                                    {"ref": "p2", "role": "lid", "quantity": 1}]}],
        "links": [{"id": "l1", "container": "p2", "contained": "p1"}]})
    res = server.methods["job.open"]({"record": json.loads(Path(out["job_path"]).read_text())})
    shown = server.methods["design.replace"]({"session_id": "s1", "parts": res["parts"], "groups": res["groups"],
                                              "links": res["links"]})
    assert [g["id"] for g in shown["groups"]] == ["g1"] and [lk["id"] for lk in shown["links"]] == ["l1"]


def test_names_windows_would_shorten_or_reserve(tmp_path):
    from dieline_engine import designs

    stem = designs.safe_stem("a" * 119 + " b")
    assert not stem.endswith((" ", ".")) and publish.belongs(f"{stem}.cf2", stem)
    assert designs.safe_stem("CON .foo") == "_CON .foo"


def test_the_plan_refuses_what_the_export_would(tmp_path):
    deep = tmp_path / ("d" * 120) / ("e" * 60)
    plan = exports.plan([ExportPart("p1", "box", BOX)], deep, "x" * 30, ["cf2"], None)
    assert not plan["ready"] and "longer than Windows allows" in plan["blocking"][0]
    exports.export(BOX, tmp_path, "job", ["cf2", "dxf"])
    (tmp_path / "job.dxf").write_text("edited")
    plan = exports.plan([ExportPart("p1", "box", BOX)], tmp_path, "job", ["cf2"], None)
    assert not plan["ready"] and "changed since it was exported" in plan["blocking"][0]


# ------------------------------------------------------------------ from the Phase 1d re-checks


def _crash_after_marking(monkeypatch):
    real = exports._mark_published

    def mark_then_die(job, txn):
        real(job, txn)
        raise Crash()

    monkeypatch.setattr(exports, "_mark_published", mark_then_die)


@pytest.mark.parametrize("move", [False, True])
def test_a_committed_sample_replacement_is_never_undone(tmp_path, monkeypatch, move):
    cad, a, b = tmp_path / "cad", tmp_path / "a", tmp_path / "b"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=a)
    longer = BOX.model_copy(update={"length": 16})
    out = exports.export(longer, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=b if move else a)
    _crash_after_marking(monkeypatch)
    with pytest.raises(Crash):
        exports.pickup(Path(out["job_path"]), out["sample_table"]["txn"])
    monkeypatch.undo()
    new_dxf = write_dxf_of(longer)
    table = b if move else a
    assert (table / "job.dxf").read_bytes() == new_dxf
    notes = exports.recover_all(cad, table)
    assert notes and "finished" in notes[0]
    assert (table / "job.dxf").read_bytes() == new_dxf and not (move and (a / "job.dxf").exists())
    exports.export(longer, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table, confirm_pickup=True)


def write_dxf_of(params):
    from dieline_engine import designs
    from dieline_engine.dxf import write_dxf

    return write_dxf(designs.build(params)).encode("ascii")


def test_a_finished_removal_is_found_and_cleaned_up(tmp_path, monkeypatch):
    cad, table = tmp_path / "cad", tmp_path / "table"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    out = exports.export(BOX, cad, "job", ["cf2"], overwrite=True, dxf_dir=None)
    _crash_after_marking(monkeypatch)
    with pytest.raises(Crash):
        exports.pickup(Path(out["job_path"]), out["sample_table"]["txn"])
    monkeypatch.undo()
    assert exports.recover_all(cad, None)
    assert not (publish.pending_dir(table) / "job.dieline.pending.json").exists()


def test_a_main_journal_cant_reach_another_tables_waiting_files(tmp_path):
    cad, cad2, table = tmp_path / "cad", tmp_path / "cad2", tmp_path / "table"
    other = exports.export_design(two(), cad2, "other", ["cf2", "dxf"], dxf_dir=table)
    waiting = publish.pending_dir(table) / other["sample_table"]["txn"]
    assert waiting.is_dir()
    cad.mkdir()
    (cad / "job.dieline.pending.json").write_text(json.dumps({
        "txn": "1" * 16, "stem": "job", "sample": False, "label": "job x", "copied": None, "moves": [],
        "sample_folder": str(table), "sample_txn": other["sample_table"]["txn"]}))
    notes = exports.recover_all(cad, None)  # this table isn't one of cad's, so it isn't locked
    assert waiting.is_dir() and "were left in" in notes[0]
    # Even with that table locked (it's cad's sample table too), the set says it's another job's.
    (cad / "job.dieline.pending.json").write_text(json.dumps({
        "txn": "1" * 16, "stem": "job", "sample": False, "label": "job x", "copied": None, "moves": [],
        "sample_folder": str(table), "sample_txn": other["sample_table"]["txn"]}))
    exports.recover_all(cad, table)
    assert waiting.is_dir()
    exports.pickup(Path(other["job_path"]), other["sample_table"]["txn"])
    assert len(list(table.glob("*.dxf"))) == 2


def test_linked_archive_or_staging_folders_stop_recovery(tmp_path, monkeypatch):
    cad, victim = tmp_path / "cad", tmp_path / "victim"
    exports.export(BOX, cad, "job")
    real_rollback = publish._rollback
    rename = _crash_at_commit(monkeypatch, cad)
    with pytest.raises(Crash):
        exports.export(BOX.model_copy(update={"length": 16}), cad, "job", overwrite=True)
    monkeypatch.setattr(publish.os, "replace", rename)
    monkeypatch.setattr(publish, "_rollback", real_rollback)
    sup = cad / "superseded"
    moved = tmp_path / "sup-real"
    sup.rename(moved)
    victim.mkdir()
    for d in moved.iterdir():
        (victim / d.name).mkdir()
        (victim / d.name / "job.dieline.json").write_text("victim")
    try:
        sup.symlink_to(victim, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    notes = exports.recover_all(cad, None)
    assert notes and "link" in notes[0] and (cad / "job.dieline.pending.json").exists()
    assert all(f.read_text() == "victim" for f in victim.rglob("job.dieline.json"))


def test_a_record_cant_name_a_folder_the_table_doesnt_know(tmp_path):
    cad, table, unrelated = tmp_path / "cad", tmp_path / "table", tmp_path / "unrelated"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    unrelated.mkdir()
    (unrelated / "job.dxf").write_bytes((table / "job.dxf").read_bytes())
    m = cad / "job.dieline.json"
    rec = json.loads(m.read_text())
    rec["sample_table"]["folder"] = str(unrelated)
    rec["sample_table"]["files"] = [str(unrelated / "job.dxf")]
    rec["parts"][0]["files"][1]["path"] = str(unrelated / "job.dxf")
    rec["files"][1] = str(unrelated / "job.dxf")
    m.write_text(json.dumps(rec))
    out = exports.export(BOX, cad, "job", ["cf2"], overwrite=True, dxf_dir=None, confirm_pickup=True)
    assert (unrelated / "job.dxf").exists() and out["sample_table"]["status"] == "none"
    assert not publish.pending_dir(unrelated).exists()  # not even locked


def test_ownership_at_a_shared_table_holds_through_a_crash(tmp_path, monkeypatch):
    cad, cad2, table = tmp_path / "cad", tmp_path / "cad2", tmp_path / "table"

    def interrupted(*_):
        raise InterruptedError("stopped right after the main commit")

    monkeypatch.setattr(exports, "_drop_obsolete_pending", interrupted)
    with pytest.raises(InterruptedError):
        exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=table)
    monkeypatch.undo()
    with pytest.raises(publish.OwnedByAnother):
        exports.export_design(two(), cad2, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table)


def test_a_damaged_ownership_index_blocks_the_table(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    (publish.pending_dir(table) / publish.INDEX).write_text("{nope")
    with pytest.raises(publish.PublishError, match="ownership index"):
        exports.export(BOX, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table)


def test_an_unreadable_journal_keeps_its_staging(tmp_path):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job")
    staged = cad / publish.STAGING / ("a" * 16)
    staged.mkdir(parents=True)
    (staged / "0-job.cf2").write_text("part bytes")
    (cad / "job.dieline.pending.json").write_text("{damaged")
    exports.recover_all(cad, None)
    assert (staged / "0-job.cf2").exists()


def test_a_stuck_break_guard_is_reported_not_spun_on(tmp_path):
    _lock(tmp_path, pid=2**22 + 12345)
    guard = publish.lock_dir(tmp_path) / f"{publish.LOCK}.break"
    guard.write_text("")
    os.utime(guard, (time.time() - 3600, time.time() - 3600))
    started = time.monotonic()
    with pytest.raises(publish.PublishError, match="interrupted"):
        exports.export(BOX, tmp_path, "job", break_lock="x")
    assert time.monotonic() - started < 5 and guard.exists()


def test_a_stale_pickup_with_its_staging_gone_is_not_marked_done(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    three = [*two(), ExportPart("p3", "insert", BOX)]
    first = exports.export_design(three, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    exports.pickup(Path(first["job_path"]), first["sample_table"]["txn"])
    out = exports.export_design(two(), cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table)
    import shutil

    shutil.rmtree(publish.pending_dir(table) / out["sample_table"]["txn"])
    with pytest.raises(publish.PublishError, match="missing or damaged"):
        exports.pickup(Path(out["job_path"]), out["sample_table"]["txn"])
    assert (table / "job-p3.dxf").exists()


def test_a_job_record_differing_only_in_case_is_refused(tmp_path):
    exports.export_design(two(), tmp_path, "JOB")
    if (tmp_path / "job.dieline.json").exists():
        pytest.skip("case-insensitive file system: the names are the same file")
    with pytest.raises(publish.PublishError, match="only in case"):
        exports.export(BOX, tmp_path, "job", overwrite=True)


def test_a_second_rollback_never_hides_a_restored_file(tmp_path, monkeypatch):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job")
    real_rollback = publish._rollback
    rename = _crash_at_commit(monkeypatch, cad)
    with pytest.raises(Crash):
        exports.export(BOX, cad, "job", overwrite=True)  # unchanged: old and new bytes are the same
    monkeypatch.setattr(publish.os, "replace", rename)
    monkeypatch.setattr(publish, "_rollback", real_rollback)
    (cad / "job.cf2").unlink()  # removed by hand before recovery
    for _ in range(2):
        notes = exports.recover_all(cad, None)
        assert notes and "couldn't be undone" in notes[0]
    assert list((cad / "superseded").rglob("job.cf2"))  # the old file is still safe in its archive


# ------------------------------------------------------------------ from the Phase 1d third reviews


def test_a_claim_stands_while_its_job_record_cant_be_read(tmp_path, monkeypatch):
    cad, cad2, table = tmp_path / "cad", tmp_path / "cad2", tmp_path / "table"
    exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=table)  # waiting
    real = publish.read_json
    held = cad / "job.dieline.json"

    def flaky(path):
        return {"_unreadable": True} if Path(path) == held else real(path)

    monkeypatch.setattr(publish, "read_json", flaky)
    with pytest.raises(publish.OwnedByAnother, match="can't be read"):
        exports.export_design(two(), cad2, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table)


def test_a_table_used_as_an_export_folder_still_respects_its_claims(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=table)  # waiting at the table
    with pytest.raises(publish.OwnedByAnother):
        exports.export_design(two(), table, "job", ["dxf"])  # another designer's export folder is that table


def test_a_malformed_ownership_index_entry_blocks_the_table(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    index = publish.pending_dir(table) / publish.INDEX
    data = json.loads(index.read_text())
    data[next(iter(data))] = None
    index.write_text(json.dumps(data))
    with pytest.raises(publish.PublishError, match="ownership index"):
        exports.export(BOX, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table)
    index.write_text("null")
    with pytest.raises(publish.PublishError, match="ownership index"):
        exports.export(BOX, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table)


def test_a_replaced_sample_file_vanishing_mid_export_changes_nothing(tmp_path, monkeypatch):
    cad, table = tmp_path / "cad", tmp_path / "table"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    before = files(tmp_path)
    real = publish.file_digest
    calls = {"n": 0}

    def vanish(path):
        if Path(path) == table / "job.dxf" and not str(path).endswith(".tmp"):
            calls["n"] += 1
            if calls["n"] > 1:  # the checks saw it; then it's gone
                return None
        return real(path)

    monkeypatch.setattr(publish, "file_digest", vanish)
    with pytest.raises(publish.ChangedOnDisk):
        exports.export(BOX, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table)
    monkeypatch.undo()
    assert {p: b for p, b in files(tmp_path).items() if publish.INDEX not in p.name} == \
        {p: b for p, b in before.items() if publish.INDEX not in p.name}


def test_files_a_table_doesnt_record_make_the_set_unverified(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    out = exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    (publish.pending_dir(table) / publish.INDEX).unlink()
    status, notes = exports.verify_set(json.loads(Path(out["job_path"]).read_text()))
    assert status == "incomplete" and any("isn't recorded" in n for n in notes)


# ------------------------------------------------------------------ from the Phase 1d fourth reviews


def test_a_sample_file_edited_during_the_export_is_never_replaced(tmp_path, monkeypatch):
    cad, table = tmp_path / "cad", tmp_path / "table"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    real_stage = publish.stage

    def stage_then_edit(t):
        out = real_stage(t)
        if t.sample:
            (table / "job.dxf").write_text("edited at the table meanwhile")
        return out

    monkeypatch.setattr(publish, "stage", stage_then_edit)
    with pytest.raises(publish.ChangedOnDisk):
        exports.export(BOX.model_copy(update={"length": 16}), cad, "job", ["cf2", "dxf"], overwrite=True,
                       dxf_dir=table)
    assert (table / "job.dxf").read_text() == "edited at the table meanwhile"
    assert not [p for p in publish.pending_dir(table).iterdir() if publish.valid_txn(p.name)]


def test_a_failed_owner_mark_leaves_no_waiting_files(tmp_path, monkeypatch):
    cad, table = tmp_path / "cad", tmp_path / "table"

    def full_disk(*_):
        raise OSError("no space left")

    monkeypatch.setattr(publish, "mark_staging", full_disk)
    with pytest.raises(OSError):
        exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=table)
    assert not [p for p in publish.pending_dir(table).iterdir() if publish.valid_txn(p.name)]


def test_a_removal_pickup_found_done_cleans_up_its_waiting_folder(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    out = exports.export(BOX, cad, "job", ["cf2"], overwrite=True, dxf_dir=table)
    (table / "job.dxf").unlink()  # removed by hand before the pickup
    assert exports.pickup(Path(out["job_path"]), out["sample_table"]["txn"])["status"] == "published"
    assert not (publish.pending_dir(table) / out["sample_table"]["txn"]).exists()


def test_a_removal_pickup_cant_name_another_jobs_waiting_set(tmp_path):
    cad, cad2, table = tmp_path / "cad", tmp_path / "cad2", tmp_path / "table"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=table)
    out = exports.export(BOX, cad, "job", ["cf2"], overwrite=True, dxf_dir=table)  # removal waiting
    other = exports.export_design(two(), cad2, "other", ["cf2", "dxf"], dxf_dir=table)
    job = Path(out["job_path"])
    rec = json.loads(job.read_text())
    rec["sample_table"]["txn"] = other["sample_table"]["txn"]
    job.write_text(json.dumps(rec))
    with pytest.raises(publish.PublishError, match="don't say they're this job's"):
        exports.pickup(job, other["sample_table"]["txn"])
    assert (publish.pending_dir(table) / other["sample_table"]["txn"]).is_dir()


def test_a_job_exported_straight_into_the_table_owns_its_names_there(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    exports.export(BOX, table, "job", ["dxf"])  # another designer's export folder is the table
    with pytest.raises(publish.OwnedByAnother):
        exports.export(BOX, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=table)


def test_a_drive_root_in_a_main_journal_is_kept_not_crashed_on(tmp_path):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job")
    journal = cad / "job.dieline.pending.json"
    journal.write_text(json.dumps({"txn": "1" * 16, "stem": "job", "sample": False, "label": "job x",
                                   "copied": None, "moves": [], "sample_folder": tmp_path.anchor,
                                   "sample_txn": "2" * 16}))
    notes = exports.recover_all(cad, None)
    assert notes and "drive root" in notes[0] and journal.exists()


# ------------------------------------------------------------------ from the Phase 1d fifth reviews


@pytest.mark.parametrize("table", [False, True])
def test_a_file_saved_while_it_moves_aside_goes_back_untouched(tmp_path, monkeypatch, table):
    cad, samples = tmp_path / "cad", tmp_path / "table"
    exports.export(BOX, cad, "job", ["cf2", "dxf"], dxf_dir=samples if table else None)
    old = (samples if table else cad) / "job.dxf"

    def save(src, dst):
        if src == old:
            old.write_text("saved at the last moment")

    hook_moves(monkeypatch, before=save)
    longer = BOX.model_copy(update={"length": 16})
    if table:  # the main folder committed; the table keeps the saved file and the new one waits
        out = exports.export(longer, cad, "job", ["cf2", "dxf"], overwrite=True, dxf_dir=samples,
                             confirm_pickup=True)
        assert out["sample_table"]["status"] == "pending" and "changed during the export" in out["sample_table"]["error"]
        assert not (publish.pending_dir(samples) / "job.dieline.pending.json").exists()
    else:
        with pytest.raises(publish.ChangedOnDisk):
            exports.export(longer, cad, "job", ["cf2", "dxf"], overwrite=True)
        assert not (cad / "job.dieline.pending.json").exists()
    monkeypatch.undo()
    assert old.read_text() == "saved at the last moment"


def test_main_folder_records_still_own_their_sample_table_files(tmp_path):
    cad, table = tmp_path / "cad", tmp_path / "table"
    table.mkdir()
    cad.mkdir()
    (table / "oth-er.dxf").write_text("a version 1 job's DXF, from before the ownership index")
    (cad / "oth-er.dieline.json").write_text(json.dumps({"format": "dieline-studio-job", "version": 1,
                                                          "files": [str(table / "oth-er.dxf")]}))
    with pytest.raises(publish.OwnedByAnother, match="oth-er"):
        exports.export_design(two("er", "x"), cad, "oth", ["dxf"], overwrite=True, dxf_dir=table)
    assert (table / "oth-er.dxf").read_text().startswith("a version 1")


def test_a_drive_root_sample_folder_doesnt_stop_cf2_exports_or_startup(tmp_path):
    root = Path(tmp_path.anchor)
    out = exports.export(BOX, tmp_path / "cad", "job", ["cf2"], dxf_dir=root)
    assert out["sample_table"]["status"] == "none"
    assert exports.recover_all(tmp_path / "cad", root) == []
    with pytest.raises(publish.PublishError, match="drive root"):
        exports.export(BOX, tmp_path / "cad", "job2", ["dxf"], dxf_dir=root)


# ------------------------------------------------------------------ from the Phase 1d sixth reviews


def test_a_second_save_during_the_move_back_is_never_overwritten(tmp_path, monkeypatch):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job", ["cf2", "dxf"])
    old = cad / "job.dxf"

    def first(src, dst):
        if src == old:
            old.write_text("first save")

    def second(src, dst):
        if src == old and not old.exists():
            old.write_text("second save")  # recreated right after it moved aside

    hook_moves(monkeypatch, before=first, after=second)
    with pytest.raises(publish.Unresolved, match="both versions"):
        exports.export(BOX.model_copy(update={"length": 16}), cad, "job", ["cf2", "dxf"], overwrite=True)
    monkeypatch.undo()
    assert old.read_text() == "second save"
    assert [p.read_text() for p in (cad / "superseded").rglob("job.dxf")] == ["first save"]
    assert (cad / "job.dieline.pending.json").exists()  # kept for the designer


def test_an_abandoned_sample_journal_is_undone_not_finished(tmp_path, monkeypatch):
    cad, table = tmp_path / "cad", tmp_path / "table"
    a = exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=table)
    exports.pickup(Path(a["job_path"]), a["sample_table"]["txn"])
    out = exports.export_design(two(), cad, "job", ["cf2"], overwrite=True, dxf_dir=table)  # removal waits
    second = table / "job-p2.dxf"

    def save(src, dst):
        if src == second:
            second.write_text("saved at the table meanwhile")

    def die(_moves):
        raise Crash()

    hook_moves(monkeypatch, before=save)
    monkeypatch.setattr(publish, "_rollback", die)
    with pytest.raises(Crash):
        exports.pickup(Path(out["job_path"]), out["sample_table"]["txn"])
    monkeypatch.undo()
    notes = exports.recover_all(cad, table)
    assert notes and "undone" in notes[0]
    assert (table / "job-p1.dxf").exists() and second.read_text() == "saved at the table meanwhile"
    assert json.loads(Path(out["job_path"]).read_text())["sample_table"]["status"] == "pending"


def test_a_drive_root_sample_folder_never_joins_the_set(tmp_path):
    w = exports._Where("job", tmp_path.resolve(), Path(tmp_path.anchor), ["cf2"])
    assert all(p.parent == tmp_path.resolve() for p in exports._family(w))


# ------------------------------------------------------------------ from the Phase 1d seventh reviews




def test_a_resave_of_the_original_bytes_keeps_the_conflict(tmp_path, monkeypatch):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job", ["cf2", "dxf"])
    old = cad / "job.dxf"
    original = old.read_bytes()
    state = {"archived": False}

    def first(src, dst):
        if src == old and not state["archived"]:
            old.write_text("first save")

    def second(src, dst):
        if src == old and not state["archived"] and "superseded" in dst.parts:
            state["archived"] = True
            old.write_bytes(original)  # the second save puts the original bytes back

    hook_moves(monkeypatch, before=first, after=second)
    with pytest.raises(publish.PublishError):
        exports.export(BOX.model_copy(update={"length": 16}), cad, "job", ["cf2", "dxf"], overwrite=True)
    monkeypatch.undo()
    archived = list((cad / "superseded").rglob("job.dxf"))
    assert archived and archived[0].read_text() == "first save"
    assert (cad / "job.dieline.pending.json").exists()
    assert exports.recover_all(cad, None) and (cad / "job.dieline.pending.json").exists()  # still asks


def test_an_edited_file_already_put_back_clears_on_recovery(tmp_path, monkeypatch):
    cad, table = tmp_path / "cad", tmp_path / "table"
    a = exports.export_design(two(), cad, "job", ["cf2", "dxf"], dxf_dir=table)
    exports.pickup(Path(a["job_path"]), a["sample_table"]["txn"])
    out = exports.export_design(two(), cad, "job", ["cf2"], overwrite=True, dxf_dir=table)
    second = table / "job-p2.dxf"
    real_write = publish._write_journal
    state = {"aborted": False}

    def save(src, dst):
        if src == second and "superseded" in dst.parts:
            second.write_text("saved at the table meanwhile")

    def write(t, body):
        if state["aborted"]:
            raise Crash()  # dies before the shortened journal is written
        real_write(t, body)
        state["aborted"] = bool(body.get("aborted"))

    def die(_moves):
        raise Crash()  # the process is gone: no rollback either

    hook_moves(monkeypatch, before=save)
    monkeypatch.setattr(publish, "_write_journal", write)
    monkeypatch.setattr(publish, "_rollback", die)
    with pytest.raises(Crash):
        exports.pickup(Path(out["job_path"]), out["sample_table"]["txn"])
    monkeypatch.undo()
    notes = exports.recover_all(cad, table)
    assert notes and "undone" in notes[0]
    assert (table / "job-p1.dxf").exists() and second.read_text() == "saved at the table meanwhile"
    assert not (publish.pending_dir(table) / "job.dieline.pending.json").exists()


def test_a_file_at_both_ends_of_an_archive_move_is_always_left_for_the_designer(tmp_path):
    a, b = tmp_path / "a.dxf", tmp_path / "b.dxf"
    a.write_text("x")
    b.write_text("x")
    m = {"kind": "archive", "src": str(a), "dst": str(b), "sha": publish.digest(b"x")}
    assert publish.move_state(m) == "inconsistent"
    with pytest.raises(publish.Unresolved):
        publish._rollback([m])
    assert a.read_text() == b.read_text() == "x"


def test_the_no_replace_rename_never_replaces(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text("mine")
    b.write_text("theirs")
    assert not publish.move_no_clobber(a, b)
    assert a.read_text() == "mine" and b.read_text() == "theirs"
    b.unlink()
    assert publish.move_no_clobber(a, b) and b.read_text() == "mine" and not a.exists()


def test_a_refused_archive_move_doesnt_block_the_next_export(tmp_path, monkeypatch):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job", ["cf2", "dxf"])
    real = publish.move_no_clobber
    taken = []

    def taken_once(src, dst):
        if "superseded" in Path(dst).parts and not taken:
            taken.append(dst)
            Path(dst).write_text("already there")
        return real(src, dst)

    monkeypatch.setattr(publish, "move_no_clobber", taken_once)
    with pytest.raises(publish.ChangedOnDisk, match="already in the archive"):
        exports.export(BOX.model_copy(update={"length": 16}), cad, "job", ["cf2", "dxf"], overwrite=True)
    monkeypatch.undo()
    assert not (cad / "job.dieline.pending.json").exists()
    assert Path(taken[0]).read_text() == "already there"
    exports.export(BOX.model_copy(update={"length": 16}), cad, "job", ["cf2", "dxf"], overwrite=True)




def test_a_save_onto_the_emptied_path_is_never_overwritten(tmp_path, monkeypatch):
    cad = tmp_path / "cad"
    exports.export(BOX, cad, "job")
    live = cad / "job.cf2"

    def save_after_archive(src, dst):
        if src == live and "superseded" in dst.parts:
            live.write_text("an editor's save, right after the old file went aside")

    hook_moves(monkeypatch, after=save_after_archive)
    # The old file can't go back over that save: both are kept, and the journal waits for the designer.
    with pytest.raises(publish.Unresolved):
        exports.export(BOX.model_copy(update={"length": 16}), cad, "job", overwrite=True)
    monkeypatch.undo()
    assert live.read_text().startswith("an editor's save")
    assert (cad / "job.dieline.pending.json").exists()
    notes = exports.recover_all(cad, None)
    assert notes and "couldn't be undone" in notes[0]
    assert list((cad / "superseded").rglob("job.cf2"))


def test_rpc_revisions_must_be_whole_numbers_and_partition_pieces_export_with_their_set(tmp_path):
    """Review finding 19."""
    from dataclasses import replace

    from dieline_engine import partitions, partsets
    from dieline_engine.designstore import FitLink, new_design
    from dieline_engine.rpc import RpcError, Server

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"output_dir": str(tmp_path / "out")})
    server = Server(io.StringIO(), store)
    shown = server.methods["design.replace"]({"session_id": "s1", "params": BOX.model_dump()})
    assert shown["revision"] == 1
    # (a) True == 1 in Python: a bool isn't a revision
    with pytest.raises(RpcError, match="expected"):
        server.methods["design.replace"]({"session_id": "s1", "params": BOX.model_dump(),
                                          "expected": {"design_id": shown["design_id"], "revision": True}})
    with pytest.raises(RpcError, match="revision"):
        server.methods["export.plan"]({"session_id": "s1", "design_id": shown["design_id"], "revision": True})
    assert server.store.current("s1").revision == 1
    # (b) one piece's parameters alone would skip its set's checks: refused with the reason
    sp = partitions.spec_from_inputs("box_cells", {"length": 12, "width": 9, "depth": 6, "cells_long": 3,
                                                   "cells_short": 2})
    piece = partitions.PartitionParams(board="32 B KR", role="long", set=sp).model_dump()
    with pytest.raises(RpcError, match="exported with its set"):
        server.methods["export.plan"]({"params": piece})
    with pytest.raises(RpcError, match="exported with its set"):
        server.methods["box.export"]({"params": piece})
    # (c) a design check that blocks the export still lists each format's readiness
    d = new_design(BOX, "carton")
    d, gid = partsets.add_set(d, sp, contained_in="carton")
    d = replace(d, links=(*d.links, FitLink("fit_gone", "p9", gid)))
    held = server.store.mutate("s1", lambda cur: d, expected="any")
    plan = server.methods["export.plan"]({"session_id": "s1", "design_id": held["design_id"],
                                          "revision": held["revision"], "formats": ["cf2", "dxf"]})
    assert not plan["ready"] and set(plan["formats"]) == {"cf2", "dxf"}
    assert all(not f["ready"] and f["blocking"] for f in plan["formats"].values())


def test_a_held_partition_piece_without_its_set_is_not_exported(tmp_path):
    """Review of the v0.4.0 merge: a snapshot holding one piece and no group used to export it."""
    from dieline_engine import designs, partitions, partsets
    from dieline_engine.designstore import new_design
    from dieline_engine.rpc import Server

    store = SettingsStore(tmp_path / "settings.json")
    store.update({"output_dir": str(tmp_path / "out")})
    server = Server(io.StringIO(), store)
    sp = partitions.spec_from_inputs("box_cells", {"length": 12, "width": 9, "depth": 6, "cells_long": 3,
                                                   "cells_short": 2})
    piece = partitions.PartitionParams(board="32 B KR", role="long", set=sp).model_dump()
    lone = server.methods["design.replace"]({"session_id": "s1", "parts": [{"part_id": "p1", "params": piece}]})
    target = {"session_id": "s1", "design_id": lone["design_id"], "revision": lone["revision"], "formats": ["cf2"]}
    plan = server.methods["export.plan"](target)
    assert not plan["ready"] and "isn't in a partition set" in plan["blocking"][0]
    assert set(plan["formats"]) == {"cf2", "dxf"}  # both listed, even when only one was asked for
    with pytest.raises(designs.DesignCheckFailed, match="isn't in a partition set"):
        server.methods["box.export"]({**target, "filename": "lone"})
    # The whole set, held with its box, exports.
    d, _ = partsets.add_set(new_design(BOX, "carton"), sp, contained_in="carton")
    held = server.store.mutate("s1", lambda cur: d, expected="any")
    full = {"session_id": "s1", "design_id": held["design_id"], "revision": held["revision"]}
    assert server.methods["export.plan"](full)["ready"], server.methods["export.plan"](full)["blocking"]
