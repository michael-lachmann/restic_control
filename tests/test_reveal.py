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
