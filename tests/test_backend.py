"""Backend tests against a throw-away local restic repository.

Run:  python -m pytest tests   (needs `restic` on PATH; no AppKit required)
"""
import os
import shutil
import subprocess
import time

import pytest

from resticcontrol.backend import Restic, RepoSpec, unique_path

pytestmark = pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("rc")
    repo_dir, data = root / "repo", root / "data"
    env = dict(os.environ, RESTIC_REPOSITORY=str(repo_dir), RESTIC_PASSWORD="pw")
    sh = lambda *a: subprocess.run(["restic", "-q", *a], env=env, check=True)
    sh("init")
    (data / "a" / "b").mkdir(parents=True)
    (data / "a" / "f.txt").write_text("one")
    (data / "a" / "b" / "g.txt").write_text("g")
    sh("backup", str(data)); time.sleep(1.1)
    sh("backup", str(data)); time.sleep(1.1)          # identical -> folded
    (data / "a" / "f.txt").write_text("two!")
    sh("backup", str(data)); time.sleep(1.1)
    (data / "a" / "f.txt").unlink()
    sh("backup", str(data))
    r = Restic(RepoSpec(repository=str(repo_dir), password="pw"), cache_dir=str(root / "cache"))
    yield r, data, root
    r.close()


def test_snapshots_and_ls(repo):
    r, data, _ = repo
    snaps = r.snapshots()
    assert len(snaps) == 4 and snaps[0].time >= snaps[-1].time
    names = [n.name for n in r.ls(snaps[0], str(data / "a"))]
    assert names == ["b"]                                  # f.txt deleted in newest
    names = [n.name for n in r.ls(snaps[-1], str(data / "a"))]
    assert names == ["b", "f.txt"]
    assert [n.name for n in r.ls(snaps[0], "/")]           # root listing works


def test_merged_marks_deleted(repo):
    r, data, _ = repo
    snaps = r.snapshots()
    merged = {e.node.name: e for e in r.merged_ls(snaps, str(data / "a"))}
    assert merged["b"].in_latest and merged["b"].count == 4
    assert not merged["f.txt"].in_latest and merged["f.txt"].count == 3


def test_history_collapses(repo):
    r, data, _ = repo
    snaps = r.snapshots()
    hist = r.history(snaps, str(data / "a" / "f.txt"))
    assert [v.node.size for v in hist] == [4, 3]
    assert [v.count for v in hist] == [1, 2]
    assert len(r.history(snaps, str(data / "a" / "f.txt"), collapse=False)) == 3


def test_dump_restore_materialize(repo, tmp_path):
    r, data, _ = repo
    snaps = r.snapshots()
    v_old = r.history(snaps, str(data / "a" / "f.txt"))[-1]
    p = r.materialize(v_old.snapshot, v_old.node)
    assert open(p).read() == "one"
    out = r.restore(v_old.snapshot, v_old.node, str(tmp_path))
    assert open(out).read() == "one"
    out2 = r.restore(v_old.snapshot, v_old.node, str(tmp_path))
    assert out2 != out and "restored" in out2
    d = r.lookup(snaps[0], str(data / "a"))
    dest = r.restore(snaps[0], d, str(tmp_path))
    assert open(os.path.join(dest, "b", "g.txt")).read() == "g"


def test_unique_path(tmp_path):
    p = tmp_path / "x.txt"
    assert unique_path(str(p)) == str(p)


def _make_tree(root, n_dirs=40):
    import random
    rnd = random.Random(1)
    dirs = [root]
    for i in range(n_dirs):
        d = rnd.choice(dirs) / f"d{i} ü"
        d.mkdir()
        dirs.append(d)
        for j in range(rnd.randint(0, 5)):
            (d / f"f{j}.txt").write_text(str(i * j))
    (root / "empty").mkdir()
    return dirs


def test_full_index_matches_per_dir_ls(tmp_path):
    repo_dir, data = tmp_path / "repo", tmp_path / "data"
    env = dict(os.environ, RESTIC_REPOSITORY=str(repo_dir), RESTIC_PASSWORD="pw")
    subprocess.run(["restic", "-q", "init"], env=env, check=True)
    data.mkdir()
    dirs = _make_tree(data)
    subprocess.run(["restic", "-q", "backup", str(data)], env=env, check=True)
    spec = RepoSpec(repository=str(repo_dir), password="pw")

    plain = Restic(spec, cache_dir=str(tmp_path / "c1"))
    indexed = Restic(spec, cache_dir=str(tmp_path / "c2"))
    snap = plain.snapshots()[0]
    seen = []
    n = indexed.index_snapshot(snap, progress=seen.append)
    assert n > 100 and seen[-1] == n and indexed.is_indexed(snap)
    assert indexed.index_snapshot(snap) == 0             # second call is a no-op

    check = ["/", str(data.parent), str(data), str(data / "empty")] + [str(d) for d in dirs]
    for d in check:
        a = [(x.name, x.type, x.size, x.mtime) for x in plain.ls(snap, d)]
        b = [(x.name, x.type, x.size, x.mtime) for x in indexed.ls(snap, d)]
        assert a == b, d
    assert indexed.ls(snap, str(data / "does-not-exist")) == []   # answered from index
    st = indexed.index.stats()
    assert st["trees"] == 1 and st["listings"] >= len(check)

    # identical second snapshot: new tree rows, but no new blobs (dedup)
    (data / "new.txt").write_text("x")
    subprocess.run(["restic", "-q", "backup", str(data)], env=env, check=True)
    snap2 = indexed.snapshots()[0]
    indexed.index_snapshot(snap2)
    st2 = indexed.index.stats()
    assert st2["trees"] == 2 and st2["blobs"] < st["blobs"] + 5

    # the index persists across instances
    again = Restic(spec, cache_dir=str(tmp_path / "c2"))
    assert again.is_indexed(snap2)
    indexed.clear_cache()
    assert not indexed.is_indexed(snap)
    for r in (plain, indexed, again):
        r.close()


def test_find_versions_single_process(repo):
    r, data, _ = repo
    snaps = r.snapshots()
    via_find = r.find_versions(str(data / "a" / "f.txt"), snaps)
    via_ls = r.history(snaps, str(data / "a" / "f.txt"))
    assert [(v.snapshot.id, v.node.size, v.count) for v in via_find] == \
           [(v.snapshot.id, v.node.size, v.count) for v in via_ls]
    assert r.find_versions(str(data / "nope.txt"), snaps) == []
    assert len(r.find_versions(str(data / "a" / "b" / "g.txt"), snaps, collapse=False)) == 4
    host = snaps[0].hostname
    assert len(r.find_versions(str(data / "a" / "b" / "g.txt"), snaps, host=host)) == 1
    assert r.find_versions(str(data / "a" / "b" / "g.txt"), snaps, host="no-such-host") == []


def test_find_versions_glob_chars(tmp_path):
    repo_dir, data = tmp_path / "repo", tmp_path / "data"
    env = dict(os.environ, RESTIC_REPOSITORY=str(repo_dir), RESTIC_PASSWORD="pw")
    subprocess.run(["restic", "-q", "init"], env=env, check=True)
    (data / "d[1]").mkdir(parents=True)
    weird = data / "d[1]" / "we*ird ?name\\x.txt"
    weird.write_text("w")
    (data / "d[1]" / "weXird Xname\\x.txt").write_text("decoy")
    subprocess.run(["restic", "-q", "backup", str(data)], env=env, check=True)
    r = Restic(RepoSpec(repository=str(repo_dir), password="pw"), cache_dir=str(tmp_path / "c"))
    vs = r.find_versions(str(weird), r.snapshots())
    assert len(vs) == 1 and vs[0].node.size == 1
    r.close()


def test_cancel_slot(repo):
    import threading
    from resticcontrol.backend import Cancelled
    r, data, _ = repo
    snaps = r.snapshots()
    errors = []

    def first():
        try:
            r.run("find", "--json", "*", slot="history")
        except Cancelled:
            errors.append("cancelled")
    t = threading.Thread(target=first)
    t.start()
    time.sleep(0.05)
    r.find_versions(str(data / "a" / "b" / "g.txt"), snaps, slot="history")   # replaces it
    t.join()
    assert errors in ([], ["cancelled"])      # first may have finished already on a fast box


def test_local_helpers(repo, tmp_path):
    from resticcontrol.backend import disk_status, list_local, snapshots_covering
    r, data, _ = repo
    snaps = r.snapshots()
    assert len(snapshots_covering(snaps, str(data / "a"))) == 4
    assert len(snapshots_covering(snaps, "/")) == 4                 # ancestor of backup path
    assert snapshots_covering(snaps, "/definitely/elsewhere") == []
    names = [n.name for n in list_local(str(data / "a"))]
    assert names == ["b"]                                            # f.txt was deleted
    g = r.lookup(snaps[0], str(data / "a" / "b" / "g.txt"))
    assert disk_status(g) == "same"
    f = r.history(snaps, str(data / "a" / "f.txt"))[0].node
    assert disk_status(f) == "missing"


@pytest.fixture(scope="module")
def series(tmp_path_factory):
    """12 snapshots; proj/ changes in snapshots 4 and 9 (1-based), other/ changes every time."""
    root = tmp_path_factory.mktemp("series")
    repo_dir, data = root / "repo", root / "data"
    env = dict(os.environ, RESTIC_REPOSITORY=str(repo_dir), RESTIC_PASSWORD="pw")
    sh = lambda *a: subprocess.run(["restic", "-q", *a], env=env, check=True)
    sh("init")
    (data / "proj" / "sub").mkdir(parents=True)
    (data / "proj" / "keep").mkdir()
    (data / "other").mkdir()
    (data / "proj" / "a.txt").write_text("a1")
    (data / "proj" / "sub" / "deep.txt").write_text("d1")
    (data / "proj" / "keep" / "k.txt").write_text("k")
    for i in range(1, 13):
        (data / "other" / "tick.txt").write_text(str(i))
        if i == 4:
            (data / "proj" / "a.txt").write_text("a2-changed")
            (data / "proj" / "new.txt").write_text("n")
        if i == 9:
            (data / "proj" / "sub" / "deep.txt").write_text("d2-changed")
            (data / "proj" / "new.txt").unlink()
        sh("backup", str(data))
    r = Restic(RepoSpec(repository=str(repo_dir), password="pw"), cache_dir=str(root / "cache"))
    yield r, data, root
    r.close()


def test_folder_versions_binary_search(series):
    r, data, root = series
    snaps = r.snapshots()                          # newest first
    assert len(snaps) == 12
    calls = []
    fast = r.folder_versions(snaps, str(data / "proj"), progress=lambda v, c: calls.append(c))
    # proj versions: snapshots 9-12, 4-8, 1-3 (newest first)
    assert [v.count for v in fast] == [4, 5, 3]
    assert [v.oldest.id for v in fast] == [snaps[3].id, snaps[8].id, snaps[11].id]
    assert calls[-1] < 12                           # fewer than one call per snapshot
    fresh = Restic(RepoSpec(repository=r.spec.repository, password="pw"),
                   cache_dir=str(root / "cache-full"))
    full = fresh.folder_versions(snaps, str(data / "proj"), full=True)
    assert [(v.fp, v.count) for v in full] == [(v.fp, v.count) for v in fast]
    fresh.close()


def test_unchanged_folder_costs_two_calls(series):
    r, data, root = series
    snaps = r.snapshots()
    fresh = Restic(RepoSpec(repository=r.spec.repository, password="pw"),
                   cache_dir=str(root / "cache-keep"))
    calls = []
    vs = fresh.folder_versions(snaps, str(data / "proj" / "keep"), progress=lambda v, c: calls.append(c))
    assert len(vs) == 1 and vs[0].count == 12 and calls == [2]
    # subfolder fingerprints came for free from the parent: 0 new calls for probed snapshots
    fresh.folder_tree(snaps[0], str(data / "proj"))
    assert fresh._fp_get(snaps[0], str(data / "proj" / "sub")) not in (None, "")
    fresh.close()


def test_folder_changes_and_listing(series):
    r, data, _ = series
    snaps = r.snapshots()
    vs = r.folder_versions(snaps, str(data / "proj"))
    newest, middle, oldest = vs
    ch = r.folder_changes(newest.newest, middle.newest, str(data / "proj"))
    assert ch.of("sub") == "changed" and ch.of("new.txt") == "removed" and ch.of("keep") == ""
    assert ch.summary() == "1 file removed, 1 folder changed"
    ch2 = r.folder_changes(middle.newest, oldest.newest, str(data / "proj"))
    assert ch2.of("a.txt") == "changed" and ch2.of("new.txt") == "added"
    assert "1 file changed" in ch2.summary() and "1 file added" in ch2.summary()
    # drilling down: only the changed subfolder differs
    sub = r.folder_changes(newest.newest, middle.newest, str(data / "proj" / "sub"))
    assert sub.of("deep.txt") == "changed"
    # listing from the cached tree equals restic ls
    listed = [(n.name, n.type, n.size) for n in r.ls(newest.newest, str(data / "proj"))]
    assert listed == [("keep", "dir", None), ("sub", "dir", None), ("a.txt", "file", 10)]
    assert r.folder_tree(snaps[0], str(data / "nope")) is None
    assert r.folder_versions(snaps, str(data / "nope")) == []


def test_diff_stats(series):
    r, data, _ = series
    snaps = r.snapshots()
    newest, middle, _ = r.folder_versions(snaps, str(data / "proj"))
    st = r.diff_stats(middle.newest, newest.newest, str(data / "proj"))
    if r.restic_version() < (0, 16, 0):
        assert st is None
    else:
        assert st == {"changed": 1, "added": 0, "removed": 1}


def test_file_versions_have_ranges(repo):
    r, data, _ = repo
    snaps = r.snapshots()
    vs = r.find_versions(str(data / "a" / "f.txt"), snaps)
    assert vs[-1].count == 2 and vs[-1].oldest.time < vs[-1].snapshot.time


def test_folder_versions_stop(series):
    from resticcontrol.backend import Cancelled
    r, data, _ = series
    with pytest.raises(Cancelled):
        r.folder_versions(r.snapshots(), str(data / "other"), stop=lambda: True)


def test_find_cache_is_persistent_and_incremental(tmp_path):
    repo_dir, data = tmp_path / "repo", tmp_path / "data"
    env = dict(os.environ, RESTIC_REPOSITORY=str(repo_dir), RESTIC_PASSWORD="pw")
    sh = lambda *a: subprocess.run(["restic", "-q", *a], env=env, check=True)
    sh("init")
    data.mkdir()
    f = data / "f.txt"
    f.write_text("1")
    sh("backup", str(data))
    spec = RepoSpec(repository=str(repo_dir), password="pw")
    r = Restic(spec, cache_dir=str(tmp_path / "c"))
    assert len(r.find_versions(str(f), r.snapshots())) == 1

    calls = []
    orig = Restic.run

    def counting_run(self, *a, **kw):
        if a and a[0] == "find":
            calls.append(a)
        return orig(self, *a, **kw)

    r2 = Restic(spec, cache_dir=str(tmp_path / "c"))       # new process: cache from disk
    snaps = r2.snapshots()
    Restic.run = counting_run
    try:
        assert len(r2.find_versions(str(f), snaps)) == 1
        assert calls == []                                    # no restic call at all
        time.sleep(1.1)
        f.write_text("22")
        sh("backup", str(data))
        snaps = r2.snapshots()
        vs = r2.find_versions(str(f), snaps)
        assert len(vs) == 2
        assert len(calls) == 1 and "--snapshot" in calls[0] and snaps[0].id in calls[0]
        assert snaps[1].id not in calls[0]                    # old snapshot not searched again
    finally:
        Restic.run = orig
    r.close()
    r2.close()


def test_cancel_group(series):
    import threading
    from resticcontrol.backend import Cancelled
    r, data, _ = series
    errs = []

    def slow():
        try:
            r.run("find", "--json", "*", group="view")
        except Cancelled:
            errs.append("x")
    t = threading.Thread(target=slow)
    t.start()
    time.sleep(0.2)
    r.cancel_group("view")
    t.join()
    assert errs in ([], ["x"])


def test_machine_names_merge_local_and_lan(tmp_path):
    from resticcontrol.backend import hostnames_of, hosts_of, same_machine
    repo_dir, data = tmp_path / "repo", tmp_path / "data"
    env = dict(os.environ, RESTIC_REPOSITORY=str(repo_dir), RESTIC_PASSWORD="pw")
    sh = lambda *a: subprocess.run(["restic", "-q", *a], env=env, check=True)
    sh("init")
    data.mkdir()
    f = data / "f.txt"
    f.write_text("1")
    sh("backup", "--host", "MacBook-Pro-7.local", str(data))
    time.sleep(1.1)
    f.write_text("22")
    sh("backup", "--host", "MacBook-Pro-7.lan", str(data))
    sh("backup", "--host", "other-box", str(data))
    r = Restic(RepoSpec(repository=str(repo_dir), password="pw"), cache_dir=str(tmp_path / "c"))
    snaps = r.snapshots()
    assert hosts_of(snaps) == ["MacBook-Pro-7", "other-box"]
    assert hostnames_of(snaps, "MacBook-Pro-7") == ["MacBook-Pro-7.lan", "MacBook-Pro-7.local"]
    assert same_machine("macbook-pro-7.LAN", "MacBook-Pro-7")
    vs = r.find_versions(str(f), snaps, host="MacBook-Pro-7", collapse=False)
    assert sorted(v.snapshot.hostname for v in vs) == ["MacBook-Pro-7.lan", "MacBook-Pro-7.local"]
    r2 = Restic(RepoSpec(repository=str(repo_dir), password="pw"), cache_dir=str(tmp_path / "c2"))
    vs2 = r2.find_versions(str(f), snaps, host="other-box", collapse=False)
    assert [v.snapshot.hostname for v in vs2] == ["other-box"]
    r.close()
    r2.close()


def test_restore_conflict_modes(repo, tmp_path):
    from resticcontrol.backend import restore_conflicts
    r, data, _ = repo
    snap = r.snapshots()[0]
    folder = r.lookup(snap, str(data / "a" / "b"))           # folder b/ with g.txt = "g"
    target = tmp_path / "t"
    target.mkdir()

    def make_current():
        (target / "b").mkdir(exist_ok=True)
        (target / "b" / "mine.txt").write_text("current")

    make_current()
    assert restore_conflicts([folder], lambda n: str(target)) == [str(target / "b")]

    # replace: current goes to "trash", restored copy takes the name
    trashed = []
    dest = r.restore(snap, folder, str(target), conflict="replace",
                     trash=lambda p: (trashed.append(p), os.rename(p, str(tmp_path / "trashed-b"))))
    assert dest == str(target / "b") and trashed == [str(target / "b")]
    assert (target / "b" / "g.txt").read_text() == "g" and not (target / "b" / "mine.txt").exists()
    assert (tmp_path / "trashed-b" / "mine.txt").read_text() == "current"

    # rename-current: current kept under "(before restore …)", restored copy gets the name
    import shutil
    shutil.rmtree(target / "b")
    make_current()
    dest = r.restore(snap, folder, str(target), conflict="rename-current")
    assert dest == str(target / "b") and (target / "b" / "g.txt").exists()
    kept = [p for p in os.listdir(target) if p.startswith("b (before restore ")]
    assert len(kept) == 1 and (target / kept[0] / "mine.txt").read_text() == "current"

    # rename (default): restored copy gets the new name, current untouched
    dest = r.restore(snap, folder, str(target))
    assert "(restored " in dest and os.path.exists(os.path.join(dest, "g.txt"))
    assert not [p for p in os.listdir(target) if p.startswith(".b.restoring")]   # no temp left


def test_failed_restore_keeps_current(repo, tmp_path, monkeypatch):
    r, data, _ = repo
    snap = r.snapshots()[0]
    g = r.lookup(snap, str(data / "a" / "b" / "g.txt"))
    (tmp_path / "g.txt").write_text("precious")

    def boom(*a, **k):
        partial = a[2] if len(a) > 2 else k.get("dest")
        open(partial, "w").close()                     # leaves a partial temp file
        raise RuntimeError("network died")
    monkeypatch.setattr(Restic, "_restore_into", lambda self, s, n, d, *a: boom(s, n, d))
    with pytest.raises(RuntimeError):
        r.restore(snap, g, str(tmp_path), conflict="rename-current")
    assert (tmp_path / "g.txt").read_text() == "precious"
    assert sorted(os.listdir(tmp_path)) == ["g.txt"]   # temp cleaned up, nothing renamed


def test_journal_recovers_interrupted_restores(tmp_path):
    from resticcontrol.backend import RestoreJournal
    j = RestoreJournal(str(tmp_path / "journal.json"))
    # 1) crashed while restic was still writing: partial temp is removed
    partial = tmp_path / ".big.app.restoring-1-1"
    (partial / "Contents").mkdir(parents=True)
    j.begin(str(partial), str(tmp_path / "big.app"))
    # 2) crashed between the two renames: the complete temp takes its name
    done = tmp_path / ".doc.txt.restoring-1-2"
    done.write_text("complete")
    j.swapping(str(done), str(tmp_path / "doc.txt"))
    # 3) a bogus entry must never delete a real file
    (tmp_path / "keep.txt").write_text("mine")
    j.begin(str(tmp_path / "keep.txt"), str(tmp_path / "x"))

    notes = RestoreJournal(str(tmp_path / "journal.json")).recover()   # "next launch"
    assert not partial.exists()
    assert (tmp_path / "doc.txt").read_text() == "complete" and not done.exists()
    assert (tmp_path / "keep.txt").read_text() == "mine"
    assert len(notes) == 2 and j.pending() == {}


def test_failed_new_name_restore_leaves_nothing_visible(repo, tmp_path, monkeypatch):
    from resticcontrol.backend import RestoreJournal
    r, data, _ = repo
    snap = r.snapshots()[0]
    folder = r.lookup(snap, str(data / "a" / "b"))
    (tmp_path / "b").mkdir()

    def boom(self, s, n, d, *a):
        os.makedirs(d)
        open(os.path.join(d, "half"), "w").close()
        raise RuntimeError("connection lost")
    monkeypatch.setattr(Restic, "_restore_into", boom)
    j = RestoreJournal(str(tmp_path / "j" / "journal.json"))
    with pytest.raises(RuntimeError):
        r.restore(snap, folder, str(tmp_path), conflict="rename", journal=j)
    assert sorted(os.listdir(tmp_path)) == ["b", "j"] and j.pending() == {}


def test_restore_size_and_space_check(series, tmp_path):
    from resticcontrol.backend import space_shortfalls
    r, data, _ = series
    snap = r.snapshots()[0]
    proj = r.lookup(snap, str(data / "proj"))
    seen = []
    # newest proj/: a.txt "a2-changed" (10) + sub/deep.txt "d2-changed" (10) + keep/k.txt "k" (1)
    assert r.restore_size(snap, proj, progress=lambda n, b: seen.append((n, b))) == 21
    assert seen[-1] == (3, 21)
    assert r.restore_size(snap, r.lookup(snap, str(data / "proj" / "a.txt"))) == 10
    assert r.restore_size(snap, proj) == 21                          # cached

    gb = 1024 ** 3
    ok = space_shortfalls([(str(tmp_path), 5 * gb)], free=lambda p: 10 * gb)
    assert ok == []
    short = space_shortfalls([(str(tmp_path), 6 * gb), (str(tmp_path / "sub"), 4 * gb)],
                             free=lambda p: 10 * gb)           # same volume: 10 GB + margin
    assert len(short) == 1 and short[0][1] > 10 * gb and short[0][2] == 10 * gb
    tiny = space_shortfalls([(str(tmp_path), 10)], free=lambda p: 50 * 1024 * 1024)
    assert len(tiny) == 1                                       # 100 MB minimum margin


def test_in_place_plan_and_restore(series, tmp_path):
    r, data, _ = series
    if r.restic_version() < (0, 17, 0):
        pytest.skip("needs restic >= 0.17")
    snap = r.snapshots()[0]
    proj = r.lookup(snap, str(data / "proj"))
    dest = tmp_path / "proj"
    r.restore(snap, proj, str(tmp_path))                       # current copy == backup
    plan = r.in_place_plan(snap, proj, str(dest))
    assert plan["bytes"] == 0 and plan["skipped"] == 3           # folders may count as items
    (dest / "a.txt").write_text("edited since the backup")       # 23 bytes on disk
    (dest / "sub" / "deep.txt").unlink()
    (dest / "only-on-disk.txt").write_text("keep me")
    plan = r.in_place_plan(snap, proj, str(dest))
    assert plan["bytes"] == 20 and plan["files"] >= 2            # a.txt (10) + deep.txt (10)
    assert (dest / "a.txt").read_text() == "edited since the backup"   # dry run changed nothing
    r.restore_in_place(snap, proj, str(dest))
    assert (dest / "a.txt").read_text() == "a2-changed"
    assert (dest / "sub" / "deep.txt").read_text() == "d2-changed"
    assert (dest / "only-on-disk.txt").read_text() == "keep me"  # never deleted


def test_snapshot_totals_bound_and_root(series):
    r, data, _ = series
    snap = r.snapshots()[0]
    if r.restic_version() < (0, 17, 0):
        assert snap.total_bytes is None                  # older restic: no summary
        return
    assert snap.total_bytes is not None and snap.total_files is not None
    assert snap.is_root(str(data)) and snap.is_root(str(data) + "/")
    assert not snap.is_root(str(data / "proj"))
    root = r.lookup(snap, str(data))
    assert r.restore_size(snap, root) == snap.total_bytes      # exact for a backup root
    assert r.restore_size(snap, r.lookup(snap, str(data / "proj"))) <= snap.total_bytes


def test_restore_reports_progress(series, tmp_path):
    r, data, _ = series
    snap = r.snapshots()[0]
    proj = r.lookup(snap, str(data / "proj"))
    seen = []
    dest = r.restore(snap, proj, str(tmp_path), progress=seen.append)
    assert os.path.exists(os.path.join(dest, "a.txt"))
    # restic >= 0.17 streams JSON status (tiny restores may finish before the first one)
    assert all(m.get("message_type") == "status" for m in seen)


def test_in_place_exact_deletes_extras(series, tmp_path):
    r, data, _ = series
    if r.restic_version() < (0, 17, 0):
        pytest.skip("needs restic >= 0.17")
    snap = r.snapshots()[0]
    proj = r.lookup(snap, str(data / "proj"))
    dest = tmp_path / "proj"
    r.restore(snap, proj, str(tmp_path))
    (dest / "a.txt").write_text("edited")
    (dest / "added-since.txt").write_text("new")
    r.restore_in_place(snap, proj, str(dest), delete=True)
    assert (dest / "a.txt").read_text() == "a2-changed"
    assert not (dest / "added-since.txt").exists()                  # exactly like the backup
    assert sorted(os.listdir(dest)) == ["a.txt", "keep", "sub"]


def test_folder_size_is_remembered(series, tmp_path):
    from resticcontrol.backend import local_size
    r, data, root = series
    snap = r.snapshots()[0]
    proj = r.lookup(snap, str(data / "proj"))
    fresh = Restic(RepoSpec(repository=r.spec.repository, password="pw"),
                   cache_dir=str(root / "cache-sizes"))
    assert fresh.cached_size(snap, proj) is None
    assert fresh.restore_size(snap, proj) == 21
    again = Restic(RepoSpec(repository=r.spec.repository, password="pw"),
                   cache_dir=str(root / "cache-sizes"))              # "after a restart"
    assert again.cached_size(snap, proj) == 21
    if snap.total_bytes is not None:                                 # restic >= 0.17
        assert again.cached_size(snap, r.lookup(snap, str(data))) == snap.total_bytes
    fresh.close()
    again.close()
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "x").write_bytes(b"12345")
    (tmp_path / "d" / "e" / "y").write_bytes(b"123")
    os.symlink("/", str(tmp_path / "d" / "link-to-root"))           # not followed
    seen = []
    assert local_size(str(tmp_path / "d"), progress=lambda f, b: seen.append((f, b))) == 8 + \
        os.lstat(str(tmp_path / "d" / "link-to-root")).st_size
    assert seen[-1][0] == 3


def test_reveal_chain_and_path_normalisation():
    from resticcontrol.backend import normalize_local_path, reveal_chain
    assert normalize_local_path("/System/Volumes/Data/Users/me/Doc/") == "/Users/me/Doc"
    assert normalize_local_path("/Users/me/") == "/Users/me"
    roots = ["/Users/me", "/Users/me/Projects", "/Volumes/Photos"]
    assert reveal_chain("/Users/me/Projects/app/main.py", roots) == \
        ("/Users/me/Projects", ["/Users/me/Projects/app", "/Users/me/Projects/app/main.py"])
    assert reveal_chain("/Users/me", roots) == ("/Users/me", [])
    assert reveal_chain("/System/Volumes/Data/Users/me/Documents", roots) == \
        ("/Users/me", ["/Users/me/Documents"])
    assert reveal_chain("/Users/meg/x", roots) is None          # prefix of a name ≠ inside
    assert reveal_chain("/etc/hosts", roots) is None
