""""What changed" tree: backend comparison per level, recursive totals, and the
window's data-source logic (with Cocoa stubbed)."""
import os
import shutil
import subprocess
import time

import pytest

from resticcontrol.backend import ChangeTotals, Restic, RepoSpec, fmt_counts

needs_restic = pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("chg")
    repo_dir, data = root / "repo", root / "data"
    env = dict(os.environ, RESTIC_REPOSITORY=str(repo_dir), RESTIC_PASSWORD="pw")
    sh = lambda *a: subprocess.run(["restic", "-q", *a], env=env, check=True)
    sh("init")
    top = data / "top"
    for d in ("a/deep", "b", "c"):
        (top / d).mkdir(parents=True)
    (top / "a/deep/x").write_text("1")
    (top / "b/y").write_text("1")
    (top / "c/z").write_text("z")
    (top / "same.txt").write_text("s")
    sh("backup", str(data)); time.sleep(1.1)
    (top / "a/deep/x").write_text("22")
    shutil.rmtree(top / "c")
    (top / "new/sub").mkdir(parents=True)
    (top / "new/sub/n").write_text("n")
    (top / "new/m").write_text("m")
    os.utime(top / "b/y", (1e9, 1e9))                 # dates only
    sh("backup", str(data))
    r = Restic(RepoSpec(repository=str(repo_dir), password="pw"), cache_dir=str(root / "cache"))
    new, old = r.snapshots()[:2]
    yield r, str(top), new, old
    r.close()


@needs_restic
def test_change_entries_levels(repo):
    r, top, new, old = repo
    got = {e.name: e.status for e in r.change_entries(new, old, top)}
    assert got == {"a": "changed", "b": "changed", "c": "removed", "new": "added"}
    assert "same.txt" not in got
    # one level down
    assert {e.name: e.status for e in r.change_entries(new, old, top + "/a")} == {"deep": "changed"}
    x = r.change_entries(new, old, top + "/a/deep")[0]
    assert (x.status, x.old.size, x.new.size) == ("changed", 1, 2)
    assert {e.name: e.status for e in r.change_entries(new, old, top + "/b")} == {"y": "meta"}
    # an added folder lists everything as added, a removed one as removed
    assert {e.name: e.status for e in r.change_entries(new, old, top + "/new")} == \
        {"sub": "added", "m": "added"}
    removed = r.change_entries(new, old, top + "/c")
    assert [(e.name, e.status, e.new) for e in removed] == [("z", "removed", None)]
    assert r.change_entries(new, new, top) == []


@needs_restic
def test_change_totals(repo):
    r, top, new, old = repo
    if r.restic_version() < (0, 16, 0):
        assert r.change_totals(old, new, top) is None
        return
    seen = []
    t = r.change_totals(old, new, top, progress=seen.append)
    assert t.complete and seen
    assert t.of(top) == (1, 2, 1)
    assert t.of(top + "/a") == (1, 0, 0) and t.of(top + "/a/deep") == (1, 0, 0)
    assert t.of(top + "/new") == (0, 2, 0) and t.of(top + "/new/sub") == (0, 1, 0)
    assert t.of(top + "/c") == (0, 0, 1)
    assert t.of(top + "/b") == (0, 0, 0)               # dates only
    assert r.change_totals(old, new, top) is t          # cached
    # the same diff knows every changed item of every folder: the tree needs no restic call
    kids = lambda p: {n: st for n, st, _t in t.children(p)}
    assert kids(top) == {"a": "changed", "b": "changed", "c": "removed", "new": "added"}
    assert kids(top + "/a") == {"deep": "changed"} and kids(top + "/a/deep") == {"x": "changed"}
    assert kids(top + "/b") == {"y": "meta"}
    assert kids(top + "/new") == {"sub": "added", "m": "added"}
    assert t.children("/elsewhere") is None
    calls = []
    orig = r.run
    r.run = lambda *a, **k: calls.append(a) or orig(*a, **k)
    try:
        quick = r.quick_change_entries(new, old, top + "/a/deep", r.cached_totals(old, new, top))
    finally:
        r.run = orig
    assert not calls and [(e.name, e.status, e.type) for e in quick] == [("x", "changed", "file")]
    assert r.diff_stats(old, new, top) == {"changed": 1, "added": 2, "removed": 1}


def test_totals_children_and_cap(monkeypatch):
    t = ChangeTotals("/base")
    for p, m in [("/d/", "U"), ("/d/f", "U"), ("/e/", "U"), ("/e/sub/g", "M"), ("/n/", "+"), ("/n/x", "+")]:
        t.add(p, m)
    t.complete = True
    got = {n: (st, typ) for n, st, typ in t.children("/base")}
    # a folder with only date changes inside it is "meta"; one with real changes "changed"
    assert got == {"d": ("changed", "dir"), "e": ("changed", "dir"), "n": ("added", "dir")}
    assert {n: st for n, st, _ in t.children("/base/d")} == {"f": "meta"}
    t2 = ChangeTotals("/")
    t2.add("/only-dates/", "U")
    t2.complete = True
    assert t2.children("/") == [("only-dates", "meta", "dir")]
    # huge diffs keep only the counts
    monkeypatch.setattr(ChangeTotals, "MAX_PATHS", 3)
    t3 = ChangeTotals("/")
    for k in range(5):
        t3.add(f"/f{k}", "+")
    t3.complete = True
    assert t3.children("/") is None and t3.of("/") == (0, 5, 0)


def test_totals_parsing_and_text():
    t = ChangeTotals("/")
    for p, m in [("/a/", "+"), ("/a/f", "+"), ("/a/b/g", "M"), ("/h", "-"), ("/a/u", "U")]:
        t.add(p, m)
    assert t.of("/") == (1, 1, 1) and t.of("/a") == (1, 1, 0) and t.of("/a/b") == (1, 0, 0)
    assert t.of("/zzz") is None                         # not known until complete
    t.complete = True
    assert t.of("/zzz") == (0, 0, 0)
    assert fmt_counts((1, 0, 0)) == "1 changed file"
    assert fmt_counts((3, 2, 0)) == "3 changed, 2 added files"
    assert "dates" in fmt_counts((0, 0, 0))
    assert fmt_counts(None) == ""


class FakeOutline:
    """Just enough of NSOutlineView, driven by the real data source, to check the tree."""

    def __init__(self, src):
        self.src, self.open = src, set()

    def _flat(self):
        out = []

        def walk(item):
            n = self.src.outlineView_numberOfChildrenOfItem_(self, item)
            for k in range(n):
                child = self.src.outlineView_child_ofItem_(self, k, item)
                out.append(child)
                if child in self.open:
                    walk(child)
        walk(None)
        return out

    def names(self):
        return [self.src.rows[it.i][1].name for it in self._flat()]

    def isItemExpanded_(self, item):
        return item in self.open

    def expandItem_(self, item):
        if item in self.open or not self.src.outlineView_isItemExpandable_(self, item):
            return
        self.open.add(item)
        note = type("N", (), {"userInfo": lambda s: {"NSObject": item}})()
        self.src.outlineViewItemWillExpand_(note)

    def collapseItem_(self, item):
        if item in self.open:
            self.open.discard(item)
            note = type("N", (), {"userInfo": lambda s: {"NSObject": item}})()
            self.src.outlineViewItemDidCollapse_(note)

    def reloadData(self): pass
    def reloadItem_reloadChildren_(self, item, children): pass
    def reloadItem_(self, item): pass
    def numberOfColumns(self): return 0
    def numberOfRows(self): return len(self._flat())
    def itemAtRow_(self, r): return self._flat()[r]
    def rowForItem_(self, item):
        flat = self._flat()
        return flat.index(item) if item in flat else -1


def _main(app, r, new, old, top, monkeypatch):
    """A MainController without a window, showing the two versions of *top*."""
    from unittest.mock import MagicMock
    monkeypatch.setattr(app, "run_async", lambda fn, done, fail=None: done(fn()))
    monkeypatch.setattr(app.AppHelper, "callAfter", lambda f, *a, **k: f(*a, **k))
    m = app.MainController()
    m.restic, m.stack, m.historyGeneration = r, [], 0
    m.status = MagicMock()
    m.jobStarted = m.jobFinished = lambda *a: None
    m.previewPanelActive = False
    m.pendingState = None
    sel = []
    m.selectedIndexes = lambda: list(sel)
    m.selectIndexes = lambda c: sel.__setitem__(slice(None), list(c))
    m.versionsSource = app.VersionsSource().setup(m)
    m.table = FakeOutline(m.versionsSource)
    node = r.lookup(new, top)
    m.versionsSource.setData([(new, node, 1), (old, node, 1)],
                             [{"prev": old, "final": True}, {"prev": None, "final": True}])
    return m, sel


@needs_restic
def test_tree_in_right_pane(app, repo, monkeypatch):
    r, top, new, old = repo
    m, sel = _main(app, r, new, old, top, monkeypatch)
    src, tv = m.versionsSource, m.table
    names = tv.names
    idx = lambda name: [it.i for it in tv._flat() if src.rows[it.i][1].name == name][0]
    assert m.canExpand(0) and not m.canExpand(1)          # the oldest has nothing to compare with
    m.toggleRow(0)
    assert names() == ["top", "a", "b", "c", "new", "top"]
    assert src.extra[idx("c")]["status"] == "removed" and src.rows[idx("c")][0] is old
    assert src.rows[idx("new")][0] is new
    # the diff has run now (it also gives the "in total" counts): opening "a" needs no
    # restic call for the structure, and follows its only change down to the file
    assert r.cached_totals(old, new, top) is not None or r.restic_version() < (0, 16, 0)
    m.toggleRow(idx("a"))
    assert names() == ["top", "a", "deep", "x", "b", "c", "new", "top"]
    assert src.text(idx("x"), "change").startswith("changed")
    assert src.rows[idx("x")][1].size == 2              # sizes / dates filled in afterwards
    assert src.extra[idx("x")]["entry"].old.size == 1
    if r.restic_version() >= (0, 16, 0):
        assert src.text(idx("a"), "change") == "changed  ·  1 changed file"
        assert src.text(idx("b"), "change") == "changed  ·  no file changes (dates/permissions only)"
    # "b" only has a date change inside: shown (dimmed), not expandable further
    m.toggleRow(idx("b"))
    y = idx("y")
    assert src.extra[y]["status"] == "meta" and not m.canExpand(y)
    # closing and reopening keeps the loaded children (no second restic call)
    n_rows = len(src.rows)
    m.toggleRow(0)
    assert names() == ["top", "top"]
    m.toggleRow(0)
    assert len(src.rows) == n_rows and "deep" in names()
    # double-click on a folder in the tree that exists on this Mac: go there on the left
    shown = []
    m.revealPath = shown.append
    sel[:] = [idx("new")]
    m.rowDoubleClicked_(None)
    assert shown == [top + "/new"]
    sel[:] = [idx("c")]              # removed from this Mac: revealPath shows the folder above
    m.rowDoubleClicked_(None)
    assert shown == [top + "/new", top + "/c"] and "z" not in names()
    # the tree rows are ordinary rows: a removed item comes from the older snapshot
    sel[:] = [idx("c")]
    assert m.selectedRows()[0][0] is old
    # double-click on a folder version closes / opens it
    sel[:] = [0]
    m.rowDoubleClicked_(None)
    assert names() == ["top", "top"]
    m.rowDoubleClicked_(None)
    # sorting keeps each change tree under its version
    src.sortKey = ("name", False)
    src.applySort()
    level1 = [src.rows[it.i][1].name for it in tv._flat() if src.extra[it.i].get("depth") == 1]
    assert names()[0] == "top" and names()[-1] == "top" and level1 == ["new", "c", "b", "a"]
    assert names()[names().index("b") + 1] == "y"          # b stays open, y under it


def _col(ident):
    from unittest.mock import MagicMock
    c = MagicMock()
    c.identifier.return_value = ident
    return c


@needs_restic
def test_partial_plan_is_not_a_version_of_the_parent(tmp_path):
    """Two Backrest plans: one backs up the whole folder, one only a subfolder.  The
    subfolder-only backups must not count as versions of the whole folder."""
    from resticcontrol.backend import plan_of, snapshots_for_folder
    env = dict(os.environ, RESTIC_REPOSITORY=str(tmp_path / "repo"), RESTIC_PASSWORD="pw")
    sh = lambda *a: subprocess.run(["restic", "-q", *a], env=env, check=True)
    docs = tmp_path / "Documents"
    (docs / "Project").mkdir(parents=True)
    (docs / "Project" / "p.txt").write_text("p")
    (docs / "letter.txt").write_text("l")
    sh("init")
    sh("backup", "--tag", "plan:home", str(docs)); time.sleep(1.1)
    sh("backup", "--tag", "plan:project", str(docs / "Project")); time.sleep(1.1)
    (docs / "Project" / "p.txt").write_text("p2")
    sh("backup", "--tag", "plan:project", str(docs / "Project")); time.sleep(1.1)
    sh("backup", "--tag", "plan:home", str(docs))
    r = Restic(RepoSpec(repository=str(tmp_path / "repo"), password="pw"),
               cache_dir=str(tmp_path / "cache"))
    try:
        snaps = r.snapshots()
        whole = snapshots_for_folder(snaps, str(docs))
        assert {plan_of(s) for s in whole} == {"home"} and len(whole) == 2
        versions = r.folder_versions(whole, str(docs))
        assert len(versions) == 2                          # the change, not delete/re-add cycles
        ch = r.change_entries(versions[0].newest, versions[1].newest, str(docs))
        assert [(e.name, e.status) for e in ch] == [("Project", "changed")]
        # the subfolder itself is in every backup of both plans
        assert len(snapshots_for_folder(snaps, str(docs / "Project"))) == 4
        # above every backup root only partial snapshots exist: those are used
        assert len(snapshots_for_folder(snaps, str(tmp_path))) == 4
    finally:
        r.close()


@needs_restic
def test_new_backup_appears_without_resetting_the_view(app, tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    env = dict(os.environ, RESTIC_REPOSITORY=str(tmp_path / "repo"), RESTIC_PASSWORD="pw")
    sh = lambda *a: subprocess.run(["restic", "-q", *a], env=env, check=True)
    top = tmp_path / "top"
    (top / "a" / "deep").mkdir(parents=True)
    (top / "a" / "deep" / "x").write_text("1")
    (top / "b.txt").write_text("b")
    sh("init")
    sh("backup", str(top)); time.sleep(1.1)
    (top / "a" / "deep" / "x").write_text("22")
    sh("backup", str(top)); time.sleep(1.1)
    r = Restic(RepoSpec(repository=str(tmp_path / "repo"), password="pw"),
               cache_dir=str(tmp_path / "cache"))
    try:
        snaps = r.snapshots()
        new, old = snaps[:2]
        m, sel = _main(app, r, new, old, str(top), monkeypatch)
        tv, src = m.table, m.versionsSource
        for k in ("pathLabel", "rightColumns", "collapseBox", "qlBtn", "revealBtn", "restoreBtn",
                  "restoreHereBtn", "backBtn", "hostPopup", "snapPopup"):
            setattr(m, k, MagicMock())
        m.hostPopup.titleOfSelectedItem.return_value = None
        m.snapshots = snaps
        m.visibleSnapshots = lambda: list(m.snapshots)
        m.hostFilter = lambda: None
        m.singleSnapshot = lambda: None
        m.rebuildHostPopup = m.rebuildSnapshotPopup = lambda: None
        m.foldersFirst, m.hideUnchanged, m.onlyChanges = True, True, False
        item = app.make_item(str(top), "top")
        m.showHistory(item)
        assert tv.names() == ["top", "top"]
        m.toggleRow(0)                                     # opens a, deep (single path) too
        x = [it.i for it in tv._flat() if src.rows[it.i][1].name == "x"][0]
        sel[:] = [x]
        before = tv.names()
        # a new backup arrives (b.txt changed): quietly added on top, nothing else moves
        (top / "b.txt").write_text("bb")
        sh("backup", str(top))
        assert r.snapshot_ids() != {s.id for s in snaps}
        m.applySnapshots(r.snapshots())
        names = tv.names()
        assert names[0] == "top" and names[1:] == before     # new version on top, closed
        assert not tv.isItemExpanded_(src.items[src.top[0]])
        assert [src.rows[i][1].name for i in sel] == ["x"]  # the selection is kept
        # the same list again: nothing happens
        rows_before = list(src.rows)
        m.applySnapshots(r.snapshots())
        assert src.rows == rows_before
    finally:
        r.close()
