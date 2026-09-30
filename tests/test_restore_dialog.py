"""Restore dialog logic (RestoreFlow + RestoreSheet.mode) with Cocoa stubbed out."""
from unittest.mock import MagicMock



def _setup(app):
    import os, tempfile
    from datetime import datetime, timezone
    from resticcontrol.backend import Node, Snapshot
    app.fmt_size = lambda n: f"{n/1024**3:.2f} GB" if n >= 1024**3 else f"{n/1024**2:.0f} MB"
    app.run_async = lambda *a, **k: None
    GB, MB = 1024 ** 3, 1024 ** 2

    class W:                                   # fake control
        def __init__(s, st=0): s.st, s.en, s.val = st, True, ""
        def state(s): return s.st
        def setState_(s, v): s.st = v
        def setEnabled_(s, v): s.en = bool(v)
        def isEnabled(s): return s.en
        def setToolTip_(s, t): pass
        def setStringValue_(s, v): s.val = v
        def setHidden_(s, v): pass
        def isIndeterminate(s): return True
        def setIndeterminate_(s, v): pass
        def setMinValue_(s, v): pass
        def setMaxValue_(s, v): pass
        def setDoubleValue_(s, v): pass

    def fake_sheet(flow, window, clash, folder, in_place_ok):
        sh = app.RestoreSheet()
        sh.flow, sh.clash, sh.folder, sh.in_place_ok, sh.userTouched = flow, clash, folder, in_place_ok, False
        for k in ("title", "live", "bar", "note", "restoreBtn", "keepVerdict", "onlyVerdict"):
            setattr(sh, k, W())
        sh.rKeep, sh.rKeepRename, sh.rKeepNew, sh.rOnly = W(1), W(1), W(0), W(0)
        sh.rUpdate, sh.rExact, sh.cSafe = (W(1 if in_place_ok else 0), W(0 if in_place_ok else 1), W(1)) if folder else (None, None, None)
        sh.show = lambda: None
        return sh
    class F:
        @staticmethod
        def alloc():
            class X:
                def init(self): return self
                def setup(self, *a): return fake_sheet(*a)
            return X()
    app.RestoreSheet.alloc = F.alloc   # used by RestoreFlow

    class FakeRestic:
        known = {}
        def restic_version(self): return (0, 18, 1)
        def cancel(self, s): pass
        def cached_size(self, snap, node): return self.known.get(node.path)

    def flow(free, total=None, clash=True):
        t = tempfile.mkdtemp(); os.makedirs(os.path.join(t, "Library"))
        snap = Snapshot(id="s"*64, short_id="s", time=datetime.now(timezone.utc), hostname="h", username="",
                        paths=("/Users/me",), tags=(), tree="t", total_bytes=total)
        node = Node(name="Library", path="/Users/me/Library", type="dir", size=None, mtime=None)
        main = MagicMock(); main.restic = FakeRestic()
        app.volume_free = lambda p: free; app.volume_name = lambda p: "RestoreTest"
        f = app.RestoreFlow(main, [], [(snap, node, t)], {os.path.join(t, "Library")} if clash else set())
        f.start(); return f

    
    flow.FakeRestic = FakeRestic
    return flow, GB, MB


def _state(f):
    sh = f.sheet
    return sh.mode(), sh.restoreBtn.en, bool(sh.cSafe.st), sh.cSafe.en, sh.note.val


def test_screenshot_case_nothing_fits(app):
    flow, GB, MB = _setup(app)
    f = flow(313 * MB)
    assert _state(f)[:2] == ("rename-current", True)          # can start while calculating
    f.update(0, 400 * MB, 380 * MB, 0.02)                      # passes the free space
    mode, enabled, safe, safe_en, note = _state(f)
    assert mode == "update" and not enabled and not safe and not safe_en
    assert "Nothing fits" in note


def test_in_place_suggested_when_both_copies_dont_fit(app):
    flow, GB, MB = _setup(app)
    g = flow(500 * GB)
    g.done_job(0, 800 * GB, 20 * GB, 3000); g.measured(True)
    mode, enabled, safe, safe_en, note = _state(g)
    assert mode == "update" and enabled and not safe and not safe_en and note == ""
    assert g.sheet.onlyVerdict.val.startswith("✓")
    g.sheet.rExact.setState_(1); g.sheet.rUpdate.setState_(0); g.sheet.userTouched = True
    g.render()
    assert _state(g)[:2] == ("exact-unsafe", True)


def test_user_choice_is_not_overridden(app):
    flow, GB, MB = _setup(app)
    g = flow(500 * GB)
    g.sheet.userTouched = True                                  # user looked at the options
    g.done_job(0, 800 * GB, 20 * GB, 3000); g.measured(True)
    mode, enabled, *_ = _state(g)
    assert mode == "rename-current" and not enabled             # stays, but can't be started


def test_snapshot_total_fits_skips_measuring(app):
    flow, GB, MB = _setup(app)
    h = flow(2000 * GB, total=1000 * GB)
    assert not h.measuring and _state(h)[:2] == ("rename-current", True)
    assert h.sheet.keepVerdict.val.startswith("✓")


def test_safe_replace_when_both_fit(app):
    flow, GB, MB = _setup(app)
    g = flow(2000 * GB)
    g.done_job(0, 800 * GB, 20 * GB, 3000); g.measured(True)
    g.sheet.select_only(); g.sheet.rExact.setState_(1); g.sheet.rUpdate.setState_(0)
    g.sheet.userTouched = True; g.render()
    mode, enabled, safe, safe_en, _ = _state(g)
    assert mode == "replace" and enabled and safe and safe_en   # safe copy is the default


def test_size_calculated_beforehand_makes_dialog_instant(app):
    flow, GB, MB = _setup(app)
    flow.FakeRestic.known = {"/Users/me/Library": 55 * MB}      # from "Calculate Size"
    try:
        f = flow(256 * MB)                                       # no snapshot total
        assert not f.measuring and f.final == [True]
        assert f.sheet.keepVerdict.val.startswith("✓ needs")
        assert _state(f)[:2] == ("rename-current", True)
        f.sheet.select_only(); f.sheet.userTouched = True; f.render()      # update in place
        assert "at most" in f.sheet.onlyVerdict.val                  # diff not measured: honest
    finally:
        flow.FakeRestic.known = {}
