"""Revealing a path from Finder: level-by-level loading, hidden folders, deleted files."""
import os
from unittest.mock import MagicMock


def _world(app, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    alerts = []
    monkeypatch.setattr(app, "alert", lambda t, x="": alerts.append(t))
    statuses = []
    from resticcontrol.backend import MergedEntry, Node

    m = app.MainController()                     # no window: set only what reveal uses
    for k in ("window", "tabs", "snapPopup", "outline"):
        setattr(m, k, MagicMock())
    m.pendingReveal, m.showHidden, m.foldersFirst, m.leftSort = None, False, True, ("name", True)
    m.current_item = None
    m.outlineSource = app.OutlineSource().setup(m)
    m.selectedOutlineItems = lambda: []
    m.snapshots = [object()]; m.singleSnapshot = lambda: None; m.hostFilter = lambda: None
    m.status = MagicMock(); m.status.setStringValue_.side_effect = lambda s: statuses.append(s)
    src = m.outlineSource
    D = lambda p: Node(name=os.path.basename(p) or p, path=p, type="dir", size=None, mtime=None)
    Fi = lambda p: Node(name=os.path.basename(p), path=p, type="file", size=1, mtime=None)
    tree = {"/Users/me": [D("/Users/me/Documents"), D("/Users/me/Library")],
            "/Users/me/Documents": [Fi("/Users/me/Documents/a.txt")],
            "/Users/me/Library": [D("/Users/me/Library/Mail")],
            "/Users/me/Library/Mail": [Fi("/Users/me/Library/Mail/x.emlx")]}
    loads = []
    def fake_load(it):                           # async in the app: record, answer later
        if it.path not in loads: loads.append(it.path)
    src.load = fake_load
    def finish_loads():
        while loads:
            p = loads.pop(0); it = find(p)
            nodes = [n for n in tree.get(p, [])]
            if p == "/Users/me":
                nodes[1] = Node(name="Library", path="/Users/me/Library", type="dir", size=None, mtime=None, hidden_flag=True)
            src.setChildren(it, [MergedEntry(node=n, newest=None) for n in nodes])
            if m.pendingReveal: m.continueReveal()
    def find(p, it=None):
        it = it or src.root
        if it.path == p: return it
        for c in it.children or []:
            r = find(p, c)
            if r: return r
    src.root.children = []
    root_item = app.make_item("/Users/me", "/Users/me", MergedEntry(node=D("/Users/me"), newest=None))
    src.root.children = [root_item]
    selected = []
    m.selectOutlineItem = lambda it: selected.append(it.path)
    m.outline.expandItem_.side_effect = lambda it: (it.children is None) and fake_load(it)
    m.toggleHiddenFiles_ = lambda s: (setattr(m, "showHidden", True), src.resortAll())
    m.showHidden = False

    return m, finish_loads, selected, alerts, statuses


def test_reveal_file(app, tmp_path, monkeypatch):
    m, finish, selected, alerts, _ = _world(app, tmp_path, monkeypatch)
    m.revealPath("/Users/me/Documents/a.txt"); finish()
    assert selected == ["/Users/me/Documents/a.txt"] and not m.showHidden and not alerts


def test_reveal_through_hidden_folder(app, tmp_path, monkeypatch):
    m, finish, selected, alerts, _ = _world(app, tmp_path, monkeypatch)
    m.revealPath("/System/Volumes/Data/Users/me/Library/Mail/x.emlx"); finish()
    assert selected == ["/Users/me/Library/Mail/x.emlx"] and m.showHidden


def test_reveal_deleted_file_shows_folder(app, tmp_path, monkeypatch):
    m, finish, selected, alerts, statuses = _world(app, tmp_path, monkeypatch)
    m.revealPath("/Users/me/Documents/deleted.txt"); finish()
    assert selected == ["/Users/me/Documents"]
    assert any("no longer on this Mac" in s for s in statuses)


def test_reveal_outside_backup(app, tmp_path, monkeypatch):
    m, finish, selected, alerts, _ = _world(app, tmp_path, monkeypatch)
    m.revealPath("/etc/hosts"); finish()
    assert selected == [] and alerts == ["Not in the backup"]


def test_open_folders_follow_the_disk(app, tmp_path, monkeypatch):
    """A file restored next to its original (or created / deleted by anyone) shows up in
    the open folders on the left; nothing else in the tree changes."""
    from resticcontrol.backend import MergedEntry, list_local, local_node
    monkeypatch.setattr(app, "run_async", lambda fn, done, fail=None: done(fn()))
    d = tmp_path / "docs"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("a")
    m = app.MainController()
    m.outline, m.restic, m.treeGeneration = MagicMock(), object(), 0
    m.showHidden, m.foldersFirst, m.leftSort = False, True, ("name", True)
    m.singleSnapshot = lambda: None
    m.selectedOutlineItems = lambda: []
    m.outline.isItemExpanded_.return_value = True
    src = app.OutlineSource().setup(m)
    folder = app.make_item(str(d), "docs", MergedEntry(node=local_node(str(d)), newest=None))
    src.root.children = [folder]
    folder.dirMtime = app.dir_mtime(str(d))
    src.setChildren(folder, [MergedEntry(node=n, newest=None) for n in list_local(str(d))])
    a_item = folder.children[1]
    assert [c.name for c in folder.children] == ["sub", "a.txt"]
    m.outline.reset_mock()
    src.refreshOpen()                                   # nothing changed: no reload
    assert not m.outline.reloadItem_reloadChildren_.called
    (d / "a (restored 2026-09-30).txt").write_text("old a")
    os.utime(d, ns=(folder.dirMtime + 10**9, folder.dirMtime + 10**9))   # coarse clocks
    src.refreshOpen()
    assert [c.name for c in folder.children] == ["sub", "a (restored 2026-09-30).txt", "a.txt"]
    assert folder.children[2] is a_item                 # same item: selection/expansion survive
    m.outline.reloadItem_reloadChildren_.assert_called_with(folder, True)
    # after a restore every open folder is re-listed, whatever its modification time
    (d / "b.txt").write_text("b")
    os.utime(d, ns=(folder.dirMtime, folder.dirMtime))
    src.refreshOpen(force=True)
    assert "b.txt" in [c.name for c in folder.children]
