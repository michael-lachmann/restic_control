"""Cocoa (PyObjC) user interface for Restic Control.

Layout
    ┌ top bar: repository ▾  Settings…   host ▾  snapshot ▾  ⟳   (spinner) status ┐
    ├ tabs: [Restore] [Manage]                                                    ┤
    │ Restore:  outline (Finder-like tree) │ versions / snapshot browser table    │
    │                                      │ [‹ Back] path            [QL] [Restore…]
    │ Manage:   WKWebView (Backrest)                                            │
    └───────────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import os
import posixpath
import socket
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import objc
from AppKit import (
    NSAlert, NSApp, NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
    NSButton, NSByteCountFormatter, NSColor, NSFont, NSImage, NSImageCell, NSImageView,
    NSLayoutConstraint, NSLineBreakByTruncatingMiddle, NSMakeRect, NSMenu, NSMenuItem,
    NSOpenPanel, NSOutlineView, NSPopUpButton, NSProgressIndicator, NSScrollView,
    NSSplitView, NSStackView, NSTableCellView, NSTableColumn, NSTableView,
    NSTabView, NSTabViewItem, NSTextField, NSView, NSWindow, NSWorkspace,
    NSEventModifierFlagCommand, NSPanel, NSPasteboard, NSPasteboardTypeString,
)
from Foundation import (NSBundle, NSIndexSet, NSMutableIndexSet, NSObject, NSSortDescriptor, NSURL, NSURLRequest,
                        NSUserDefaults)
from PyObjCTools import AppHelper

try:
    from Quartz import QLPreviewPanel
except ImportError:  # older PyObjC layouts
    from QuickLookUI import QLPreviewPanel  # type: ignore

from .backend import (Cancelled, MergedEntry, Node, Restic, ResticError, disk_status, fmt_counts,
                      fmt_delta,
                      hostnames_of, hosts_of, list_local, local_node, machine_of,
                      RestoreJournal, existing_dir, free_space, local_size, normalize_local_path,
                      restore_conflicts, reveal_chain,
                      same_machine, snapshots_covering, snapshots_for_folder, plan_of)
from . import backrest as br
from .config import AppConfig, config_path
from .uikit import (ColumnChooser, ProgressSheet, add_open_with_submenu, apps_for_extension, kind_of,
                    open_with, placeholder_page, preview_item, sort_nodes)

# AppKit constants (numeric to stay independent of PyObjC constant naming across versions)
STYLE_MASK = 1 | 2 | 4 | 8          # titled | closable | miniaturizable | resizable
WIDTH_HEIGHT = 2 | 16               # NSViewWidthSizable | NSViewHeightSizable
KEY_DOWN = 10                        # NSEventTypeKeyDown
SPINNING = 1                         # NSProgressIndicatorStyleSpinning
LOCAL_FILES = "This Mac (local files)"
POLL_SECONDS = 60                    # how often to look for new backups
DISK_CHECK_SECONDS = 3               # how often to check the open folders on the left
ALL_HOSTS = "All hosts"

_executor = ThreadPoolExecutor(max_workers=4)


def run_async(fn, on_done, on_error=None):
    """Run *fn* on a worker thread, deliver the result on the main thread."""
    def work():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, Cancelled):     # superseded requests are normal, not errors
                traceback.print_exc()
            if on_error:
                AppHelper.callAfter(on_error, e)
            return
        AppHelper.callAfter(on_done, result)
    _executor.submit(work)


def dir_mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def has_full_disk_access():
    """True/False, or None if it can't be told (e.g. not macOS)."""
    probes = ["/Library/Application Support/com.apple.TCC/TCC.db",
              os.path.expanduser("~/Library/Safari"),
              os.path.expanduser("~/Library/Mail")]
    for p in probes:
        if not os.path.exists(p):
            continue
        try:
            if os.path.isdir(p):
                os.listdir(p)
            else:
                with open(p, "rb"):
                    pass
            return True
        except PermissionError:
            return False
        except OSError:
            continue
    return None


def restore_start_dir(paths):
    """Folder the "Restore to…" panel opens in: the items' (common) parent folder,
    or its nearest ancestor that still exists on this Mac."""
    parents = [posixpath.dirname(p.rstrip("/")) or "/" for p in paths]
    start = posixpath.commonpath(parents) if parents else os.path.expanduser("~")
    while start not in ("", "/") and not os.path.isdir(start):
        start = posixpath.dirname(start)
    return start or "/"


def _volume_value(path, key):
    url = NSURL.fileURLWithPath_(existing_dir(path))
    vals, _err = url.resourceValuesForKeys_error_([key], None)
    return vals.get(key) if vals else None


def volume_free(path):
    """Free space as Finder shows it (includes space macOS can purge on demand)."""
    try:
        v = _volume_value(path, "NSURLVolumeAvailableCapacityForImportantUsageKey")
        if v is not None and int(v) > 0:
            return int(v)
    except Exception:  # noqa: BLE001
        pass
    return free_space(path)


def volume_name(path):
    try:
        return str(_volume_value(path, "NSURLVolumeLocalizedNameKey") or existing_dir(path))
    except Exception:  # noqa: BLE001
        return existing_dir(path)


def move_to_trash(path):
    """Put *path* in the Trash (recoverable with Put Back), raising on failure."""
    from AppKit import NSFileManager
    ok, _url, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(path), None, None)
    if not ok:
        raise OSError(f"Could not move {path} to the Trash: {err.localizedDescription() if err else ''}")


def fmt_duration(seconds):
    s = int(seconds or 0)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def fmt_size(n):
    if n is None:
        return ""
    return NSByteCountFormatter.stringFromByteCount_countStyle_(n, 0)


def fmt_time(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else ""


_icon_cache: dict = {}


def icon_for(node):
    if node is None:
        return None
    key = "dir" if node.is_dir else (os.path.splitext(node.name)[1].lstrip(".").lower() or "?")
    img = _icon_cache.get(key)
    if img is None:
        if node.is_dir:
            img = NSImage.imageNamed_("NSFolder").copy()
        else:
            img = NSWorkspace.sharedWorkspace().iconForFileType_(key if key != "?" else "public.data").copy()
        img.setSize_((16, 16))
        _icon_cache[key] = img
    return img


def alert(title, text=""):
    a = NSAlert.alloc().init()
    a.setMessageText_(title)
    a.setInformativeText_(text)
    a.runModal()


def label(text="", bold=False):
    tf = NSTextField.labelWithString_(text)
    if bold:
        tf.setFont_(NSFont.boldSystemFontOfSize_(NSFont.systemFontSize()))
    tf.setLineBreakMode_(NSLineBreakByTruncatingMiddle)
    return tf


def pin(view, parent, top=0, bottom=0, left=0, right=0):
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    parent.addSubview_(view)
    NSLayoutConstraint.activateConstraints_([
        view.topAnchor().constraintEqualToAnchor_constant_(parent.topAnchor(), top),
        view.bottomAnchor().constraintEqualToAnchor_constant_(parent.bottomAnchor(), -bottom),
        view.leadingAnchor().constraintEqualToAnchor_constant_(parent.leadingAnchor(), left),
        view.trailingAnchor().constraintEqualToAnchor_constant_(parent.trailingAnchor(), -right),
    ])


def scrolled(view):
    sv = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 100))
    sv.setDocumentView_(view)
    sv.setHasVerticalScroller_(True)
    sv.setHasHorizontalScroller_(True)
    sv.setAutohidesScrollers_(True)
    return sv


# ============================================================================
# Model objects for the outline view (must be ObjC objects with stable identity)
# ============================================================================

class TreeItem(NSObject):
    @objc.python_method
    def setup(self, path, name, entry=None, placeholder=False):
        self.path = path
        self.name = name
        self.entry = entry            # MergedEntry (merged mode) or None (root)
        self.node = entry.node if entry else None
        self.is_dir = True if entry is None else entry.node.is_dir
        self.children = None          # None = not loaded yet (visible children)
        self.allEntries = None        # every entry incl. hidden ones
        self.itemCache = {}           # name -> TreeItem, so identity survives re-filtering
        self.loading = False
        self.placeholder = placeholder
        return self


def make_item(path, name, entry=None, placeholder=False):
    return TreeItem.alloc().init().setup(path, name, entry, placeholder)


# ============================================================================
# Table view with Quick Look support
# ============================================================================

class QLVersionsView(NSOutlineView):
    """Right pane (a tree: folder versions open to show what changed).
    Space toggles Quick Look; acts as the QLPreviewPanel controller."""

    def keyDown_(self, event):
        if event.charactersIgnoringModifiers() == " ":
            self.qlController.toggleQuickLook_(self)
        elif event.charactersIgnoringModifiers() in ("\r", "\x03"):
            self.qlController.rowDoubleClicked_(self)
        elif event.keyCode() == 51 and (event.modifierFlags() & NSEventModifierFlagCommand):
            self.qlController.goBack_(self)  # ⌘⌫ = back
        else:
            objc.super(QLVersionsView, self).keyDown_(event)     # ← → open/close natively

    @objc.typedSelector(b"Z@:@")
    def acceptsPreviewPanelControl_(self, panel):
        return True

    @objc.typedSelector(b"v@:@")
    def beginPreviewPanelControl_(self, panel):
        panel.setDataSource_(self.qlController)
        panel.setDelegate_(self.qlController)
        self.qlController.previewPanelActive = True
        self.qlController.refreshPreview()

    @objc.typedSelector(b"v@:@")
    def endPreviewPanelControl_(self, panel):
        self.qlController.previewPanelActive = False


class QLOutlineView(NSOutlineView):
    """Left tree: Space toggles Quick Look of the local files."""

    def keyDown_(self, event):
        if event.charactersIgnoringModifiers() == " ":
            self.qlController.toggleQuickLook_(self)
        else:
            objc.super(QLOutlineView, self).keyDown_(event)

    @objc.typedSelector(b"Z@:@")
    def acceptsPreviewPanelControl_(self, panel):
        return True

    @objc.typedSelector(b"v@:@")
    def beginPreviewPanelControl_(self, panel):
        panel.setDataSource_(self.qlController)
        panel.setDelegate_(self.qlController)
        self.qlController.previewPanelActive = True
        self.qlController.refreshPreview()

    @objc.typedSelector(b"v@:@")
    def endPreviewPanelControl_(self, panel):
        self.qlController.previewPanelActive = False


# ============================================================================
# Outline data source (left pane)
# ============================================================================

class OutlineSource(NSObject):
    @objc.python_method
    def setup(self, main):
        self.main = main
        self.root = make_item("/", "/")
        self.restoringSelection = False
        return self

    @objc.python_method
    def reset(self):
        self.root = make_item("/", "/")
        self.main.outline.reloadData()
        self.load(self.root)

    @objc.python_method
    def _item(self, item):
        return self.root if item is None else item

    # -- data source ----------------------------------------------------------
    def outlineView_numberOfChildrenOfItem_(self, ov, item):
        it = self._item(item)
        if it.placeholder or not it.is_dir:
            return 0
        if it.children is None:
            self.load(it)
            return 1                      # "Loading…" placeholder
        return len(it.children)

    def outlineView_child_ofItem_(self, ov, index, item):
        it = self._item(item)
        if it.children is None:
            if not hasattr(it, "_ph"):
                it._ph = make_item(it.path, "Loading…", placeholder=True)
            return it._ph
        return it.children[index]

    def outlineView_isItemExpandable_(self, ov, item):
        it = self._item(item)
        return bool(it.is_dir and not it.placeholder)

    def outlineView_viewForTableColumn_item_(self, ov, col, item):
        it = self._item(item)
        if col.identifier() != "name":
            cell = ov.makeViewWithIdentifier_owner_("TextCell", self)
            if cell is None:
                cell = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 20))
                cell.setIdentifier_("TextCell")
                tf = label()
                tf.setTextColor_(NSColor.secondaryLabelColor())
                cell.setTextField_(tf)
                pin(tf, cell, 2, 2, 2, 4)
            n, c = it.node, col.identifier()
            text = ""
            if n is not None and not it.placeholder:
                if c == "mtime":
                    text = fmt_time(n.mtime)
                elif c == "size":
                    if n.is_dir:
                        ls = getattr(it, "localSize", None)
                        text = "—" if ls is None else fmt_size(ls)
                    else:
                        text = fmt_size(n.size)
                elif c == "kind":
                    text = kind_of(n)
            cell.textField().setStringValue_(text)
            cell.textField().setAlignment_(2 if c == "size" else 0)     # right-align sizes
            return cell
        cell = ov.makeViewWithIdentifier_owner_("NameCell", self)
        if cell is None:
            cell = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 20))
            cell.setIdentifier_("NameCell")
            iv = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
            tf = label()
            for v in (iv, tf):
                v.setTranslatesAutoresizingMaskIntoConstraints_(False)
                cell.addSubview_(v)
            cell.setImageView_(iv)
            cell.setTextField_(tf)
            NSLayoutConstraint.activateConstraints_([
                iv.leadingAnchor().constraintEqualToAnchor_constant_(cell.leadingAnchor(), 2),
                iv.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
                iv.widthAnchor().constraintEqualToConstant_(16),
                iv.heightAnchor().constraintEqualToConstant_(16),
                tf.leadingAnchor().constraintEqualToAnchor_constant_(iv.trailingAnchor(), 5),
                tf.trailingAnchor().constraintEqualToAnchor_constant_(cell.trailingAnchor(), -2),
                tf.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
            ])
        cell.textField().setStringValue_(it.name)
        cell.imageView().setImage_(None if it.placeholder else icon_for(it.node))
        deleted = it.entry is not None and not it.entry.in_latest
        cell.textField().setTextColor_(
            NSColor.secondaryLabelColor() if (deleted or it.placeholder) else NSColor.labelColor())
        font = NSFont.systemFontOfSize_(NSFont.systemFontSize())
        if deleted:
            font = NSFont.fontWithName_size_("Helvetica-Oblique", NSFont.systemFontSize()) or font
        cell.textField().setFont_(font)
        cell.setToolTip_("Not in the newest snapshot (deleted)" if deleted else it.path)
        return cell

    def outlineViewSelectionDidChange_(self, note):
        ov = self.main.outline
        row = ov.selectedRow()
        item = ov.itemAtRow_(row) if row >= 0 else None
        if self.restoringSelection:
            return
        if item is not None and not item.placeholder and item is not self.main.current_item:
            self.main.showHistory(item)
        if self.main.previewPanelActive and self.main.previewFromOutline:
            self.main.refreshPreview()

    def outlineView_sortDescriptorsDidChange_(self, ov, old):
        descs = ov.sortDescriptors()
        if descs:
            self.main.leftSort = (descs[0].key(), bool(descs[0].ascending()))
        self.resortAll()

    @objc.python_method
    def sortItems(self, items):
        key, asc = self.main.leftSort
        return sort_nodes(items, key, asc, self.main.foldersFirst, get=lambda c: c.node)

    @objc.python_method
    def visible(self, entries):
        if self.main.showHidden:
            return list(entries)
        return [e for e in entries if not e.node.is_hidden]

    @objc.python_method
    def resortAll(self):
        """Re-sort / re-filter every loaded folder; keep the selection and keep it in view."""
        def walk(it):
            if it.children is None:
                return
            if it is not self.root and it.allEntries is not None:
                it.children = self.sortItems(self.itemsFor(it, self.visible(it.allEntries)))
            for c in it.children:
                walk(c)
        ov = self.main.outline
        selected = self.main.selectedOutlineItems()
        walk(self.root)
        self.restoringSelection = True
        try:
            ov.reloadItem_reloadChildren_(None, True)
            rows = NSMutableIndexSet.indexSet()
            for it in selected:
                r = ov.rowForItem_(it)
                if r >= 0:
                    rows.addIndex_(r)
            ov.selectRowIndexes_byExtendingSelection_(rows, False)
        finally:
            self.restoringSelection = False
        if rows.count():
            ov.scrollRowToVisible_(rows.firstIndex())
        elif selected:                       # the selection was hidden away
            self.main.clearRight()

    # -- loading --------------------------------------------------------------
    @objc.python_method
    def load(self, it):
        """Local mode: list the Mac's own disk (instant). Snapshot mode: one `restic ls`."""
        main = self.main
        if it.loading or main.restic is None:
            return
        restic, single = main.restic, main.singleSnapshot()
        generation = main.treeGeneration
        is_root = it is self.root
        it.loading = True

        if single is None:
            roots = main.backupRoots() if is_root else None

            def work():
                if is_root:
                    nodes = [local_node(p, name=p) for p in roots]
                    return [n for n in nodes if n is not None]
                it.dirMtime = dir_mtime(it.path)          # before listing: never miss a change
                return list_local(it.path)
        else:
            def work():
                if is_root:
                    return restic.ls(single, "/")
                return restic.ls(single, it.path)

        def done(nodes):
            if generation != main.treeGeneration:
                return
            it.loading = False
            main.jobFinished()
            self.setChildren(it, [MergedEntry(node=n, newest=single) for n in nodes])
            if main.pendingReveal:
                AppHelper.callAfter(main.continueReveal)
            elif is_root and len(it.children) == 1:
                main.outline.expandItem_(it.children[0])

        def fail(e):
            if generation != main.treeGeneration:
                return
            it.loading = False
            if it.children is None:
                it.children = []
            main.jobFinished("" if isinstance(e, Cancelled) else str(e))
            self.reloadChildren(it)

        main.jobStarted(f"Listing {it.path} …")
        run_async(work, done, fail)

    @objc.python_method
    def setChildren(self, it, entries):
        """Replace children but keep existing TreeItems (preserves expansion/selection)."""
        if it is self.root:
            it.children = self.itemsFor(it, entries)        # backup roots: never hidden
        else:
            it.allEntries = list(entries)
            it.children = self.sortItems(self.itemsFor(it, self.visible(entries)))
        self.reloadChildren(it)

    @objc.python_method
    def itemsFor(self, it, entries):
        """TreeItems for *entries*, reusing earlier ones by name (stable identity)."""
        cache = it.itemCache
        out = []
        for e in entries:
            c = cache.get(e.node.name)
            if c is None:
                c = cache[e.node.name] = make_item(e.node.path, e.node.name, e)
            else:
                c.entry, c.node, c.is_dir = e, e.node, e.node.is_dir
            out.append(c)
        return out

    @objc.python_method
    def reloadChildren(self, it):
        self.main.outline.reloadItem_reloadChildren_(None if it is self.root else it, True)

    # -- keeping open folders current ------------------------------------------
    @objc.python_method
    def openFolders(self):
        """Loaded folders whose contents are on screen: expanded ones (local mode)."""
        ov, out = self.main.outline, []

        def walk(it):
            for c in it.children or []:
                if c.is_dir and c.allEntries is not None and ov.isItemExpanded_(c):
                    out.append(c)
                    walk(c)
        walk(self.root)
        return out

    @objc.python_method
    def refreshOpen(self, force=False):
        """Re-list the open folders that changed on disk (or all open ones if *force*,
        e.g. after a restore) and update the tree in place: a new file appears, a
        deleted one goes, everything else (open folders, selection, scroll) stays.
        A folder's modification time changes whenever an item is added, removed or
        renamed in it, so the periodic check is one stat() per open folder."""
        main = self.main
        if main.singleSnapshot() is not None or getattr(self, "refreshing", False):
            return
        folders = self.openFolders()
        if not folders:
            return
        generation = main.treeGeneration
        self.refreshing = True

        def work():
            out = []
            for it in folders:
                m = dir_mtime(it.path)
                if force or m != getattr(it, "dirMtime", None):
                    out.append((it, m, list_local(it.path)))
            return out

        def done(results):
            self.refreshing = False
            if generation != main.treeGeneration or not results:
                return
            changed = []
            for it, m, nodes in results:
                it.dirMtime = m
                old = [(e.node.name, e.node.type, e.node.size, e.node.mtime)
                       for e in it.allEntries or []]
                new = [(n.name, n.type, n.size, n.mtime) for n in nodes]
                if old != new:
                    it.allEntries = [MergedEntry(node=n, newest=None) for n in nodes]
                    it.children = self.sortItems(self.itemsFor(it, self.visible(it.allEntries)))
                    changed.append(it)
            if not changed:
                return
            ov = main.outline
            selected = main.selectedOutlineItems()
            self.restoringSelection = True
            try:
                for it in changed:
                    ov.reloadItem_reloadChildren_(it, True)
                rows = NSMutableIndexSet.indexSet()
                for it in selected:
                    r = ov.rowForItem_(it)
                    if r >= 0:
                        rows.addIndex_(r)
                ov.selectRowIndexes_byExtendingSelection_(rows, False)
            finally:
                self.restoringSelection = False

        def fail(e):
            self.refreshing = False
        run_async(work, done, fail)


# ============================================================================
# Right pane data source: versions of one path, or contents of a directory
# ============================================================================

def change_detail(entry, totals):
    """Second half of the Changes text of a tree row: recursive counts for folders
    (while the background `restic diff` runs they fill in), size change for files."""
    if entry.is_dir:
        if entry.status == "meta":
            return ""
        return fmt_counts(totals.of(entry.path)) if totals is not None else ""
    if entry.status == "changed" and entry.old is not None and entry.new is not None \
            and entry.old.size is not None and entry.new.size is not None:
        return (f"{fmt_size(entry.old.size)} → {fmt_size(entry.new.size)} "
                f"({fmt_delta(entry.new.size, entry.old.size)})")
    return ""


class RowItem(NSObject):
    """Outline-view item for row *i* of VersionsSource (stable identity)."""

    @objc.python_method
    def setup(self, i):
        self.i = i
        return self


def make_name_cell(ov, owner):
    cell = ov.makeViewWithIdentifier_owner_("NameCell", owner)
    if cell is None:
        cell = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 20))
        cell.setIdentifier_("NameCell")
        iv = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
        tf = label()
        for v in (iv, tf):
            v.setTranslatesAutoresizingMaskIntoConstraints_(False)
            cell.addSubview_(v)
        cell.setImageView_(iv)
        cell.setTextField_(tf)
        NSLayoutConstraint.activateConstraints_([
            iv.leadingAnchor().constraintEqualToAnchor_constant_(cell.leadingAnchor(), 2),
            iv.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
            iv.widthAnchor().constraintEqualToConstant_(16),
            iv.heightAnchor().constraintEqualToConstant_(16),
            tf.leadingAnchor().constraintEqualToAnchor_constant_(iv.trailingAnchor(), 5),
            tf.trailingAnchor().constraintEqualToAnchor_constant_(cell.trailingAnchor(), -2),
            tf.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
        ])
    return cell


def make_text_cell(ov, owner):
    cell = ov.makeViewWithIdentifier_owner_("TextCell", owner)
    if cell is None:
        cell = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 20))
        cell.setIdentifier_("TextCell")
        tf = label()
        cell.setTextField_(tf)
        pin(tf, cell, 2, 2, 2, 4)
    return cell


class VersionsSource(NSObject):
    """Data source of the right pane, a (view-based) outline view.

    rows / extra are parallel lists; a row's index there ("canonical index") never
    changes.  Top-level rows are versions (or folder entries when browsing).  Opening
    a folder version appends the rows of what changed inside it, with
    extra["parent"] pointing back, so the change tree hangs below its version."""

    COLUMNS = [("name", "Name", 230), ("snapshot", "Backup", 125),
               ("first", "Since", 125), ("mtime", "Modified", 125), ("size", "Size", 75),
               ("count", "Snapshots", 70), ("plan", "Plan", 110), ("change", "Changes", 300),
               ("disk", "On disk", 70),
               ("host", "Host", 100), ("id", "ID", 80)]
    DISK_LABELS = {"same": "✓ same", "changed": "changed", "missing": "missing", "": ""}
    STATUS_LABELS = {"changed": "changed", "added": "added", "removed": "removed",
                     "meta": "dates/permissions only", "": ""}

    @objc.python_method
    def setup(self, main):
        self.main = main
        self.rows = []          # list of (snapshot, node, count)
        self.extra = []         # parallel list of dicts: disk, change, first, status, prev, …
        self.items = []         # parallel list of RowItem (what the outline view holds)
        self.top = []           # canonical indexes of the top-level rows, in display order
        self.kidOrder = {}      # canonical index -> its children in display order
        self.sortKey = None     # (column id, ascending) or None = natural order
        self.dimUnchanged = False
        return self

    @objc.python_method
    def setData(self, rows, extra):
        self.rows, self.extra = rows, extra
        self.items = [RowItem.alloc().init().setup(i) for i in range(len(rows))]
        self.applySort()

    @objc.python_method
    def add(self, row, ex):
        """Append a (change tree) row; returns its canonical index."""
        self.rows.append(row)
        self.extra.append(ex)
        self.items.append(RowItem.alloc().init().setup(len(self.rows) - 1))
        self.kidOrder.pop(ex.get("parent"), None)
        return len(self.rows) - 1

    @objc.python_method
    def applySort(self):
        ex = self.extra
        top = [i for i in range(len(self.rows)) if i >= len(ex) or ex[i].get("parent") is None]
        if self.sortKey:
            key, asc = self.sortKey
            top.sort(key=self.sortValue(key), reverse=not asc)
        self.top = top
        self.kidOrder = {}

    @objc.python_method
    def childrenOf(self, i):
        kids = self.kidOrder.get(i)
        if kids is None:
            kids = self.kidOrder[i] = self.sortChildren(self.extra[i].get("children") or [])
        return kids

    @objc.python_method
    def sortChildren(self, ids):
        """Siblings in a change tree: folders first; real changes before date-only ones
        unless a column is sorted."""
        rows, ex = self.rows, self.extra

        def natural(i):
            return (not rows[i][1].is_dir, ex[i].get("status") == "meta", rows[i][1].name.casefold())
        ids = sorted(ids, key=natural)
        if self.sortKey and self.sortKey[0] in ("name", "mtime", "size", "change", "disk"):
            key, asc = self.sortKey
            value = self.sortValue(key)
            dirs = sorted([i for i in ids if rows[i][1].is_dir], key=value, reverse=not asc)
            files = sorted([i for i in ids if not rows[i][1].is_dir], key=value, reverse=not asc)
            ids = dirs + files
        return ids

    @objc.python_method
    def sortValue(self, key):
        epoch = datetime.fromtimestamp(0, timezone.utc)

        def value(i):
            snap, node, count = self.rows[i]
            ex = self.extra[i] if i < len(self.extra) else {}
            if key == "name":
                return node.name.casefold()
            if key == "snapshot":
                return snap.time
            if key == "first":
                return ex.get("first") or epoch
            if key == "mtime":
                return node.mtime or epoch
            if key == "size":
                v = ex.get("dirsize") if node.is_dir else node.size
                return -1 if v is None else v
            if key == "count":
                return count
            if key == "plan":
                return ex["plans"] if "plans" in ex else plan_of(snap)
            if key == "host":
                return snap.hostname
            if key == "id":
                return snap.short_id
            if key == "change" and ex.get("kind") == "change":
                return ex.get("status", "")
            return str(ex.get(key, ""))           # change, disk
        return value

    # -- outline view data source ------------------------------------------------
    def outlineView_numberOfChildrenOfItem_(self, ov, item):
        if item is None:
            return len(self.top)
        return len(self.childrenOf(item.i)) if item.i < len(self.extra) else 0

    def outlineView_child_ofItem_(self, ov, index, item):
        ids = self.top if item is None else self.childrenOf(item.i)
        return self.items[ids[index]]

    def outlineView_isItemExpandable_(self, ov, item):
        return item is not None and self.main.canExpand(item.i)

    def outlineViewItemWillExpand_(self, note):
        item = note.userInfo()["NSObject"]
        self.main.rowWillExpand(item.i)

    def outlineViewItemDidCollapse_(self, note):
        item = note.userInfo()["NSObject"]
        if item.i < len(self.extra):
            self.extra[item.i]["expanded"] = False

    def outlineView_sortDescriptorsDidChange_(self, ov, old):
        descs = ov.sortDescriptors()
        self.sortKey = (descs[0].key(), bool(descs[0].ascending())) if descs else None
        sel = self.main.selectedIndexes()
        self.applySort()
        ov.reloadData()
        self.main.selectIndexes(sel)

    def outlineView_viewForTableColumn_item_(self, ov, col, item):
        i, c = item.i, col.identifier()
        if c == "name":
            cell = make_name_cell(ov, self)
            cell.imageView().setImage_(icon_for(self.rows[i][1]))
        else:
            cell = make_text_cell(ov, self)
        tf = cell.textField()
        tf.setStringValue_(self.text(i, c))
        tf.setTextColor_(self.color(i, c))
        tf.setAlignment_(2 if c in ("size", "count") else 0)
        return cell

    def outlineViewSelectionDidChange_(self, note):
        if not getattr(self.main, "programmatic", 0):
            self.main.pendingState = None     # the user chose something: stop restoring
        self.main.updateButtons()
        if self.main.previewPanelActive:
            self.main.refreshPreview()

    # -- cell contents -------------------------------------------------------------
    @objc.python_method
    def text(self, i, c):
        if i >= len(self.rows):
            return ""
        snap, node, count = self.rows[i]
        ex = self.extra[i] if i < len(self.extra) else {}
        if c == "name":
            return node.name + ("/" if node.is_dir else "")
        if ex.get("kind") == "change":
            return self.changeText(c, node, ex)
        if c == "snapshot":
            return fmt_time(snap.time)
        if c == "first":
            return fmt_time(ex.get("first"))
        if c == "mtime":
            return fmt_time(node.mtime) if node.mtime else "—"
        if c == "size":
            if node.is_dir:
                return fmt_size(ex["dirsize"]) if ex.get("dirsize") is not None else "—"
            return fmt_size(node.size)
        if c == "count":
            return str(count) if count > 1 else ("1" if ex.get("first") else "")
        if c == "change":
            return ex.get("change", "")
        if c == "disk":
            return self.DISK_LABELS.get(ex.get("disk", ""), "")
        if c == "plan":
            return ex["plans"] if "plans" in ex else plan_of(snap)
        if c == "host":
            return snap.hostname
        if c == "id":
            return snap.short_id
        return ""

    @objc.python_method
    def changeText(self, c, node, ex):
        """Columns of a row in a change tree (an item that differs from the older version)."""
        if c == "mtime":
            return fmt_time(node.mtime) if node.mtime else ""
        if c == "size":
            if node.is_dir:
                return fmt_size(ex["dirsize"]) if ex.get("dirsize") is not None else ""
            return fmt_size(node.size)
        if c == "change":
            status = ex.get("status", "")
            text = self.STATUS_LABELS.get(status, status)
            root = self.extra[ex["root"]] if ex["root"] < len(self.extra) else {}
            detail = change_detail(ex["entry"], root.get("totals"))
            return f"{text}  ·  {detail}" if detail else text
        if c == "disk":
            return self.DISK_LABELS.get(ex.get("disk", ""), "")
        return ""

    @objc.python_method
    def color(self, i, c):
        ex = self.extra[i] if i < len(self.extra) else {}
        status = ex.get("status", "")
        if status == "pending":
            return NSColor.tertiaryLabelColor()
        if c == "change" and status:
            return {"added": NSColor.systemGreenColor(), "removed": NSColor.systemRedColor(),
                    "changed": NSColor.systemOrangeColor()}.get(status, NSColor.secondaryLabelColor())
        if self.dimUnchanged and status in ("", "meta"):
            return NSColor.secondaryLabelColor()
        if ex.get("kind") == "change" and status in ("meta", "removed"):
            return NSColor.secondaryLabelColor()
        if c != "name":
            return NSColor.labelColor()
        return NSColor.labelColor()


def _register_webkit_blocks():
    """Tell PyObjC the shape of WebKit's decision handlers before WebPolicy is defined:
    a delegate method's signature is fixed when its class is created, and a block
    without one can't be called ("cannot call block without a signature").  Importing
    WebKit loads PyObjC's own metadata; registering it here as well makes this
    independent of import order and PyObjC versions."""
    try:
        import WebKit  # noqa: F401
    except ImportError:
        pass
    register = getattr(objc, "registerMetaDataForSelector", None)
    if register is None:
        return
    handler = {"callable": {"retval": {"type": b"v"},
                            "arguments": {0: {"type": b"^v"}, 1: {"type": b"q"}}}}
    for sel in (b"webView:decidePolicyForNavigationAction:decisionHandler:",
                b"webView:decidePolicyForNavigationResponse:decisionHandler:"):
        try:
            # argument 4 = the block (0 self, 1 _cmd, 2 web view, 3 action/response)
            register(b"NSObject", sel, {"arguments": {4: handler}})
        except Exception:  # noqa: BLE001
            traceback.print_exc()


_register_webkit_blocks()


def decide(handler, policy):
    """Answer a WebKit decision handler (1 = allow, 0 = cancel)."""
    try:
        handler(policy)
    except TypeError:
        # still no signature (unexpected PyObjC version): give the block one, try again
        traceback.print_exc()
        try:
            handler.__block_signature__ = objc.splitSignature(b"v^vq")
            handler(policy)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


class WebPolicy(NSObject):
    """Navigation / UI delegate of a web view: Backrest's own pages stay in the Backrest
    tab, "new tab" links and other sites go to the links tab, other URL schemes go to
    macOS, and downloads go to the default browser (which saves and shows them)."""

    @objc.python_method
    def setup(self, main, links_tab):
        self.main, self.links_tab = main, links_tab
        return self

    @objc.python_method
    def route(self, url, new_window):
        """Where *url* (an NSURL) should open; returns True if this view should load it."""
        text = url.absoluteString() if url is not None else ""
        where = br.link_target(text, getattr(self.main, "backrestBase", ""), new_window,
                               self.links_tab)
        if where == "system":
            NSWorkspace.sharedWorkspace().openURL_(url)
            return False
        if where == "links":
            AppHelper.callAfter(self.main.openInLinksTab, url)
            return False
        return True

    def webView_decidePolicyForNavigationAction_decisionHandler_(self, view, action, handler):
        frame = action.targetFrame()
        main_frame = frame is not None and frame.isMainFrame()
        if not main_frame and frame is not None:
            decide(handler, 1)                    # subframes (iframes) load where they are
            return
        try:
            ok = self.route(action.request().URL(), frame is None)
        except Exception:  # noqa: BLE001 — never leave WebKit waiting for an answer
            traceback.print_exc()
            ok = True
        decide(handler, 1 if ok else 0)

    def webView_decidePolicyForNavigationResponse_decisionHandler_(self, view, response, handler):
        if response.canShowMIMEType():
            decide(handler, 1)
            return
        url = response.response().URL()           # a download: let the browser save it
        if url is not None:
            NSWorkspace.sharedWorkspace().openURL_(url)
        decide(handler, 0)

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, view, config, action, features):
        url = action.request().URL()             # target=_blank / window.open
        if self.route(url, True):
            view.loadRequest_(action.request())   # (links tab: open in the same tab)
        return None

    def webView_didFinishNavigation_(self, view, navigation):
        if self.links_tab:
            self.main.linksPageChanged(view)


# ============================================================================
# Main window controller
# ============================================================================

class MainController(NSObject):

    def init(self):
        self = objc.super(MainController, self).init()
        if self is None:
            return None
        self.config = AppConfig.load()
        self.restic = None
        self.snapshots = []
        self.treeGeneration = 0
        self.historyGeneration = 0
        self.jobs = 0
        self.stack = []                # browse stack: list of (snapshot, dir_node, compare_snapshot)
        self.folderVersions = []       # FolderVersion list when a folder is selected
        self.hideUnchanged = True      # file versions: fold identical copies
        self.onlyChanges = False       # folder browsing: show only changed entries
        self.previewFromOutline = False  # Quick Look source: left tree (local) or right table
        self.previewItems = []
        defaults = NSUserDefaults.standardUserDefaults()
        self.foldersFirst = defaults.objectForKey_("FoldersOnTop") is None or \
            bool(defaults.boolForKey_("FoldersOnTop"))
        self.leftSort = ("name", True)
        self.journal = RestoreJournal(os.path.join(os.path.dirname(config_path()),
                                                   "restores-in-progress.json"))
        self.restoresRunning = 0
        self.measureCancelled = False
        self.pendingReveal = None          # path to show, from Finder / the Dock
        self._sleepActivity = None
        self.showHidden = bool(defaults.boolForKey_("ShowHiddenFiles"))
        self.current_item = None       # left selection
        self.previewPanelActive = False
        self.previewPrevious = False       # Quick Look the older side of a change row
        self.pendingState = None           # view to restore after a quiet refresh
        self.polling = False               # a check for new backups is running
        self.pollTimer = None
        self.webLoaded = False
        self.settings = None
        self.buildWindow()
        return self

    # ------------------------------------------------------------------ UI --
    @objc.python_method
    def buildWindow(self):
        w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(200, 200, 1150, 700), STYLE_MASK, NSBackingStoreBuffered, False)
        w.setTitle_("Restic Control")
        w.setFrameAutosaveName_("ResticControlMain")
        w.setMinSize_((800, 450))
        self.window = w
        content = w.contentView()

        # --- top bar
        self.repoPopup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 200, 26), False)
        self.repoPopup.setTarget_(self)
        self.repoPopup.setAction_("repoChanged:")
        settingsBtn = NSButton.buttonWithTitle_target_action_("Settings…", self, "openSettings:")
        self.hostPopup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 140, 26), False)
        self.hostPopup.setTarget_(self)
        self.hostPopup.setAction_("hostChanged:")
        self.snapPopup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 320, 26), False)
        self.snapPopup.setTarget_(self)
        self.snapPopup.setAction_("snapshotChanged:")
        refreshBtn = NSButton.buttonWithTitle_target_action_("Refresh", self, "refresh:")
        self.spinner = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, 0, 16, 16))
        self.spinner.setStyle_(SPINNING)
        self.spinner.setControlSize_(1)
        self.spinner.setDisplayedWhenStopped_(False)
        self.status = label("")
        self.status.setTextColor_(NSColor.secondaryLabelColor())
        self.status.setContentCompressionResistancePriority_forOrientation_(1, 0)
        top = NSStackView.stackViewWithViews_([
            label("Repository:"), self.repoPopup, settingsBtn, label("  Host:"), self.hostPopup,
            label("  Browse:"), self.snapPopup, refreshBtn, self.spinner, self.status])
        top.setSpacing_(6)

        # --- tabs
        self.tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, 1000, 600))
        self.tabs.setDelegate_(self)
        restoreTab = NSTabViewItem.alloc().initWithIdentifier_("restore")
        restoreTab.setLabel_("Restore")
        restoreTab.setView_(self.buildRestoreView())
        manageTab = NSTabViewItem.alloc().initWithIdentifier_("manage")
        manageTab.setLabel_("Manage (Backrest)")
        manageTab.setView_(self.buildManageView())
        self.tabs.addTabViewItem_(restoreTab)
        self.tabs.addTabViewItem_(manageTab)

        for v in (top, self.tabs):
            v.setTranslatesAutoresizingMaskIntoConstraints_(False)
            content.addSubview_(v)
        NSLayoutConstraint.activateConstraints_([
            top.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 10),
            top.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 12),
            top.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(content.trailingAnchor(), -12),
            self.tabs.topAnchor().constraintEqualToAnchor_constant_(top.bottomAnchor(), 6),
            self.tabs.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 8),
            self.tabs.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -8),
            self.tabs.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -8),
        ])

    @objc.python_method
    def buildRestoreView(self):
        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1000, 600))
        split = NSSplitView.alloc().initWithFrame_(container.bounds())
        split.setVertical_(True)
        split.setDividerStyle_(2)  # thin
        split.setAutoresizingMask_(WIDTH_HEIGHT)
        container.addSubview_(split)
        self.split = split

        # --- left: outline
        ov = QLOutlineView.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 500))
        ov.qlController = self
        left_cols = [("name", "Name", 230), ("mtime", "Date Modified", 125),
                     ("size", "Size", 70), ("kind", "Kind", 90)]
        for ident, title, width in left_cols:
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.headerCell().setStringValue_(title)
            col.setWidth_(width)
            col.setSortDescriptorPrototype_(NSSortDescriptor.sortDescriptorWithKey_ascending_(
                ident, ident in ("name", "kind")))
            ov.addTableColumn_(col)
            if ident == "name":
                ov.setOutlineTableColumn_(col)
        ov.setSortDescriptors_([NSSortDescriptor.sortDescriptorWithKey_ascending_("name", True)])
        ov.setRowHeight_(20)
        ov.setAutoresizesOutlineColumn_(False)
        ov.setColumnAutoresizingStyle_(5)  # first column takes the slack
        ov.setAllowsMultipleSelection_(True)
        self.outline = ov
        self.outlineSource = OutlineSource.alloc().init().setup(self)
        ov.setDataSource_(self.outlineSource)
        ov.setDelegate_(self.outlineSource)
        self.leftColumns = ColumnChooser.alloc().init().setup(
            ov, "LeftHiddenColumns", [(i, t) for i, t, _ in left_cols], default_hidden=("kind",))
        self.outlineMenu = NSMenu.alloc().initWithTitle_("File")
        self.outlineMenu.setAutoenablesItems_(False)
        self.outlineMenu.setDelegate_(self)
        ov.setMenu_(self.outlineMenu)
        left = scrolled(ov)
        left.setFrame_(NSMakeRect(0, 0, 320, 600))

        # --- right: header, table, buttons
        right = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 680, 600))
        self.backBtn = NSButton.buttonWithTitle_target_action_("‹ Back", self, "goBack:")
        self.pathLabel = label("Select a file or folder on the left", bold=True)
        header = NSStackView.stackViewWithViews_([self.backBtn, self.pathLabel])

        tv = QLVersionsView.alloc().initWithFrame_(NSMakeRect(0, 0, 600, 400))
        tv.qlController = self
        self.versionsSource = VersionsSource.alloc().init().setup(self)
        for ident, title, width in VersionsSource.COLUMNS:
            c = NSTableColumn.alloc().initWithIdentifier_(ident)
            c.headerCell().setStringValue_(title)
            c.setWidth_(width)
            c.setEditable_(False)
            c.setSortDescriptorPrototype_(NSSortDescriptor.sortDescriptorWithKey_ascending_(
                ident, ident in ("name", "host", "id", "change", "disk", "plan")))
            tv.addTableColumn_(c)
            if ident == "name":
                tv.setOutlineTableColumn_(c)       # the ▸ sits at the very left, like Finder
        tv.setRowHeight_(20)
        tv.setIndentationPerLevel_(16)
        tv.setAutoresizesOutlineColumn_(False)
        tv.setAllowsMultipleSelection_(True)
        tv.setUsesAlternatingRowBackgroundColors_(True)
        tv.setDataSource_(self.versionsSource)
        tv.setDelegate_(self.versionsSource)
        tv.setTarget_(self)
        tv.setDoubleAction_("rowDoubleClicked:")
        self.table = tv
        self.rightColumns = ColumnChooser.alloc().init().setup(
            tv, "RightHiddenColumns", [(i, t) for i, t, _ in VersionsSource.COLUMNS],
            default_hidden=("id",))
        self.tableMenu = NSMenu.alloc().initWithTitle_("Version")
        self.tableMenu.setAutoenablesItems_(False)
        self.tableMenu.setDelegate_(self)
        tv.setMenu_(self.tableMenu)
        tableScroll = scrolled(tv)

        self.collapseBox = NSButton.checkboxWithTitle_target_action_(
            "Hide unchanged versions", self, "collapseChanged:")
        self.collapseBox.setState_(1)
        self.qlBtn = NSButton.buttonWithTitle_target_action_("Quick Look", self, "quickLookBackup:")
        self.revealBtn = NSButton.buttonWithTitle_target_action_("Show in Finder", self, "revealCached:")
        self.restoreHereBtn = NSButton.buttonWithTitle_target_action_(
            "Restore Next to Original", self, "restoreNextToOriginal:")
        self.restoreBtn = NSButton.buttonWithTitle_target_action_("Restore to…", self, "restoreTo:")
        spacer = NSView.alloc().init()
        spacer.setContentHuggingPriority_forOrientation_(1, 0)
        buttons = NSStackView.stackViewWithViews_([
            self.collapseBox, spacer, self.qlBtn, self.revealBtn, self.restoreHereBtn, self.restoreBtn])

        for v in (header, tableScroll, buttons):
            v.setTranslatesAutoresizingMaskIntoConstraints_(False)
            right.addSubview_(v)
        NSLayoutConstraint.activateConstraints_([
            header.topAnchor().constraintEqualToAnchor_constant_(right.topAnchor(), 6),
            header.leadingAnchor().constraintEqualToAnchor_constant_(right.leadingAnchor(), 8),
            header.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(right.trailingAnchor(), -8),
            tableScroll.topAnchor().constraintEqualToAnchor_constant_(header.bottomAnchor(), 6),
            tableScroll.leadingAnchor().constraintEqualToAnchor_(right.leadingAnchor()),
            tableScroll.trailingAnchor().constraintEqualToAnchor_(right.trailingAnchor()),
            buttons.topAnchor().constraintEqualToAnchor_constant_(tableScroll.bottomAnchor(), 8),
            buttons.leadingAnchor().constraintEqualToAnchor_constant_(right.leadingAnchor(), 8),
            buttons.trailingAnchor().constraintEqualToAnchor_constant_(right.trailingAnchor(), -8),
            buttons.bottomAnchor().constraintEqualToAnchor_constant_(right.bottomAnchor(), -8),
        ])

        split.addSubview_(left)
        split.addSubview_(right)
        split.setAutosaveName_("ResticControlSplit")
        return container

    @objc.python_method
    def buildManageView(self):
        """Backrest tab: address bar + status, then the web view (or a message)."""
        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1000, 600))
        self.brField = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 22))
        self.brField.setPlaceholderString_("http://127.0.0.1:9898  ·  a port  ·  or host:port of another server")
        self.brField.setStringValue_(self.config.backrest_url)
        self.brField.setTarget_(self)
        self.brField.setAction_("backrestConnect:")           # Return connects
        self.brField.setContentHuggingPriority_forOrientation_(1, 0)
        connect = NSButton.buttonWithTitle_target_action_("Connect", self, "backrestConnect:")
        detect = NSButton.buttonWithTitle_target_action_("Find on This Mac", self, "backrestDetect:")
        self.brStartBtn = NSButton.buttonWithTitle_target_action_("Start Backrest", self, "backrestStart:")
        self.brStartBtn.setHidden_(True)
        browser = NSButton.buttonWithTitle_target_action_("Open in Browser", self, "openBackrestInBrowser:")
        bar = NSStackView.stackViewWithViews_([label("Backrest:"), self.brField, connect, detect,
                                               self.brStartBtn, browser])
        self.brStatus = label("")
        self.brStatus.setTextColor_(NSColor.secondaryLabelColor())

        holder = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1000, 540))
        try:
            from WebKit import WKWebView, WKWebViewConfiguration
            self.webView = WKWebView.alloc().initWithFrame_configuration_(
                holder.bounds(), WKWebViewConfiguration.alloc().init())
            self.webPolicy = WebPolicy.alloc().init().setup(self, links_tab=False)
            self.webView.setNavigationDelegate_(self.webPolicy)
            self.webView.setUIDelegate_(self.webPolicy)
            self.webView.setAutoresizingMask_(WIDTH_HEIGHT)
            self.webView.setHidden_(True)
            holder.addSubview_(self.webView)
        except ImportError:
            self.webView = None
        self.brTitle = label("", bold=True)
        self.brTitle.setFont_(NSFont.boldSystemFontOfSize_(17))
        self.brText = NSTextField.wrappingLabelWithString_("")
        self.brText.setTextColor_(NSColor.secondaryLabelColor())
        self.brText.setPreferredMaxLayoutWidth_(560)
        overlay = NSStackView.stackViewWithViews_([self.brTitle, self.brText])
        overlay.setOrientation_(1)
        overlay.setSpacing_(10)
        overlay.setTranslatesAutoresizingMaskIntoConstraints_(False)
        holder.addSubview_(overlay)
        self.brOverlay = overlay
        NSLayoutConstraint.activateConstraints_([
            overlay.centerXAnchor().constraintEqualToAnchor_(holder.centerXAnchor()),
            overlay.centerYAnchor().constraintEqualToAnchor_constant_(holder.centerYAnchor(), -40),
            overlay.widthAnchor().constraintLessThanOrEqualToConstant_(580),
        ])
        if self.webView is None:
            self.showBackrestMessage("WebKit is missing",
                                     "Install pyobjc-framework-WebKit to show Backrest here, "
                                     "or use Open in Browser.")

        for v in (bar, self.brStatus, holder):
            v.setTranslatesAutoresizingMaskIntoConstraints_(False)
            container.addSubview_(v)
        NSLayoutConstraint.activateConstraints_([
            bar.topAnchor().constraintEqualToAnchor_constant_(container.topAnchor(), 8),
            bar.leadingAnchor().constraintEqualToAnchor_constant_(container.leadingAnchor(), 8),
            bar.trailingAnchor().constraintEqualToAnchor_constant_(container.trailingAnchor(), -8),
            self.brStatus.topAnchor().constraintEqualToAnchor_constant_(bar.bottomAnchor(), 4),
            self.brStatus.leadingAnchor().constraintEqualToAnchor_constant_(container.leadingAnchor(), 10),
            self.brStatus.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(
                container.trailingAnchor(), -10),
            holder.topAnchor().constraintEqualToAnchor_constant_(self.brStatus.bottomAnchor(), 6),
            holder.leadingAnchor().constraintEqualToAnchor_(container.leadingAnchor()),
            holder.trailingAnchor().constraintEqualToAnchor_(container.trailingAnchor()),
            holder.bottomAnchor().constraintEqualToAnchor_(container.bottomAnchor()),
        ])
        return container

    # ---------------------------------------------------------- lifecycle --
    @objc.python_method
    def start(self):
        self.window.makeKeyAndOrderFront_(None)
        if NSUserDefaults.standardUserDefaults().objectForKey_(
                "NSSplitView Subview Frames ResticControlSplit") is None:
            self.split.setPosition_ofDividerAtIndex_(320, 0)
        self.rebuildRepoPopup()
        if not self.config.repos:
            self.openSettings_(None)
        else:
            self.connect()
        self.updateButtons()
        AppHelper.callLater(0.5, self.checkFullDiskAccess)
        # new backups appear by themselves: look for them once a minute
        from Foundation import NSTimer
        self.pollTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_SECONDS, self, "pollSnapshots:", None, True)
        # …and the open folders on the left follow what happens on disk
        self.diskTimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            DISK_CHECK_SECONDS, self, "checkOpenFolders:", None, True)
        run_async(self.journal.recover, self.reportRecovered)

    @objc.python_method
    def reportRecovered(self, notes):
        """Tell the user what was cleaned up after an interrupted restore."""
        if notes:
            alert("Cleaned up after an interrupted restore",
                  "\n".join(notes) + "\n\nYour existing files were not changed.")

    # ------------------------------------------------------ open from Finder --
    @objc.python_method
    def revealPath(self, path):
        """Bring the app forward and select *path* in the local tree (Finder service /
        a file dropped on the Dock icon).  The tree loads level by level, so this sets
        a pending target that continueReveal() pursues as each level arrives."""
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.tabs.selectTabViewItemWithIdentifier_("restore")
        self.pendingReveal = normalize_local_path(path)
        self.status.setStringValue_(f"Opening {self.pendingReveal} …")
        if not self.snapshots:
            return                                   # continues once the tree is loaded
        if self.singleSnapshot() is not None:        # browsing a snapshot: back to this Mac
            self.snapPopup.selectItemAtIndex_(0)
            self.resetTree()
            return
        self.continueReveal()

    @objc.python_method
    def continueReveal(self):
        target = self.pendingReveal
        root = self.outlineSource.root
        if not target or root.children is None:
            return
        found = reveal_chain(target, [c.path for c in root.children])
        if found is None:
            self.pendingReveal = None
            host = self.hostFilter()
            roots = "\n".join(c.path for c in root.children) or "(none)"
            alert("Not in the backup",
                  f"“{target}” isn't inside a folder that is backed up"
                  f"{f' from “{host}”' if host else ''}.\n\nBacked-up folders:\n{roots}")
            return
        root_path, chain = found
        item = next(c for c in root.children if c.path == root_path)
        for p in chain:
            if item.children is None:                # not loaded yet: load, resume later
                self.outline.expandItem_(item)
                return
            self.outline.expandItem_(item)
            nxt = next((c for c in item.children if c.path == p), None)
            if nxt is None and not self.showHidden and any(
                    e.node.path == p for e in (item.allEntries or [])):
                self.toggleHiddenFiles_(None)        # e.g. ~/Library: show hidden items
                self.status.setStringValue_("Showing hidden files to reach it")
                nxt = next((c for c in item.children if c.path == p), None)
            if nxt is None:                          # gone from disk: show its folder
                self.pendingReveal = None
                self.selectOutlineItem(item)
                self.status.setStringValue_(
                    f"“{os.path.basename(p)}” is no longer on this Mac — showing the folder "
                    "that contained it")
                return
            item = nxt
        self.pendingReveal = None
        self.selectOutlineItem(item)
        self.status.setStringValue_("")

    @objc.python_method
    def selectOutlineItem(self, item):
        row = self.outline.rowForItem_(item)
        if row < 0:
            return
        self.outline.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
        self.outline.scrollRowToVisible_(row)
        self.window.makeFirstResponder_(self.outline)

    @objc.python_method
    def checkFullDiskAccess(self):
        """Without Full Disk Access, macOS hides Mail, Safari, parts of ~/Library etc.
        from the app — and from restic, which runs as its child."""
        if has_full_disk_access() is not False:
            return
        defaults = NSUserDefaults.standardUserDefaults()
        if defaults.boolForKey_("SkipFullDiskAccessCheck"):
            return
        a = NSAlert.alloc().init()
        a.setMessageText_("Restic Control doesn't have Full Disk Access")
        a.setInformativeText_(
            "Some folders on this Mac (Mail, Safari, parts of Library, other apps' data) "
            "can't be listed, and restores into them may fail.\n\n"
            "Open System Settings → Privacy & Security → Full Disk Access, switch on "
            "“Restic Control” (use + to add it if it's missing), then quit and reopen the app.")
        a.addButtonWithTitle_("Open System Settings")
        a.addButtonWithTitle_("Later")
        a.setShowsSuppressionButton_(True)
        a.suppressionButton().setTitle_("Don't check again")
        if a.runModal() == 1000:               # first button
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(
                "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"))
        if a.suppressionButton().state():
            defaults.setBool_forKey_(True, "SkipFullDiskAccessCheck")

    @objc.python_method
    def rebuildRepoPopup(self):
        self.repoPopup.removeAllItems()
        for r in self.config.repos:
            self.repoPopup.addItemWithTitle_(r.name)
            self.repoPopup.lastItem().setRepresentedObject_(r.id)
        sel = self.config.selected
        if sel:
            self.repoPopup.selectItemWithTitle_(sel.name)

    @objc.python_method
    def connect(self):
        repo = self.config.selected
        if self.restic:
            self.restic.close()
        self.restic = None
        self.snapshots = []
        self.clearRight()
        if repo is None:
            return
        try:
            self.restic = Restic(repo.spec())
        except ResticError as e:
            alert("Cannot use restic", str(e))
            return
        self.loadSnapshots()

    @objc.python_method
    def loadSnapshots(self):
        restic = self.restic
        self.jobStarted("Loading snapshots …")

        def done(snaps):
            if restic is not self.restic:
                return
            self.snapshots = snaps
            self.jobFinished(f"{len(snaps)} snapshots")
            self.rebuildHostPopup()
            self.rebuildSnapshotPopup()
            self.resetTree()

        def fail(e):
            self.jobFinished(f"Error: {e}")
            alert("Could not list snapshots", str(e))
        run_async(restic.snapshots, done, fail)

    @objc.python_method
    def rebuildHostPopup(self):
        self.hostPopup.removeAllItems()
        self.hostPopup.addItemWithTitle_(ALL_HOSTS)
        for h in hosts_of(self.snapshots):
            self.hostPopup.addItemWithTitle_(h)
            names = hostnames_of(self.snapshots, h)
            if len(names) > 1 or names != [h]:
                self.hostPopup.lastItem().setToolTip_("Snapshots recorded as: " + ", ".join(names))
        repo = self.config.selected
        wanted = (machine_of(repo.host_filter) if repo and repo.host_filter else None) \
            or self.localHostname()
        if wanted and self.hostPopup.itemWithTitle_(wanted):
            self.hostPopup.selectItemWithTitle_(wanted)

    @objc.python_method
    def rebuildSnapshotPopup(self):
        self.snapPopup.removeAllItems()
        self.snapPopup.addItemWithTitle_(LOCAL_FILES)
        self.snapPopup.menu().addItem_(NSMenuItem.separatorItem())
        for s in self.visibleSnapshots():
            self.snapPopup.addItemWithTitle_(s.label())
            self.snapPopup.lastItem().setRepresentedObject_(s.id)

    @objc.python_method
    def visibleSnapshots(self):
        host = self.hostPopup.titleOfSelectedItem()
        if not host or host == ALL_HOSTS:
            return list(self.snapshots)
        return [s for s in self.snapshots if same_machine(s.hostname, host)]

    @objc.python_method
    def singleSnapshot(self):
        sid = self.snapPopup.selectedItem().representedObject() if self.snapPopup.selectedItem() else None
        return next((s for s in self.snapshots if s.id == sid), None) if sid else None

    @objc.python_method
    def resetTree(self):
        self.treeGeneration += 1
        self.clearRight()
        self.outlineSource.reset()

    # --------------------------------------------------------- local host --
    @objc.python_method
    def backupRoots(self):
        """Top-level folders for the local tree: the backed-up paths that exist here."""
        roots = []
        for s in self.visibleSnapshots():
            for p in s.paths:
                if p not in roots and os.path.isdir(p):
                    roots.append(p)
        # drop roots nested in other roots
        roots = [p for p in roots if not any(p != q and p.startswith(q.rstrip("/") + "/")
                                             for q in roots)]
        return sorted(roots) or [os.path.expanduser("~")]

    @objc.python_method
    def localHostname(self):
        """This Mac's machine name as it appears in the repository (any .local/.lan variant)."""
        me = machine_of(socket.gethostname())
        for h in hosts_of(self.snapshots):
            if same_machine(h, me):
                return h
        return None

    # ---------------------------------------------------------- job status --
    @objc.python_method
    def jobStarted(self, msg):
        self.jobs += 1
        self.spinner.startAnimation_(None)
        self.status.setStringValue_(msg)

    @objc.python_method
    def jobFinished(self, msg=""):
        self.jobs = max(0, self.jobs - 1)
        if self.jobs == 0:
            self.spinner.stopAnimation_(None)
        self.status.setStringValue_(msg)

    # --------------------------------------------------------- right pane --
    @objc.python_method
    def clearRight(self):
        self.historyGeneration += 1
        self.stack = []
        self.current_item = None
        self.versionsSource.setData([], [])
        self.table.reloadData()
        self.pathLabel.setStringValue_("Select a file or folder on the left")
        self.updateButtons()

    @objc.python_method
    def hostFilter(self):
        host = self.hostPopup.titleOfSelectedItem()
        return None if not host or host == ALL_HOSTS else host

    @objc.python_method
    def setRows(self, rows, gen, extra=None):
        """Show rows now; fill the "On disk" column in the background."""
        src = self.versionsSource
        extra = extra if extra is not None else [{} for _ in rows]
        if self.restic is not None:
            for (s, n, _c), ex in zip(rows, extra):
                if n.is_dir and ex.get("status") != "pending":
                    known = self.restic.cached_size(s, n)
                    if known is not None:
                        ex["dirsize"] = known
        # the same rows may come again (e.g. while the version search narrows down):
        # keep what was selected
        keep = {(src.rows[i][0].id, src.rows[i][1].path) for i in self.selectedIndexes()
                if i < len(src.rows)}
        self.programmatic = getattr(self, "programmatic", 0) + 1
        try:
            src.setData(rows, extra)
            self.table.reloadData()
            if keep:
                self.selectIndexes([i for i, (s, n, _c) in enumerate(rows) if (s.id, n.path) in keep])
        finally:
            self.programmatic -= 1
        extras = src.extra

        def done(status):
            if gen == self.historyGeneration and src.extra is extras:
                for ex, st in zip(extras, status):
                    ex["disk"] = st
                self.refreshCells()
        run_async(lambda: [disk_status(n) if n.mtime is not None else "" for _, n, _ in rows], done)

    @objc.python_method
    def setExtra(self, gen, extras, row, **values):
        if gen == self.historyGeneration and self.versionsSource.extra is extras and row < len(extras):
            extras[row].update(values)
            self.refreshCells()

    @objc.python_method
    def refreshCells(self):
        """Redraw the visible cells of the right pane with fresh values.  Unlike
        reloadData this never touches the tree's structure or the selection, so it is
        safe to call as background results arrive (while the user clicks around)."""
        tv = self.table
        rows, cols = tv.numberOfRows(), tv.numberOfColumns()
        if rows <= 0 or cols <= 0:
            return
        vis = tv.rowsInRect_(tv.visibleRect())
        start, length = vis.location, vis.length
        if length <= 0:
            return
        tv.reloadDataForRowIndexes_columnIndexes_(
            NSIndexSet.indexSetWithIndexesInRange_((start, min(length, rows - start))),
            NSIndexSet.indexSetWithIndexesInRange_((0, cols)))

    @objc.python_method
    def reloadChildren(self, i):
        """Row *i* got new children: reload that part of the tree, keeping the selection."""
        sel = self.selectedIndexes()
        self.programmatic = getattr(self, "programmatic", 0) + 1
        try:
            self.table.reloadItem_reloadChildren_(self.versionsSource.items[i], True)
            self.selectIndexes(sel)
        finally:
            self.programmatic -= 1

    @objc.python_method
    def updateModeControls(self):
        """The checkbox means 'hide unchanged versions' for files, 'only changes' when browsing."""
        box = self.collapseBox
        if self.stack:
            _, _, prev = self.stack[-1]
            box.setTitle_("Only show changes")
            box.setHidden_(prev is None)
            box.setState_(1 if self.onlyChanges else 0)
        elif self.current_item is not None and self.current_item.is_dir:
            box.setHidden_(True)
        else:
            box.setTitle_("Hide unchanged versions")
            box.setHidden_(False)
            box.setState_(1 if self.hideUnchanged else 0)

    @objc.python_method
    def showHistory(self, item, quiet=False):
        """Right pane for the item selected on the left.

        File:   all versions, found with ONE `restic find` over every snapshot.
        Folder: its distinct versions, found by binary search over folder
                fingerprints (2 restic calls if it never changed).
        """
        self.current_item = item
        self.stack = []
        self.historyGeneration += 1
        gen = self.historyGeneration
        restic, snaps, host = self.restic, self.visibleSnapshots(), self.hostFilter()
        if not quiet:                        # quiet: keep showing the old rows until the new ones
            self.pendingState = None
            self.folderVersions = []
            self.versionsSource.setData([], [])
            self.versionsSource.dimUnchanged = False
            self.table.reloadData()
            self.rightColumns.setModeHidden(())
            self.updateModeControls()
            self.updateButtons()
        if restic is None:
            return
        if not quiet:                        # (quiet: running checks are still useful)
            restic.cancel("history")
            restic.cancel_group("view")      # stale folder checks must not slow this view down
        if item.is_dir:
            self.showFolderVersions(item, snaps, gen, quiet)
            return

        self.pathLabel.setStringValue_(f"Versions of {item.path}")
        mine = [s for s in snaps if not host or same_machine(s.hostname, host)]
        covering = snapshots_covering(mine, item.path)
        if not quiet and len(restic.cached_versions(item.path, covering)) < len(covering):
            # restic has to run: show every candidate snapshot right away, weed out afterwards
            ghost = Node(name=posixpath.basename(item.path), path=item.path,
                         type=item.node.type if item.node else "file", size=None, mtime=None)
            self.setRows([(s, ghost, 1) for s in covering], gen,
                         [{"status": "pending", "change": "checking …"} for _ in covering])

        def done(versions):
            if gen != self.historyGeneration:
                return
            extra = []
            for i, v in enumerate(versions):
                older = versions[i + 1] if i + 1 < len(versions) else None
                if older is None:
                    change = "oldest version"
                else:
                    change = fmt_delta(v.node.size, older.node.size)
                extra.append({"first": v.oldest.time, "change": change,
                              "key": ("oldest", v.oldest.id)})
            self.setRows([(v.snapshot, v.node, v.count) for v in versions], gen, extra)
            self.applyPendingState()
            if not quiet:
                self.jobFinished(f"{len(versions)} version(s) of {item.name}" if versions
                                 else f"{item.name} is not in any snapshot")
            self.updateButtons()

        def fail(e):
            if not quiet:
                self.jobFinished("" if isinstance(e, Cancelled) else f"Error: {e}")
        if not quiet:
            self.jobStarted(f"Searching snapshots for {item.name} …")
        run_async(lambda: restic.find_versions(item.path, snaps, host=host,
                                               collapse=self.hideUnchanged), done, fail)

    @objc.python_method
    def showFolderVersions(self, item, snaps, gen, quiet=False):
        restic, path = self.restic, item.path
        name = item.name if item.name != item.path else (posixpath.basename(path) or path)
        node = Node(name=name, path=path, type="dir", size=None, mtime=None)
        covering = snapshots_for_folder(snaps, path)
        self.pathLabel.setStringValue_(f"Versions of {path}/  —  click the triangle next to a version to see what changed since the version below it")

        # quiet refresh: versions that are still there keep their Changes text meanwhile
        known = (self.pendingState or {}).get("changes", {}) if quiet else {}

        def show(versions, final):
            if gen != self.historyGeneration:
                return
            self.folderVersions = versions
            pending = "…" if final else "narrowing down …"
            extra = []
            for i, v in enumerate(versions):
                prev = versions[i + 1] if i + 1 < len(versions) else None
                key, prevkey = ("fp", v.fp), (("fp", prev.fp) if prev else None)
                old_text, old_prev = known.get(key, (None, None))
                extra.append({"first": v.oldest.time, "final": final, "key": key,
                              "prevkey": prevkey,
                              "change": old_text if old_text and old_prev == prevkey else pending,
                              "plans": ", ".join(sorted({plan_of(s) for s in v.snapshots} - {""})),
                              "prev": prev.newest if prev else None})
            self.setRows([(v.newest, node, v.count) for v in versions], gen, extra)
            if final:
                self.applyPendingState()
            self.updateButtons()

        def on_progress(versions, calls):
            if gen != self.historyGeneration or quiet:
                return
            show(versions, False)
            self.status.setStringValue_(f"Comparing folder fingerprints … {calls} checks")

        def progress(versions, calls):            # called on the worker thread
            AppHelper.callAfter(on_progress, versions, calls)

        def done(versions):
            if gen != self.historyGeneration:
                return
            if not quiet:
                self.jobFinished(f"{len(versions)} version(s) of {name}/" if versions
                                 else f"{name}/ is not in any snapshot")
            show(versions, True)
            self.computeFolderChanges(gen, path, versions)

        def fail(e):
            if not quiet:
                self.jobFinished("" if isinstance(e, Cancelled) else f"Error: {e}")
        if not quiet:
            self.jobStarted(f"Comparing {len(covering)} snapshots of {name}/ …")
        run_async(lambda: restic.folder_versions(covering, path, progress=progress,
                                                 stop=lambda: gen != self.historyGeneration),
                  done, fail)

    @objc.python_method
    def computeFolderChanges(self, gen, path, versions):
        """Fill the Changes column: direct changes first (cheap), then recursive totals."""
        restic, extras = self.restic, self.versionsSource.extra

        def work():
            summaries = []
            for i, v in enumerate(versions):
                if gen != self.historyGeneration:
                    return
                if i + 1 == len(versions):
                    text = "oldest version"
                else:
                    ch = restic.folder_changes(v.newest, versions[i + 1].newest, path)
                    text = ch.summary() if ch is not None else ""
                summaries.append(text)
                AppHelper.callAfter(self.setExtra, gen, extras, i, change=text)
            for i, v in enumerate(versions[:-1]):
                if gen != self.historyGeneration:
                    return
                st = restic.diff_stats(versions[i + 1].newest, v.newest, path)
                if st is None:
                    return            # restic < 0.17
                total = f"{st['changed']} changed, {st['added']} added, {st['removed']} removed"
                AppHelper.callAfter(self.setExtra, gen, extras, i,
                                    change=f"{summaries[i]}  ·  in total: {total} files")
        run_async(work, lambda _: None)

    @objc.python_method
    def browse(self, snap, dir_node, prev=None):
        """Right pane: contents of *dir_node* in *snap*, optionally compared with *prev*."""
        self.stack.append((snap, dir_node, prev))
        self.showBrowseTop()

    @objc.python_method
    def showBrowseTop(self):
        snap, dir_node, prev = self.stack[-1]
        if self.restic is not None:
            self.restic.cancel_group("view")
        self.historyGeneration += 1
        gen = self.historyGeneration
        restic, only = self.restic, self.onlyChanges and prev is not None
        vs = f"   compared with {fmt_time(prev.time)}" if prev else ""
        self.pathLabel.setStringValue_(f"{dir_node.path}   @ {fmt_time(snap.time)}{vs}")
        self.versionsSource.setData([], [])
        self.versionsSource.dimUnchanged = prev is not None
        self.table.reloadData()
        self.rightColumns.setModeHidden({"count", "first"} | ({"change"} if prev is None else set()))
        self.updateModeControls()
        self.updateButtons()

        def work():
            nodes = restic.ls(snap, dir_node.path)
            if prev is None:
                return nodes, None, []
            ch = restic.folder_changes(snap, prev, dir_node.path)
            removed = [n for n in restic.ls(prev, dir_node.path) if ch and ch.of(n.name) == "removed"]
            return nodes, ch, removed

        def done(result):
            if gen != self.historyGeneration:
                return
            nodes, ch, removed = result
            rows, extra = [], []
            for sn, n in [(snap, n) for n in nodes] + [(prev, n) for n in removed]:
                if n.is_hidden and not self.showHidden:
                    continue
                st = ch.of(n.name) if ch is not None else ""
                if only and st in ("", "meta"):
                    continue
                rows.append((sn, n, 1))
                extra.append({"status": st, "change": VersionsSource.STATUS_LABELS.get(st, st)})
            self.setRows(rows, gen, extra)
            if rows:
                self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
            self.window.makeFirstResponder_(self.table)
            if ch is not None:
                msg = ch.summary()
            else:
                msg = f"{len(nodes)} item(s)" if nodes else "Folder not in this snapshot"
            self.jobFinished(msg)
            self.updateButtons()

        def fail(e):
            self.jobFinished("" if isinstance(e, Cancelled) else f"Error: {e}")
        self.jobStarted(f"Listing {dir_node.path} …")
        run_async(work, done, fail)

    @objc.python_method
    def selectedIndexes(self):
        """Canonical indexes (into versionsSource.rows) of the selected rows, top to bottom."""
        tv, idx = self.table, self.table.selectedRowIndexes()
        out, r, n = [], idx.firstIndex(), tv.numberOfRows()
        while r != 0x7FFFFFFFFFFFFFFF and r < n:    # NSNotFound
            item = tv.itemAtRow_(r)
            if item is not None:
                out.append(item.i)
            r = idx.indexGreaterThanIndex_(r)
        return out

    @objc.python_method
    def selectIndexes(self, canonical):
        items, sel = self.versionsSource.items, NSMutableIndexSet.indexSet()
        for i in canonical:
            if 0 <= i < len(items):
                r = self.table.rowForItem_(items[i])
                if r >= 0:
                    sel.addIndex_(r)
        self.programmatic = getattr(self, "programmatic", 0) + 1
        try:
            self.table.selectRowIndexes_byExtendingSelection_(sel, False)
        finally:
            self.programmatic -= 1

    @objc.python_method
    def selectedRows(self):
        rows = self.versionsSource.rows
        return [rows[i] for i in self.selectedIndexes() if i < len(rows)]

    @objc.python_method
    def selectedExtra(self):
        ex = self.versionsSource.extra
        return [ex[i] for i in self.selectedIndexes() if i < len(ex)]

    @objc.python_method
    def selectedOutlineItems(self):
        ov, idx = self.outline, self.outline.selectedRowIndexes()
        out, r = [], idx.firstIndex()
        while r != 0x7FFFFFFFFFFFFFFF:
            it = ov.itemAtRow_(r)
            if it is not None and not it.placeholder:
                out.append(it)
            r = idx.indexGreaterThanIndex_(r)
        return out

    @objc.python_method
    def updateButtons(self):
        sel = self.selectedRows()
        self.backBtn.setHidden_(not self.stack)
        self.qlBtn.setEnabled_(bool(sel))
        self.revealBtn.setEnabled_(bool(sel))
        self.restoreBtn.setEnabled_(bool(sel))
        self.restoreHereBtn.setEnabled_(bool(sel))

    # ------------------------------------------------------------ actions --
    def repoChanged_(self, sender):
        rid = sender.selectedItem().representedObject()
        self.config.selected_id = rid
        self.config.save()
        self.webLoaded = False
        self.connect()

    def hostChanged_(self, sender):
        repo = self.config.selected
        if repo:
            t = sender.titleOfSelectedItem()
            repo.host_filter = "" if t == ALL_HOSTS else t
            self.config.save()
        self.rebuildSnapshotPopup()
        self.resetTree()

    def snapshotChanged_(self, sender):
        self.resetTree()

    def refresh_(self, sender):
        if self.restic:
            self.checkForNewBackups(force=True)

    def checkOpenFolders_(self, timer):
        if self.restic is not None and not self.pendingReveal:
            self.outlineSource.refreshOpen()

    def pollSnapshots_(self, timer):
        if self.restic is not None and not self.polling and not self.restoresRunning:
            self.checkForNewBackups()

    @objc.python_method
    def checkForNewBackups(self, force=False):
        """Look for new (or deleted) backups and add them to what's shown, keeping the
        view as it is.  The poll is one cheap `restic list snapshots` (a directory
        listing); only when that differs are the snapshots read."""
        restic, known = self.restic, {s.id for s in self.snapshots}
        self.polling = True
        if force:
            self.jobStarted("Looking for new backups …")

        def work():
            if not force and restic.snapshot_ids() == known:
                return None
            return restic.snapshots()

        def done(snaps):
            self.polling = False
            if force:
                self.jobFinished("")
            if restic is self.restic and snaps is not None:
                self.applySnapshots(snaps, announce=True)
            elif force:
                self.status.setStringValue_("No new backups")

        def fail(e):
            self.polling = False
            if force:
                self.jobFinished("" if isinstance(e, Cancelled) else f"Could not check for backups: {e}")
        run_async(work, done, fail)

    @objc.python_method
    def applySnapshots(self, snaps, announce=False):
        """Take a new list of snapshots without resetting anything: the menus keep their
        choice, the left tree stays, and the right pane is recomputed quietly with its
        selection, open rows and scroll position kept."""
        old = {s.id for s in self.snapshots}
        now = {s.id for s in snaps}
        added = [s for s in snaps if s.id not in old]
        removed = len(old - now)
        if not added and not removed:
            if announce:
                self.status.setStringValue_("No new backups")
            return
        host = self.hostPopup.titleOfSelectedItem()
        single = self.singleSnapshot()
        self.snapshots = snaps
        self.rebuildHostPopup()
        if host and self.hostPopup.itemWithTitle_(host):
            self.hostPopup.selectItemWithTitle_(host)
        self.rebuildSnapshotPopup()
        if single is not None:
            idx = self.snapPopup.indexOfItemWithRepresentedObject_(single.id)
            if idx >= 0:
                self.snapPopup.selectItemAtIndex_(idx)
            else:                                 # the backup being browsed was deleted
                self.resetTree()
                return
        if announce:
            parts = []
            if added:
                newest = max(added, key=lambda s: s.time)
                plan = plan_of(newest)
                parts.append(f"{len(added)} new backup{'s' if len(added) > 1 else ''} "
                             f"(latest {fmt_time(newest.time)}{', ' + plan if plan else ''})")
            if removed:
                parts.append(f"{removed} backup{'s' if removed > 1 else ''} removed")
            self.status.setStringValue_(" · ".join(parts))
        if single is None and self.current_item is not None and not self.stack:
            self.pendingState = self.viewState()
            self.showHistory(self.current_item, quiet=True)

    # -------------------------------------------- keeping the view on refresh --
    @objc.python_method
    def rowKey(self, i):
        """What row *i* stands for, independent of snapshots that come and go: a folder
        version by its content (tree id), a file version by its oldest snapshot, a
        change-tree row by its version and path."""
        src = self.versionsSource
        if i >= len(src.extra):
            return None
        ex = src.extra[i]
        if ex.get("kind") == "change":
            root = src.extra[ex["root"]] if ex["root"] < len(src.extra) else {}
            return (root.get("key"), src.rows[i][1].path)
        return (ex.get("key"), None)

    @objc.python_method
    def viewState(self):
        src, tv = self.versionsSource, self.table
        n = len(src.rows)
        first = None
        try:
            vis = tv.rowsInRect_(tv.visibleRect())
            item = tv.itemAtRow_(vis.location) if vis.length else None
            first = self.rowKey(item.i) if item is not None else None
        except Exception:  # noqa: BLE001
            pass
        return {"expanded": {self.rowKey(i) for i in range(n) if src.extra[i].get("expanded")},
                "selected": {self.rowKey(i) for i in self.selectedIndexes()},
                "first": first,
                "changes": {ex["key"]: (ex.get("change"), ex.get("prevkey"))
                            for ex in src.extra if ex.get("key") and "prevkey" in ex},
                "until": time.monotonic() + 10}

    @objc.python_method
    def applyPendingState(self, rows=None):
        """Reopen / reselect what was open / selected before a quiet refresh, as far as
        the rows exist (again).  Called after the rows are set and whenever a reopened
        row's children arrive, so nested rows come back level by level."""
        st = self.pendingState
        if st is None:
            return
        if time.monotonic() > st["until"]:
            self.pendingState = None
            return
        self.programmatic = getattr(self, "programmatic", 0) + 1
        try:
            self._applyPendingState(st, rows)
        finally:
            self.programmatic -= 1

    @objc.python_method
    def _applyPendingState(self, st, rows):
        src, tv = self.versionsSource, self.table
        candidates = range(len(src.rows)) if rows is None else rows
        for i in candidates:
            if self.rowKey(i) in st["expanded"] and not tv.isItemExpanded_(src.items[i]) \
                    and self.canExpand(i):
                self.toggleRow(i, True)
        sel = [i for i in range(len(src.rows)) if self.rowKey(i) in st["selected"]]
        if sel and set(self.selectedIndexes()) != set(sel):
            self.selectIndexes(sel)
        if st["first"] is not None:
            for i in range(len(src.rows)):
                if self.rowKey(i) == st["first"]:
                    r = tv.rowForItem_(src.items[i])
                    if r >= 0:
                        tv.scrollRowToVisible_(r)
                    break


    def collapseChanged_(self, sender):
        on = bool(sender.state())
        if self.stack:
            self.onlyChanges = on
            self.showBrowseTop()
        else:
            self.hideUnchanged = on
            if self.current_item is not None:
                self.showHistory(self.current_item)

    def goBack_(self, sender):
        if not self.stack:
            return
        self.stack.pop()
        if self.stack:
            self.showBrowseTop()
        elif self.current_item is not None:
            self.showHistory(self.current_item)

    def rowDoubleClicked_(self, sender):
        sel = self.selectedRows()
        if len(sel) != 1:
            return
        snap, node, _ = sel[0]
        if not node.is_dir:
            self.toggleQuickLook_(sender)
            return
        i = self.selectedIndexes()[0]
        ex0 = self.versionsSource.extra[i] if i < len(self.versionsSource.extra) else {}
        if ex0.get("kind") == "change":
            # a folder in a change tree: go there on the left — or, if it's no longer on
            # this Mac, to the nearest folder above it that is (▸ opens it in the tree)
            self.revealPath(node.path)
            return
        if self.canExpand(i):                     # a folder version / changed folder: open ▸
            self.toggleRow(i)
            return
        ex = (self.selectedExtra() or [{}])[0]
        if not self.stack:
            # a folder version: compare with the next older version
            self.browse(snap, node, ex.get("prev"))
            return
        _, _, prev = self.stack[-1]
        # keep comparing on the way down, except into folders that no longer exist
        self.browse(snap, node, None if ex.get("status") == "removed" else prev)

    # ------------------------------------------------ change tree (right pane) --
    @objc.python_method
    def canExpand(self, i):
        """Can row *i* be opened ▸ to show what changed inside?"""
        if self.stack or self.restic is None:
            return False
        src = self.versionsSource
        if i is None or i >= len(src.rows) or i >= len(src.extra):
            return False
        node, ex = src.rows[i][1], src.extra[i]
        if not node.is_dir:
            return False
        if ex.get("kind") == "change":
            return ex["entry"].status in ("changed", "added", "removed")
        return bool(ex.get("final")) and ex.get("prev") is not None

    def toggleSelectedRow_(self, sender):
        idx = self.selectedIndexes()
        if len(idx) == 1 and self.canExpand(idx[0]):
            self.toggleRow(idx[0])

    def browseSelected_(self, sender):
        sel, ex = self.selectedRows(), self.selectedExtra()
        if len(sel) != 1 or not sel[0][1].is_dir:
            return
        snap, node, _ = sel[0]
        e = ex[0] if ex else {}
        prev = e.get("old") if e.get("kind") == "change" and e.get("status") == "changed" \
            else (e.get("prev") if e.get("kind") != "change" else None)
        self.browse(snap, node, prev)

    def showAllVersions_(self, sender):
        sel = self.selectedRows()
        if len(sel) == 1:
            self.revealPath(sel[0][1].path)

    @objc.python_method
    def toggleRow(self, i, open_=None):
        """Open (▾) or close (▸) row *i*; opening loads what changed inside."""
        item, tv = self.versionsSource.items[i], self.table
        expanded = bool(tv.isItemExpanded_(item))
        want = (not expanded) if open_ is None else open_
        if want and not expanded:
            tv.expandItem_(item)                  # -> outlineViewItemWillExpand_ -> rowWillExpand
        elif expanded and not want:
            tv.collapseItem_(item)

    @objc.python_method
    def rowWillExpand(self, i):
        ex = self.versionsSource.extra[i]
        ex["expanded"] = True
        if ex.get("children") is None:
            self.loadChanges(i)

    @objc.python_method
    def loadChanges(self, i):
        """Fill in what differs inside row *i*.

        If the version's `restic diff` has finished (it also produced the "in total"
        counts), the level is built from it at once, without calling restic; only the
        sizes and dates are then read in the background (one cached `cat tree` per side).
        Otherwise the level comes from those two `cat tree`s."""
        src, restic, gen = self.versionsSource, self.restic, self.historyGeneration
        rows, extras = src.rows, src.extra
        snap, node, _ = rows[i]
        ex = extras[i]
        if ex.get("kind") == "change":
            new, old, root, depth = ex["new"], ex["old"], ex["root"], ex["depth"] + 1
        else:
            new, old, root, depth = snap, ex["prev"], i, 1
        if ex.get("loading"):
            return
        root_path = rows[root][1].path

        def current():
            return gen == self.historyGeneration and src.extra is extras

        def with_disk(entries):
            return [(e, disk_status(e.node) if e.node.mtime is not None else "") for e in entries]

        def add_rows(result):
            kids = [src.add((new if e.new is not None else old, e.node, 1),
                            {"kind": "change", "entry": e, "status": e.status, "disk": disk,
                             "parent": i, "depth": depth, "new": new, "old": old, "root": root})
                    for e, disk in result]
            ex["children"] = kids
            src.kidOrder.pop(i, None)
            if depth == 1:
                self.startTotals(i, old, new, node.path)
            if self.pendingState is not None:     # quiet refresh: reopen / reselect these too
                AppHelper.callAfter(lambda: current() and self.applyPendingState(kids))
                return kids
            real = [k for k in kids if extras[k]["status"] != "meta"]
            if len(real) == 1 and self.canExpand(real[0]):
                # one changed subfolder: follow it down (after this expansion has finished)
                AppHelper.callAfter(lambda k=real[0]: current() and ex.get("expanded")
                                    and self.toggleRow(k, True))
            return kids

        quick = restic.quick_change_entries(new, old, node.path,
                                            restic.cached_totals(old, new, root_path))
        if quick is not None:
            add_rows([(e, "") for e in quick])    # called while the row opens: no reload needed
            if not quick:
                self.status.setStringValue_(f"No differences inside {node.name}/")

            def details_done(result):
                if not current():
                    return
                by_name = {extras[k]["entry"].name: k for k in ex["children"]}
                added = []
                for e, disk in result:
                    k = by_name.get(e.name)
                    if k is None:                 # e.g. a difference restic diff doesn't show
                        added.append(src.add((new if e.new is not None else old, e.node, 1),
                                             {"kind": "change", "entry": e, "status": e.status,
                                              "disk": disk, "parent": i, "depth": depth,
                                              "new": new, "old": old, "root": root}))
                        continue
                    kx = extras[k]
                    kx["entry"] = replace(e, status=kx["status"])   # keep the diff's verdict
                    kx["disk"] = disk
                    src.rows[k] = (new if e.new is not None else old, e.node, 1)
                if added:
                    ex["children"] = ex["children"] + added
                    src.kidOrder.pop(i, None)
                    self.reloadChildren(i)
                else:
                    self.refreshCells()
            run_async(lambda: with_disk(restic.change_entries(new, old, node.path)),
                      details_done, lambda e: None)
            return

        ex["loading"] = True
        self.jobStarted(f"Comparing {node.name}/ …")

        def done(result):
            ex["loading"] = False
            self.jobFinished("" if result else f"No differences inside {node.name}/")
            if not current():
                return
            add_rows(result)
            self.reloadChildren(i)

        def fail(e):
            ex["loading"] = False
            self.jobFinished("" if isinstance(e, Cancelled) else f"Error: {e}")
            if current():
                self.table.collapseItem_(src.items[i])
        run_async(lambda: with_disk(restic.change_entries(new, old, node.path)), done, fail)

    @objc.python_method
    def startTotals(self, i, old, new, path):
        """One streamed `restic diff` for version row *i*: counts of changed / added /
        removed files below every folder, shown in the tree as they arrive."""
        src, restic, gen = self.versionsSource, self.restic, self.historyGeneration
        extras = src.extra
        ex = extras[i]
        if "totals" in ex:
            return
        ex["totals"] = None

        def current():
            return gen == self.historyGeneration and src.extra is extras

        last = [0.0]

        def progress(totals):                     # worker thread; redraw at most twice a second
            ex["totals"] = totals
            now = time.monotonic()
            if now - last[0] >= 0.5:
                last[0] = now
                AppHelper.callAfter(lambda: current() and self.refreshCells())

        def done(totals):
            ex["totals"] = totals
            if current():
                self.refreshCells()

        def fail(e):
            if not isinstance(e, Cancelled) and current():
                self.status.setStringValue_(f"Could not count the changes below: {e}")
        run_async(lambda: restic.change_totals(old, new, path, progress=progress, group="view"),
                  done, fail)

    def openSettings_(self, sender):
        from .settings import SettingsController
        if self.settings is None:
            self.settings = SettingsController.alloc().init().setup(self)
        self.settings.show()

    @objc.python_method
    def settingsSaved(self):
        self.rebuildRepoPopup()
        self.webLoaded = False
        self.brField.setStringValue_(self.config.backrest_url)
        self.connect()

    # ------------------------------------------------------- Quick Look --
    def toggleQuickLook_(self, sender):
        panel = QLPreviewPanel.sharedPreviewPanel()
        if QLPreviewPanel.sharedPreviewPanelExists() and panel.isVisible():
            panel.orderOut_(None)
            self.previewPrevious = False
            return
        from_outline = sender is self.outline or (
            sender is not self.table and self.window.firstResponder() is self.outline)
        self.previewFromOutline = from_outline
        if from_outline and not self.selectedOutlineItems():
            return
        if not from_outline and not self.selectedRows():
            return
        self.window.makeFirstResponder_(self.outline if from_outline else self.table)
        panel.makeKeyAndOrderFront_(None)

    def quickLookLocal_(self, sender):
        self.toggleQuickLook_(self.outline)

    def quickLookBackup_(self, sender):
        self.previewPrevious = False
        self.toggleQuickLook_(self.table)

    def quickLookPrevious_(self, sender):
        panel = QLPreviewPanel.sharedPreviewPanel()
        self.previewPrevious = True
        if self.previewPanelActive:
            self.refreshPreview()
        else:
            self.toggleQuickLook_(self.table)

    @objc.python_method
    def refreshPreview(self):
        """Point the panel at the selection.  Backup files that are not fetched yet show
        a placeholder page at once (with the real title), replaced when the download ends."""
        panel = QLPreviewPanel.sharedPreviewPanel()
        if self.previewFromOutline:
            self.previewItems = [preview_item(it.path) for it in self.selectedOutlineItems()
                                 if os.path.lexists(it.path)]
            panel.reloadData()
            return
        sel, restic = self.selectedRows(), self.restic
        if restic is None or not sel:
            self.previewItems = []
            panel.reloadData()
            return
        if self.previewPrevious:                  # "Quick Look Previous Version" of a change row
            exs = self.selectedExtra()
            sel = [(ex["old"], ex["entry"].old, 1) if ex.get("kind") == "change" and ex["entry"].old
                   is not None else row for row, ex in zip(sel, exs)]
        items, todo = [], []
        for i, (s, n, _) in enumerate(sel):
            if n.is_dir:
                items.append(preview_item(placeholder_page(
                    restic.cache_dir, n.name, fmt_time(s.time),
                    "Open ▸ the row in the list to see what changed inside.", verb="Folder"),
                    n.name + "/"))
                continue
            cached = restic.cache_path(s, n)
            if os.path.exists(cached):
                items.append(preview_item(cached, n.name))
                continue
            size = fmt_size(n.size) + " · " if n.size is not None else ""
            items.append(preview_item(placeholder_page(
                restic.cache_dir, n.name, fmt_time(s.time),
                size + "restic needs a few seconds to open the repository"), n.name))
            todo.append(i)
        self.previewItems = items
        panel.reloadData()
        if not todo:
            return
        items_ref, gen = self.previewItems, self.historyGeneration

        def work():
            return [(i, restic.materialize(sel[i][0], sel[i][1])) for i in todo]

        def done(paths):
            self.jobFinished("")
            if gen != self.historyGeneration or self.previewItems is not items_ref:
                return
            for i, p in paths:
                items_ref[i] = preview_item(p, sel[i][1].name)
            if self.previewPanelActive:
                panel.reloadData()
                panel.refreshCurrentPreviewItem()

        def fail(e):
            self.jobFinished(f"Quick Look failed: {e}")
        total = sum(sel[i][1].size or 0 for i in todo)
        self.jobStarted(f"Fetching {fmt_size(total)} for preview …")
        run_async(work, done, fail)

    @objc.typedSelector(b"q@:@")
    def numberOfPreviewItemsInPreviewPanel_(self, panel):
        return len(self.previewItems)

    @objc.typedSelector(b"@@:@q")
    def previewPanel_previewItemAtIndex_(self, panel, index):
        return self.previewItems[index] if 0 <= index < len(self.previewItems) else None

    @objc.typedSelector(b"Z@:@@")
    def previewPanel_handleEvent_(self, panel, event):
        if event.type() == KEY_DOWN:     # arrow keys etc. drive the list behind the panel
            (self.outline if self.previewFromOutline else self.table).keyDown_(event)
            return True
        return False

    # ------------------------------------------------------ context menus --
    def menuNeedsUpdate_(self, menu):
        menu.removeAllItems()
        if menu is self.tableMenu:
            self.buildTableMenu(menu)
        elif menu is self.outlineMenu:
            self.buildOutlineMenu(menu)

    @objc.python_method
    def _add(self, menu, title, action, enabled=True, obj=None):
        it = menu.addItemWithTitle_action_keyEquivalent_(title, action, "")
        it.setTarget_(self)
        it.setEnabled_(bool(enabled))
        if obj is not None:
            it.setRepresentedObject_(obj)
        return it

    @objc.python_method
    def _selectClicked(self, view):
        row = view.clickedRow()
        if row >= 0 and not view.isRowSelected_(row):
            view.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)

    @objc.python_method
    def _cacheDir(self):
        return self.restic.cache_dir if self.restic else \
            os.path.expanduser("~/Library/Caches/ResticControl")

    @objc.python_method
    def buildTableMenu(self, menu):
        self._selectClicked(self.table)
        sel = self.selectedRows()
        if not sel or self.restic is None:
            self._add(menu, "No selection", None, enabled=False)
            return
        files = [n for _, n, _ in sel if not n.is_dir]
        idx = self.selectedIndexes()
        ex = (self.selectedExtra() or [{}])[0]
        if len(sel) == 1 and sel[0][1].is_dir:
            if self.stack:
                self._add(menu, "Open Folder", "rowDoubleClicked:")
            else:
                if self.canExpand(idx[0]):
                    self._add(menu, "Hide Changes" if ex.get("expanded") else "Show What Changed",
                              "toggleSelectedRow:")
                self._add(menu, "Browse This Version", "browseSelected:")
        self._add(menu, "Quick Look", "quickLookBackup:")
        if len(sel) == 1 and ex.get("kind") == "change" and not sel[0][1].is_dir \
                and ex["entry"].status in ("changed", "meta") and ex["entry"].old is not None:
            self._add(menu, "Quick Look Previous Version", "quickLookPrevious:")
        if files:
            self._add(menu, "Open Copy", "openBackupCopy:")
            add_open_with_submenu(menu, self, "openBackupWith:",
                                  apps_for_extension(self._cacheDir(), files[0].name))
        if any(os.path.lexists(n.path) for _s, n, _c in sel):
            self._add(menu, "Show in Finder", "revealOnDisk:")
        else:
            self._add(menu, "Show Enclosing Folder in Finder", "revealOnDisk:")
        if any(n.is_dir for _s, n, _c in sel):
            self._add(menu, "Calculate Size", "calculateBackupSize:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Restore to…", "restoreTo:")
        self._add(menu, "Restore Next to Original", "restoreNextToOriginal:")
        if files:
            self._add(menu, "Show Preview Copy in Finder", "revealCached:")
        menu.addItem_(NSMenuItem.separatorItem())
        if len(sel) == 1 and ex.get("kind") == "change":
            self._add(menu, "Show All Versions", "showAllVersions:")
        self._add(menu, "Copy Path", "copyPath:", obj="right")

    @objc.python_method
    def buildOutlineMenu(self, menu):
        self._selectClicked(self.outline)
        items = [it for it in self.selectedOutlineItems() if os.path.lexists(it.path)]
        if not items:
            self._add(menu, "Not on this Mac", None, enabled=False)
            return
        files = [it for it in items if not it.is_dir]
        self._add(menu, "Open", "openLocal:")
        if files:
            add_open_with_submenu(menu, self, "openLocalWith:",
                                  apps_for_extension(self._cacheDir(), files[0].name))
        self._add(menu, "Quick Look", "quickLookLocal:")
        self._add(menu, "Show in Finder", "revealLocal:")
        if any(it.is_dir for it in items):
            self._add(menu, "Calculate Size", "calculateLocalSize:")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Copy Path", "copyPath:", obj="left")

    def openBackupCopy_(self, sender):
        self._openBackup(None)

    def openBackupWith_(self, sender):
        self._openBackup(sender.representedObject())

    @objc.python_method
    def _openBackup(self, app):
        """Open read-only preview copies of backup files (this is not a restore)."""
        sel, restic = [(s, n) for s, n, _ in self.selectedRows() if not n.is_dir], self.restic

        def done(paths):
            self.jobFinished("Opened read-only copies — use Restore to keep a file")
            for p in paths:
                open_with(p, app)

        def fail(e):
            self.jobFinished("Open failed")
            alert("Could not fetch the file", str(e))
        self.jobStarted("Fetching …")
        run_async(lambda: [restic.materialize(s, n) for s, n in sel], done, fail)

    def calculateBackupSize_(self, sender):
        """Measure the selected backup folders once; the result is kept permanently."""
        restic, src = self.restic, self.versionsSource
        idx = [i for i in self.selectedIndexes() if i < len(src.rows) and src.rows[i][1].is_dir]
        if restic is None or not idx:
            return
        rows, extras, gen = src.rows, src.extra, self.historyGeneration
        state = {"running": True, "stopped": False}

        def stop():
            state["stopped"] = True
            restic.cancel("measure")
        sheet = ProgressSheet.alloc().init().setup(self.window, "Calculating size", on_cancel=stop)

        def work():
            out = []
            for k, i in enumerate(idx):
                if state["stopped"]:
                    raise Cancelled("stopped")
                s, n, _c = rows[i]
                of = f" ({k + 1} of {len(idx)})" if len(idx) > 1 else ""

                def prog(files, nbytes, name=n.name, of=of):
                    AppHelper.callAfter(sheet.update, f"{name}{of}: {files:,} files, "
                                                      f"{fmt_size(nbytes)} so far")
                out.append((i, restic.restore_size(s, n, progress=prog)))
            return out

        def finish():
            state["running"] = False
            sheet.close()
            self.jobFinished("")

        def done(results):
            finish()
            for i, nbytes in results:
                if i < len(extras):
                    extras[i]["dirsize"] = nbytes
            if gen == self.historyGeneration:
                self.refreshCells()
            if len(results) == 1:
                self.status.setStringValue_(
                    f"“{rows[results[0][0]][1].name}” in the backup: {fmt_size(results[0][1])}")
            else:
                self.status.setStringValue_(
                    f"{len(results)} folders: {fmt_size(sum(b for _i, b in results))} in total")

        def fail(e):
            finish()
            self.status.setStringValue_("" if isinstance(e, Cancelled) else f"Size failed: {e}")
        self.jobStarted("Calculating size …")
        AppHelper.callLater(0.3, lambda: state["running"] and sheet.show(
            "restic is reading the folder's file list from the backup."))
        run_async(work, done, fail)

    def calculateLocalSize_(self, sender):
        """Size of the selected folders on this Mac (like Finder's Get Info)."""
        items = [it for it in self.selectedOutlineItems() if it.is_dir]
        if not items:
            return
        state = {"running": True, "stopped": False}
        sheet = ProgressSheet.alloc().init().setup(
            self.window, "Calculating size on this Mac",
            on_cancel=lambda: state.__setitem__("stopped", True))

        def work():
            out = []
            for it in items:
                def prog(files, nbytes, name=it.name):
                    AppHelper.callAfter(sheet.update, f"{name}: {files:,} files, "
                                                      f"{fmt_size(nbytes)} so far")
                out.append((it, local_size(it.path, progress=prog,
                                           stop=lambda: state["stopped"])))
            return out

        def finish():
            state["running"] = False
            sheet.close()

        def done(results):
            finish()
            for it, nbytes in results:
                it.localSize = nbytes
                self.outline.reloadItem_(it)
            self.status.setStringValue_(
                f"“{results[0][0].name}” on this Mac: {fmt_size(results[0][1])}" if len(results) == 1
                else f"{len(results)} folders: {fmt_size(sum(b for _i, b in results))}")

        def fail(e):
            finish()
        AppHelper.callLater(0.3, lambda: state["running"] and sheet.show("Adding up file sizes …"))
        run_async(work, done, fail)

    def openLocal_(self, sender):
        for it in self.selectedOutlineItems():
            open_with(it.path)

    def openLocalWith_(self, sender):
        for it in self.selectedOutlineItems():
            if not it.is_dir:
                open_with(it.path, sender.representedObject())

    def revealLocal_(self, sender):
        urls = [NSURL.fileURLWithPath_(it.path) for it in self.selectedOutlineItems()
                if os.path.lexists(it.path)]
        NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(urls)

    def revealOnDisk_(self, sender):
        """Right pane: select the items' current copies (on this Mac) in Finder."""
        ws, paths = NSWorkspace.sharedWorkspace(), [n.path for _s, n, _c in self.selectedRows()]
        urls = [NSURL.fileURLWithPath_(p) for p in paths if os.path.lexists(p)]
        if urls:
            ws.activateFileViewerSelectingURLs_(urls)
        elif paths:                               # none left on this Mac: open the nearest
            ws.openURL_(NSURL.fileURLWithPath_(existing_dir(paths[0])))   # folder above

    def copyPath_(self, sender):
        if sender.representedObject() == "left":
            paths = [it.path for it in self.selectedOutlineItems()]
        else:
            paths = [n.path for _, n, _ in self.selectedRows()]
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_("\n".join(paths), NSPasteboardTypeString)

    def toggleHiddenFiles_(self, sender):
        self.showHidden = not self.showHidden
        NSUserDefaults.standardUserDefaults().setBool_forKey_(self.showHidden, "ShowHiddenFiles")
        self.outlineSource.resortAll()
        if self.stack:                       # right pane shows a folder listing
            self.showBrowseTop()

    def toggleFoldersOnTop_(self, sender):
        self.foldersFirst = not self.foldersFirst
        NSUserDefaults.standardUserDefaults().setBool_forKey_(self.foldersFirst, "FoldersOnTop")
        self.outlineSource.resortAll()

    def validateMenuItem_(self, item):
        action = item.action()
        action = action.decode() if isinstance(action, bytes) else str(action)
        if action == "toggleFoldersOnTop:":
            item.setState_(1 if self.foldersFirst else 0)
        elif action == "toggleHiddenFiles:":
            item.setState_(1 if self.showHidden else 0)
        return True

    def revealCached_(self, sender):
        """Fetch the selected files into the local cache and reveal them in Finder."""
        sel = [(s, n) for s, n, _ in self.selectedRows() if not n.is_dir]
        if not sel:
            alert("Show in Finder works for files", "Use “Restore to…” for folders.")
            return
        restic = self.restic

        def done(paths):
            self.jobFinished("")
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(
                [NSURL.fileURLWithPath_(p) for p in paths])

        def fail(e):
            self.jobFinished("Fetch failed")
            alert("Fetch failed", str(e))
        self.jobStarted("Fetching …")
        run_async(lambda: [restic.materialize(s, n) for s, n in sel], done, fail)

    # ------------------------------------------------------------ restore --
    def restoreTo_(self, sender):
        sel = self.selectedRows()
        if not sel:
            return
        folder = self.chooseRestoreFolder(sel)
        if folder:
            self.doRestore(sel, lambda node: folder)

    @objc.python_method
    def chooseRestoreFolder(self, sel):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(True)
        panel.setCanChooseFiles_(False)
        panel.setCanCreateDirectories_(True)
        panel.setPrompt_("Restore Here")
        panel.setMessage_(f"Choose where to restore {len(sel)} item(s)")
        start = restore_start_dir([n.path for _, n, _ in sel])
        panel.setDirectoryURL_(NSURL.fileURLWithPath_isDirectory_(start, True))
        if panel.runModal() != 1:
            return None
        return panel.URL().path()

    @objc.python_method
    def restoreStarted(self):
        """Keep the Mac from idle-sleeping while a restore runs (a closed lid still sleeps)."""
        self.restoresRunning += 1
        if self._sleepActivity is None:
            from Foundation import NSProcessInfo
            self._sleepActivity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                0x00FFFFFF, "Restoring files from a restic backup")   # NSActivityUserInitiated

    @objc.python_method
    def restoreEnded(self):
        self.restoresRunning = max(0, self.restoresRunning - 1)
        self.outlineSource.refreshOpen(force=True)     # show what was restored on the left
        if self.restoresRunning == 0 and self._sleepActivity is not None:
            from Foundation import NSProcessInfo
            NSProcessInfo.processInfo().endActivity_(self._sleepActivity)
            self._sleepActivity = None

    @objc.python_method
    def shouldQuit(self):
        """Called before quitting: a running restore needs confirmation, then clean-up."""
        if self.restoresRunning == 0:
            return True
        a = NSAlert.alloc().init()
        a.setMessageText_("A restore is still running")
        a.setInformativeText_("If you quit now, the restore is stopped and the partly restored "
                              "copy is removed. Your current files are not changed.")
        a.addButtonWithTitle_("Keep Restoring")
        a.addButtonWithTitle_("Stop and Quit")
        if a.runModal() == 1000:
            return False
        self.stopEverything()
        return True

    @objc.python_method
    def stopEverything(self):
        """Stop all restic processes and clean up partial restores (on quit)."""
        import time as _time
        if self.restic is not None:
            self.restic.close()                        # kills every restic child process
            deadline = _time.monotonic() + 3
            while self.restic.busy() and _time.monotonic() < deadline:
                _time.sleep(0.05)
        self.journal.recover()

    @objc.python_method
    def askRestoreConflict(self, clashes):
        """Ask once what to do with items that already exist. Returns a mode or None."""
        a = NSAlert.alloc().init()
        if len(clashes) == 1:
            name = os.path.basename(clashes[0])
            where = os.path.basename(os.path.dirname(clashes[0])) or "/"
            a.setMessageText_(f"“{name}” already exists in “{where}”")
        else:
            a.setMessageText_(f"{len(clashes)} items already exist where you are restoring")
        a.setInformativeText_(
            "Replace moves the current version to the Trash.\n"
            "Keep Both renames the current version to “… (before restore <date>)”.\n"
            "Restore with New Name leaves the current version as it is.")
        a.addButtonWithTitle_("Replace")                       # 1000
        a.addButtonWithTitle_("Keep Both")                     # 1001
        a.addButtonWithTitle_("Restore with New Name")         # 1002
        a.addButtonWithTitle_("Cancel")                        # 1003: Return and Esc
        a.buttons()[0].setKeyEquivalent_("")
        a.buttons()[3].setKeyEquivalent_("\r")
        return {1000: "replace", 1001: "rename-current", 1002: "rename"}.get(a.runModal())

    def restoreNextToOriginal_(self, sender):
        sel = self.selectedRows()
        missing = sorted({posixpath.dirname(n.path) for _, n, _ in sel
                          if not os.path.isdir(posixpath.dirname(n.path))})
        if missing:
            alert("Original folder not found on this Mac",
                  "\n".join(missing) + "\n\nUse “Restore to…” instead.")
            return
        # if the name is taken, doRestore asks: replace / rename current / new name
        self.doRestore(sel, lambda node: posixpath.dirname(node.path))

    @objc.python_method
    def doRestore(self, sel, target_for):
        """Open the live restore dialog: options + running size check, then act."""
        jobs = [(s, n, target_for(n)) for s, n, _ in sel]
        clashes = set(restore_conflicts([n for _, n, _ in jobs], target_for))
        self.restoreFlow = RestoreFlow(self, sel, jobs, clashes)
        self.restoreFlow.start()

    @objc.python_method
    def restoreElsewhere(self, sel):
        folder = self.chooseRestoreFolder(sel)
        if folder:
            self.doRestore(sel, lambda node: folder)

    @objc.python_method
    def confirmInPlace(self, names, items, nbytes, delete=False):
        a = NSAlert.alloc().init()
        a.setAlertStyle_(2)
        target = ", ".join(f"“{n}”" for n in names)
        a.setMessageText_(f"Make {target} exactly like the backup?" if delete
                          else f"Update {target} in place?")
        what = (f"{items:,} items ({fmt_size(nbytes)})" if nbytes is not None
                else "Files that differ from the backup")
        text = f"{what} will be overwritten with their backup versions."
        if delete:
            text += (" Files that are not in the backup — including everything added since — "
                     "will be deleted permanently (not moved to the Trash).")
        else:
            text += " Files that aren't in the backup are left alone."
        text += ("\n\nThis changes the folder directly and can't be undone. If it's interrupted, "
                 "run it again to finish. Quit apps that use these files first.")
        a.setInformativeText_(text)
        a.addButtonWithTitle_("Delete and Restore" if delete else "Update in Place")
        a.addButtonWithTitle_("Cancel")
        a.buttons()[0].setKeyEquivalent_("")          # no accidental Return
        a.buttons()[1].setKeyEquivalent_("\r")
        return a.runModal() == 1000

    @objc.python_method
    def startRestore(self, jobs, conflict, in_place_dests=()):
        restic, journal = self.restic, self.journal
        in_place = conflict in ("in-place", "in-place-delete")
        cancelled = [False]
        names = [n.name for _s, n, _t in jobs]
        title = f"Restoring “{names[0]}”" if len(names) == 1 else f"Restoring {len(names)} items"
        sheet = ProgressSheet.alloc().init().setup(self.window, title,
                                                   on_cancel=lambda: stop())

        def stop():
            cancelled[0] = True
            restic.cancel("restore")

        def show(i, name, d=None):
            head = f"Item {i + 1} of {len(jobs)}: {name}\n" if len(jobs) > 1 else ""
            if d is None:
                sheet.update(head + "Starting restic …")
                return
            done_b, total_b = d.get("bytes_restored", 0), d.get("total_bytes", 0)
            done_f, total_f = d.get("files_restored", 0), d.get("total_files", 0)
            eta = d.get("seconds_remaining")
            left = f" · about {fmt_duration(eta)} left" if eta else ""
            sheet.update(f"{head}{fmt_size(done_b)} of {fmt_size(total_b)} · "
                         f"{done_f:,} of {total_f:,} files{left}", d.get("percent_done"))

        def one(i, s, n, t):
            if cancelled[0]:
                raise Cancelled("stopped")
            AppHelper.callAfter(show, i, n.name)

            def prog(d):
                AppHelper.callAfter(show, i, n.name, d)
            dest = os.path.join(t, n.name)
            if in_place and dest in in_place_dests:
                return restic.restore_in_place(s, n, dest, progress=prog,
                                               delete=conflict == "in-place-delete")
            mode = "rename" if in_place else conflict
            return restic.restore(s, n, t, conflict=mode, trash=move_to_trash, journal=journal,
                                  progress=prog)

        def work():
            return [one(i, s, n, t) for i, (s, n, t) in enumerate(jobs)]

        self.restoreStarted()
        sheet.show("Starting restic …")

        def done(paths):
            sheet.close()
            self.restoreEnded()
            self.jobFinished(f"Restored {len(paths)} item(s)")
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(
                [NSURL.fileURLWithPath_(p) for p in paths])

        def fail(e):
            sheet.close()
            self.restoreEnded()
            if isinstance(e, Cancelled):
                self.jobFinished("Restore cancelled")
                if in_place:
                    alert("Restore stopped", "Some files may already have been updated in place. "
                          "Run Update in Place again to finish; files not in the backup were "
                          "not touched.")
                else:
                    alert("Restore cancelled", "The partial copy was removed. Your current "
                          "files were not changed.")
                return
            self.jobFinished("Restore failed")
            if in_place:
                alert("Restore failed", f"{e}\n\nSome files may already have been updated in "
                      "place; running Update in Place again finishes the job.")
            else:
                alert("Restore failed", f"{e}\n\nNothing was changed: the partial restore was "
                      "removed and your current files are untouched.")
        self.jobStarted(f"Restoring {len(jobs)} item(s) …")
        run_async(work, done, fail)

    # ----------------------------------------------------------- Backrest --
    # ------------------------------------------- links tab (next to Backrest) --
    @objc.python_method
    def openInLinksTab(self, url):
        """Show *url* in the tab to the right of Manage (Backrest), creating it once."""
        if getattr(self, "linksItem", None) is None:
            self.buildLinksTab()
        if self.linksView is None:                # no WebKit: fall back to the browser
            NSWorkspace.sharedWorkspace().openURL_(url)
            return
        self.linksView.loadRequest_(NSURLRequest.requestWithURL_(url))
        self.linksItem.setLabel_(url.host() or "Link")
        self.tabs.selectTabViewItem_(self.linksItem)

    @objc.python_method
    def buildLinksTab(self):
        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1000, 600))
        back = NSButton.buttonWithTitle_target_action_("‹", self, "linksBack:")
        fwd = NSButton.buttonWithTitle_target_action_("›", self, "linksForward:")
        reload_ = NSButton.buttonWithTitle_target_action_("Reload", self, "linksReload:")
        browser = NSButton.buttonWithTitle_target_action_("Open in Browser", self, "linksInBrowser:")
        close = NSButton.buttonWithTitle_target_action_("Close Tab", self, "linksClose:")
        self.linksAddress = label("")
        self.linksAddress.setTextColor_(NSColor.secondaryLabelColor())
        self.linksAddress.setContentCompressionResistancePriority_forOrientation_(1, 0)
        spacer = NSView.alloc().init()
        spacer.setContentHuggingPriority_forOrientation_(1, 0)
        bar = NSStackView.stackViewWithViews_([back, fwd, reload_, self.linksAddress, spacer,
                                               browser, close])
        holder = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1000, 560))
        try:
            from WebKit import WKWebView, WKWebViewConfiguration
            # the default configuration shares cookies (e.g. a Backrest login) with the
            # Backrest tab
            self.linksView = WKWebView.alloc().initWithFrame_configuration_(
                holder.bounds(), WKWebViewConfiguration.alloc().init())
            self.linksPolicy = WebPolicy.alloc().init().setup(self, links_tab=True)
            self.linksView.setNavigationDelegate_(self.linksPolicy)
            self.linksView.setUIDelegate_(self.linksPolicy)
            self.linksView.setAutoresizingMask_(WIDTH_HEIGHT)
            holder.addSubview_(self.linksView)
        except ImportError:
            self.linksView = None
        for v in (bar, holder):
            v.setTranslatesAutoresizingMaskIntoConstraints_(False)
            container.addSubview_(v)
        NSLayoutConstraint.activateConstraints_([
            bar.topAnchor().constraintEqualToAnchor_constant_(container.topAnchor(), 8),
            bar.leadingAnchor().constraintEqualToAnchor_constant_(container.leadingAnchor(), 8),
            bar.trailingAnchor().constraintEqualToAnchor_constant_(container.trailingAnchor(), -8),
            holder.topAnchor().constraintEqualToAnchor_constant_(bar.bottomAnchor(), 8),
            holder.leadingAnchor().constraintEqualToAnchor_(container.leadingAnchor()),
            holder.trailingAnchor().constraintEqualToAnchor_(container.trailingAnchor()),
            holder.bottomAnchor().constraintEqualToAnchor_(container.bottomAnchor()),
        ])
        item = NSTabViewItem.alloc().initWithIdentifier_("links")
        item.setLabel_("Link")
        item.setView_(container)
        self.tabs.addTabViewItem_(item)
        self.linksItem = item

    @objc.python_method
    def linksPageChanged(self, view):
        """A page finished loading in the links tab: its title becomes the tab's label."""
        if view is not getattr(self, "linksView", None) or self.linksItem is None:
            return
        url = view.URL()
        title = (view.title() or "") or (url.host() if url else "") or "Link"
        self.linksItem.setLabel_(title if len(title) <= 32 else title[:31] + "…")
        self.linksAddress.setStringValue_(url.absoluteString() if url else "")

    def linksBack_(self, sender):
        if self.linksView is not None and self.linksView.canGoBack():
            self.linksView.goBack()

    def linksForward_(self, sender):
        if self.linksView is not None and self.linksView.canGoForward():
            self.linksView.goForward()

    def linksReload_(self, sender):
        if self.linksView is not None:
            self.linksView.reload()

    def linksInBrowser_(self, sender):
        url = self.linksView.URL() if self.linksView is not None else None
        if url is not None:
            NSWorkspace.sharedWorkspace().openURL_(url)

    def linksClose_(self, sender):
        if getattr(self, "linksItem", None) is None:
            return
        if self.linksView is not None:
            self.linksView.stopLoading()
            self.linksView.loadHTMLString_baseURL_("", None)
        self.tabs.selectTabViewItemWithIdentifier_("manage")
        self.tabs.removeTabViewItem_(self.linksItem)
        self.linksItem = None
        self.linksView = None

    def tabView_didSelectTabViewItem_(self, tabs, item):
        if item.identifier() == "manage" and not self.webLoaded:
            self.backrestConnect_(None)

    @objc.python_method
    def showBackrestMessage(self, title, text):
        if self.webView is not None:
            self.webView.setHidden_(True)
        self.brOverlay.setHidden_(False)
        self.brTitle.setStringValue_(title)
        self.brText.setStringValue_(text)

    @objc.python_method
    def showBackrestPage(self, url):
        self.backrestBase = url                   # links elsewhere go to the links tab
        self.brOverlay.setHidden_(True)
        if self.webView is not None:
            self.webView.setHidden_(False)
            self.webView.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        self.webLoaded = True

    def backrestConnect_(self, sender):
        """Check the address first; load Backrest only if it really answers there."""
        url = br.normalize_url(self.brField.stringValue())
        self.brField.setStringValue_(url)
        if url != self.config.backrest_url:
            self.config.backrest_url = url
            self.config.save()
        self.brStartBtn.setHidden_(True)
        self.brStatus.setStringValue_(f"Checking {url} …")
        if not self.webLoaded:
            self.showBackrestMessage("Connecting to Backrest …", url)
        local = br.is_local(url)

        def work():
            p = br.probe(url)
            if p.is_backrest or not local:
                return p, [], None
            return p, br.running_urls(), br.installation()   # look around on this Mac

        def done(result):
            p, running, inst = result
            if p.is_backrest:
                self.brStatus.setStringValue_(f"✓ Connected to Backrest at {url}")
                self.showBackrestPage(url)
                return
            self.webLoaded = False
            other = [u for u in running if u != url]
            if other:                                 # it runs, just on another port
                self.brField.setStringValue_(other[0])
                self.brStatus.setStringValue_(f"Found Backrest on this Mac at {other[0]}")
                self.backrestConnect_(None)
                return
            self.brStatus.setStringValue_(f"✗ {url}: {p.message}")
            if p.reachable:
                self.showBackrestMessage(
                    "That address isn't Backrest",
                    f"{p.message}. Check the port — Backrest's default is 9898 — or use "
                    "Find on This Mac.")
            elif local and inst is not None:
                self.brStartBtn.setHidden_(False)
                how = {"launchd": "as a background service", "brew": "with Homebrew",
                       "binary": f"at {inst.detail}"}.get(inst.method, "")
                where = f" It is set up to listen at {inst.url}." if inst.url and inst.url != url else ""
                self.showBackrestMessage(
                    "Backrest isn't running on this Mac",
                    f"It is installed {how}.{where} Click Start Backrest to start it; it keeps "
                    "running when you quit Restic Control.")
            elif local:
                self.showBackrestMessage(
                    "Backrest isn't installed on this Mac",
                    "Install it from github.com/garethgeorge/backrest (or with Homebrew), or "
                    "enter the address of a Backrest server on another machine above.")
            else:
                self.showBackrestMessage(
                    "Can't reach Backrest",
                    f"{p.message}. Check that the server is on, the address and port are right, "
                    "and that Backrest listens on the network there — by default it only "
                    "accepts connections from its own machine (127.0.0.1). On that server set "
                    "BACKREST_PORT=0.0.0.0:9898 or --bind-address :9898, ideally only on a "
                    "trusted network or VPN.")

        def fail(e):
            self.brStatus.setStringValue_(f"✗ {e}")
        run_async(work, done, fail)

    def backrestDetect_(self, sender):
        self.brStatus.setStringValue_("Looking for Backrest on this Mac …")

        def done(result):
            running, inst = result
            if running:
                self.brField.setStringValue_(running[0])
                self.backrestConnect_(None)
            elif inst is not None:
                if inst.url:
                    self.brField.setStringValue_(inst.url)
                self.brStartBtn.setHidden_(False)
                self.brStatus.setStringValue_("Backrest is installed on this Mac but not running")
            else:
                self.brStatus.setStringValue_("No Backrest found on this Mac")
        run_async(lambda: (br.running_urls(), br.installation()), done,
                  lambda e: self.brStatus.setStringValue_(f"✗ {e}"))

    def backrestStart_(self, sender):
        field_url = br.normalize_url(self.brField.stringValue())
        self.brStartBtn.setEnabled_(False)
        self.brStatus.setStringValue_("Starting Backrest …")

        def work():
            inst = br.installation()
            if inst is None:
                raise br.BackrestError("Backrest isn't installed on this Mac")
            what = br.start_local(inst)
            target = inst.url or field_url
            probe = br.wait_until_up(target, seconds=20)
            if not probe.is_backrest:                          # maybe a different port
                running = br.running_urls()
                if running:
                    target = running[0]
                    probe = br.probe(target)
            return what, target, probe

        def done(result):
            what, target, probe = result
            self.brStartBtn.setEnabled_(True)
            self.brField.setStringValue_(target)
            if probe.is_backrest:
                self.brStatus.setStringValue_(what)
                self.backrestConnect_(None)
            else:
                self.brStatus.setStringValue_(f"{what}, but it doesn't answer yet at {target}")

        def fail(e):
            self.brStartBtn.setEnabled_(True)
            self.brStatus.setStringValue_(f"✗ Could not start Backrest: {e}")
        run_async(work, done, fail)

    def openBackrestInBrowser_(self, sender):
        url = br.normalize_url(self.brField.stringValue()) if hasattr(self, "brField") \
            else self.config.backrest_url
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))

    def clearCache_(self, sender):
        if self.restic:
            self.restic.clear_cache()
            self.resetTree()


# ============================================================================
# Restore dialog: options + live size check
# ============================================================================

def _need(nbytes):
    """Bytes needed including the safety margin used everywhere (2 %, at least 100 MB)."""
    return nbytes + max(int(nbytes * 0.02), 100 * 1024 * 1024)


class RestoreSheet(NSObject):
    """The restore dialog: what to keep, a live size line, Choose Another Location / Cancel / Restore."""

    @objc.python_method
    def setup(self, flow, window, clash, folder, in_place_ok):
        self.flow, self.window = flow, window
        self.clash, self.folder, self.in_place_ok = clash, folder, in_place_ok
        self.userTouched = False
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 560, 300), 1, 2, False)
        small = NSFont.systemFontOfSize_(NSFont.smallSystemFontSize())
        self.title = NSTextField.wrappingLabelWithString_("")
        self.title.setFont_(NSFont.boldSystemFontOfSize_(14))
        self.title.setPreferredMaxLayoutWidth_(520)
        self.live = NSTextField.wrappingLabelWithString_("")
        self.live.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(
            NSFont.smallSystemFontSize(), 0))
        self.live.setTextColor_(NSColor.secondaryLabelColor())
        self.live.setPreferredMaxLayoutWidth_(520)
        self.bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, 0, 520, 10))
        self.bar.setControlSize_(1)
        self.bar.setIndeterminate_(True)
        views = [self.title, self.live, self.bar]

        def radio(title):
            return NSButton.radioButtonWithTitle_target_action_(title, self, "changed:")

        def verdict():
            v = label("")
            v.setFont_(small)
            return v

        def sub(items):
            s = NSStackView.stackViewWithViews_(items)
            s.setOrientation_(1)
            s.setAlignment_(1)
            s.setSpacing_(4)
            s.setEdgeInsets_((0, 22, 0, 0))
            return s
        self.rKeep = self.rOnly = self.rKeepRename = self.rKeepNew = None
        self.rUpdate = self.rExact = self.cSafe = None
        if clash:
            self.rKeep = radio("Keep both")
            self.rKeepRename = radio("Rename the current one — the restored copy gets the name")
            self.rKeepNew = radio("Give the restored copy a new name")
            self.keepVerdict = verdict()
            self.rOnly = radio("Keep only the restored version")
            only_items = []
            if folder:
                self.rUpdate = radio("Update only what changed — keeps files added since the backup")
                self.rExact = radio("Make it exactly like the backup — removes files added since")
                self.cSafe = NSButton.checkboxWithTitle_target_action_(
                    "Safe: finish restoring before removing anything (needs room for both)",
                    self, "changed:")
                safe_row = sub([self.cSafe])
                only_items = [self.rUpdate, self.rExact, safe_row]
            self.onlyVerdict = verdict()
            self.rKeep.setState_(1)
            self.rKeepRename.setState_(1)
            if folder:
                (self.rUpdate if in_place_ok else self.rExact).setState_(1)
                self.cSafe.setState_(1)
            views += [self.rKeep, sub([self.rKeepRename, self.rKeepNew, self.keepVerdict]),
                      self.rOnly, sub(only_items + [self.onlyVerdict])]
        self.note = NSTextField.wrappingLabelWithString_("")
        self.note.setFont_(small)
        self.note.setPreferredMaxLayoutWidth_(520)
        views.append(self.note)
        elsewhere = NSButton.buttonWithTitle_target_action_("Choose Another Location…", self,
                                                            "elsewhere:")
        cancel = NSButton.buttonWithTitle_target_action_("Cancel", self, "cancel:")
        cancel.setKeyEquivalent_("\x1b")
        self.restoreBtn = NSButton.buttonWithTitle_target_action_("Restore", self, "restore:")
        spacer = NSView.alloc().init()
        spacer.setContentHuggingPriority_forOrientation_(1, 0)
        row = NSStackView.stackViewWithViews_([elsewhere, spacer, cancel, self.restoreBtn])
        views.append(row)
        stack = NSStackView.stackViewWithViews_(views)
        stack.setOrientation_(1)
        stack.setAlignment_(1)
        stack.setSpacing_(8)
        stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        content = self.panel.contentView()
        content.addSubview_(stack)
        NSLayoutConstraint.activateConstraints_([
            stack.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 18),
            stack.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 20),
            stack.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -20),
            stack.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -16),
            self.bar.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()),
            row.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()),
        ])
        return self

    # -- state ----------------------------------------------------------------
    @objc.python_method
    def mode(self):
        """new | rename-current | rename | replace | update | exact-unsafe"""
        if not self.clash:
            return "new"
        if self.rKeep.state():
            return "rename-current" if self.rKeepRename.state() else "rename"
        if not self.folder:
            return "replace"
        if self.rUpdate.state():
            return "update"
        return "replace" if self.cSafe.state() else "exact-unsafe"

    @objc.python_method
    def select_only(self):
        self.rOnly.setState_(1)
        self.rKeep.setState_(0)

    # -- actions ----------------------------------------------------------------
    def changed_(self, sender):
        self.userTouched = True
        self.flow.render()

    def restore_(self, sender):
        self.close()
        self.flow.chosen(self.mode())

    def elsewhere_(self, sender):
        self.close()
        self.flow.chosen("elsewhere")

    def cancel_(self, sender):
        self.close()
        self.flow.chosen(None)

    @objc.python_method
    def show(self):
        self.bar.startAnimation_(None)
        self.window.beginSheet_completionHandler_(self.panel, None)

    @objc.python_method
    def close(self):
        self.bar.stopAnimation_(None)
        self.window.endSheet_(self.panel)
        self.panel.orderOut_(None)


class RestoreFlow:
    """One restore request: the dialog opens at once; a single measuring pass keeps its
    size line and verdicts current.  Totals only grow, so "doesn't fit" is final the
    moment the running total passes the free space."""

    def __init__(self, main, sel, jobs, clashes):
        self.main, self.sel, self.jobs, self.clashes = main, sel, jobs, clashes
        self.restic = main.restic
        self.target = jobs[0][2]
        self.avail = volume_free(self.target)
        self.volume = volume_name(self.target)
        n = len(jobs)
        self.size, self.final = [0] * n, [False] * n
        self.diff, self.diff_final, self.items = [None] * n, [False] * n, [0] * n
        self.pct, self.approx, self.error = None, False, None
        self.decided = self.measuring = False
        self.clash_idx = [i for i, (s, nd, t) in enumerate(jobs)
                          if os.path.join(t, nd.name) in clashes]
        folder = bool(self.clash_idx) and all(jobs[i][1].is_dir for i in self.clash_idx)
        self.in_place_ok = folder and all(
            os.path.isdir(os.path.join(jobs[i][2], jobs[i][1].name)) for i in self.clash_idx) \
            and self.restic.restic_version() >= (0, 17, 0)
        self.sheet = RestoreSheet.alloc().init().setup(self, main.window, bool(self.clash_idx),
                                                       folder, self.in_place_ok)

    # -- measuring -------------------------------------------------------------
    def start(self):
        known = [self.restic.cached_size(s, nd) for s, nd, t in self.jobs]   # no restic call
        bounds = [k if k is not None else (s.total_bytes if nd.is_dir else (nd.size or 0))
                  for (s, nd, t), k in zip(self.jobs, known)]
        for i, k in enumerate(known):
            if k is not None:                  # measured before / backup root / file
                self.size[i], self.final[i] = k, True
        if all(b is not None for b in bounds) and _need(sum(bounds)) <= self.avail:
            for i, ((s, nd, t), b) in enumerate(zip(self.jobs, bounds)):   # proven to fit
                self.size[i], self.final[i] = b, True
                self.approx = self.approx or known[i] is None
            self.render()
            self.sheet.show()
            return
        self.measuring = True
        self.render()
        self.sheet.show()
        run_async(self.measure, self.measured, self.failed)

    def copy_short(self):
        return _need(sum(self.size)) > self.avail

    def diff_bytes(self):
        return sum((self.diff[i] or 0) if i in self.clash_idx else self.size[i]
                   for i in range(len(self.jobs)))

    def diff_short(self):
        return _need(self.diff_bytes()) > self.avail

    def measure(self):                                       # worker thread
        for i, (s, nd, t) in enumerate(self.jobs):
            if self.decided:
                raise Cancelled("decided")
            dest = os.path.join(t, nd.name)
            if self.in_place_ok and i in self.clash_idx:
                def status(d, i=i):
                    restored, skipped = d.get("bytes_restored", 0), d.get("bytes_skipped", 0)
                    AppHelper.callAfter(self.update, i, restored + skipped, restored,
                                        d.get("percent_done"))
                    if self.copy_short() and self.diff_short():
                        self.restic.cancel("measure-inplace")      # nothing can fit
                try:
                    plan = self.restic.in_place_plan(s, nd, dest, progress=status)
                    AppHelper.callAfter(self.done_job, i, plan["total"], plan["bytes"], plan["files"])
                except Cancelled:
                    if self.decided:
                        raise
            elif self.final[i]:
                continue                               # size known before we started
            elif not nd.is_dir:
                AppHelper.callAfter(self.done_job, i, nd.size or 0, None)
            elif self.copy_short() and not self.in_place_ok:
                continue
            else:
                def counted(files, nbytes, i=i):
                    AppHelper.callAfter(self.update, i, nbytes, None, None)
                    if self.copy_short() and not self.in_place_ok:
                        self.restic.cancel("measure")          # the answer is already known
                try:
                    total = self.restic.restore_size(s, nd, progress=counted)
                    AppHelper.callAfter(self.done_job, i, total, None)
                except Cancelled:
                    if self.decided:
                        raise
        return True

    def update(self, i, nbytes, diff, pct):
        if self.decided:
            return
        if not self.final[i]:
            self.size[i] = max(self.size[i], nbytes)
        if diff is not None:
            self.diff[i] = max(self.diff[i] or 0, diff)
        self.pct = pct
        self.render()

    def done_job(self, i, nbytes, diff, items=0):
        if self.decided:
            return
        self.size[i], self.final[i] = nbytes, True
        if diff is not None:
            self.diff[i], self.diff_final[i], self.items[i] = diff, True, items
        self.render()

    def measured(self, _result):
        self.measuring = False
        if not self.decided:
            self.render()

    def failed(self, e):
        self.measuring = False
        if self.decided or isinstance(e, Cancelled):
            if not self.decided:
                self.render()
            return
        self.error = str(e)
        self.render()

    # -- dialog -------------------------------------------------------------------
    def verdict(self, nbytes, known, short):
        """'✓ needs 55.7 MB + 100 MB safety margin = 155.7 MB · 256.8 MB available'"""
        fs = fmt_size
        if self.error:
            return f"size unknown · {fs(self.avail)} available"
        if not nbytes and not known:
            return f"calculating … · {fs(self.avail)} available"
        total = _need(nbytes)
        sum_text = (f"{'at most ' if self.approx and known else ''}{fs(nbytes)} + "
                    f"{fs(total - nbytes)} safety margin = {fs(total)}")
        mark = "✗ " if short else ("✓ " if known else "")
        tail = "" if known or short else " so far"
        return f"{mark}needs {sum_text}{tail} · {fs(self.avail)} available"

    def render(self):
        sh, fs = self.sheet, fmt_size
        full, diff = sum(self.size), self.diff_bytes()
        full_known = all(self.final)
        diff_known = (not self.measuring) or all(self.diff_final[i] for i in self.clash_idx)
        copy_short, diff_short = self.copy_short(), self.in_place_ok and self.diff_short()

        # title + live line
        if len(self.clashes) == 1:
            p = next(iter(self.clashes))
            title = (f"“{os.path.basename(p)}” already exists in "
                     f"“{os.path.basename(os.path.dirname(p)) or '/'}”")
        elif self.clashes:
            title = f"{len(self.clashes)} items already exist where you are restoring"
        else:
            names = [nd.name for _s, nd, _t in self.jobs]
            what = f"“{names[0]}”" if len(names) == 1 else f"{len(names)} items"
            title = f"Restore {what} to “{os.path.basename(self.target) or self.target}”"
        if self.error:
            live = f"Couldn't calculate the size: {self.error}"
        else:
            live = ("Restore size: calculating" if not full and not full_known else
                    f"Restore size {'≤ ' if self.approx else ''}{fs(full)}"
                    f"{'' if full_known else ' so far'}")
        live += f"  ·  {fs(self.avail)} free on “{self.volume}”"
        if self.in_place_ok and any(self.diff[i] is not None for i in self.clash_idx):
            live += (f"  ·  differs from yours: {fs(sum(self.diff[i] or 0 for i in self.clash_idx))}"
                     f"{'' if diff_known else ' so far'}")
        if self.measuring:
            live += "  ·  calculating …"
        sh.title.setStringValue_(title)
        sh.live.setStringValue_(live)
        sh.bar.setHidden_(not self.measuring)
        if self.measuring and self.pct is not None:
            if sh.bar.isIndeterminate():
                sh.bar.setIndeterminate_(False)
                sh.bar.setMinValue_(0.0)
                sh.bar.setMaxValue_(1.0)
            sh.bar.setDoubleValue_(float(self.pct))

        # options
        note = ""
        if sh.clash:
            if copy_short and not sh.userTouched and sh.rKeep.state():
                sh.select_only()                       # keeping both can't work: suggest "only"
                if sh.folder and self.in_place_ok:
                    sh.rUpdate.setState_(1)
                    sh.rExact.setState_(0)
            keep = bool(sh.rKeep.state())
            sh.rKeepRename.setEnabled_(keep)
            sh.rKeepNew.setEnabled_(keep)
            sh.keepVerdict.setStringValue_(self.verdict(full, full_known, copy_short))
            if sh.folder:
                sh.rUpdate.setEnabled_(not keep and self.in_place_ok)
                sh.rExact.setEnabled_(not keep)
                if not self.in_place_ok:
                    sh.rUpdate.setToolTip_("Needs restic 0.17 or newer")
                exact = bool(sh.rExact.state())
                if copy_short:                         # no room for both: "safe" is impossible
                    sh.cSafe.setState_(0)
                sh.cSafe.setEnabled_(not keep and exact and self.in_place_ok and not copy_short)
                if not self.in_place_ok:
                    sh.cSafe.setState_(1)              # without in-place, exact == safe copy
                sh.cSafe.setToolTip_("Not enough room for both copies" if copy_short else "")
            mode = sh.mode()
            diff_measured = any(self.diff[i] is not None for i in self.clash_idx)
            if mode in ("update", "exact-unsafe") and not diff_measured and not self.measuring:
                # not compared (size was already known to fit): the difference is at most all
                was = self.approx
                self.approx = True
                only = self.verdict(full, full_known, False)
                self.approx = was
            elif mode in ("update", "exact-unsafe"):
                only = self.verdict(diff, diff_known, diff_short)
            else:
                only = self.verdict(full, full_known, copy_short)
            sh.onlyVerdict.setStringValue_("" if keep else only)
            if copy_short and (not self.in_place_ok or diff_short):
                note = f"Nothing fits on “{self.volume}”. Choose another location."
        mode = sh.mode()
        blocked = (diff_short if mode in ("update", "exact-unsafe") else copy_short) \
            and not self.error
        sh.restoreBtn.setEnabled_(not blocked)
        if not sh.clash and copy_short:
            note = f"Not enough space on “{self.volume}”. Choose another location."
        sh.note.setStringValue_(note)

    def chosen(self, mode):
        self.decided = True
        self.restic.cancel("measure")
        self.restic.cancel("measure-inplace")
        main = self.main
        if mode is None:
            main.status.setStringValue_("Restore cancelled")
        elif mode == "elsewhere":
            main.restoreElsewhere(self.sel)
        elif mode in ("update", "exact-unsafe"):
            names = [self.jobs[i][1].name for i in self.clash_idx]
            known = all(self.diff_final[i] for i in self.clash_idx)
            nbytes = sum(self.diff[i] or 0 for i in self.clash_idx) if known else None
            items = sum(self.items[i] for i in self.clash_idx)
            delete = mode == "exact-unsafe"
            if main.confirmInPlace(names, items, nbytes, delete=delete):
                main.startRestore(self.jobs, "in-place-delete" if delete else "in-place",
                                  in_place_dests=self.clashes)
        else:
            main.startRestore(self.jobs, "rename" if mode == "new" else mode)


# ============================================================================
# Application delegate & menus
# ============================================================================

def paths_from_pasteboard(pboard):
    """File paths a Finder service hands us (file URLs, or the legacy filenames list)."""
    try:
        from Foundation import NSURL as _NSURL
        urls = pboard.readObjectsForClasses_options_(
            [_NSURL], {"NSPasteboardURLReadingFileURLsOnlyKey": True}) or []
        paths = [u.path() for u in urls if u.path()]
        if paths:
            return paths
    except Exception:  # noqa: BLE001
        pass
    names = pboard.propertyListForType_("NSFilenamesPboardType")
    return [str(n) for n in (names or [])]


class ServiceProvider(NSObject):
    """Handles Finder → Quick Actions / Services → “Check in Restic Control”
    (declared as NSServices in the app's Info.plist, see setup.py)."""

    @objc.typedSelector(b"v@:@@o^@")
    def checkInResticControl_userData_error_(self, pboard, userData, error):
        paths = paths_from_pasteboard(pboard)
        if paths:
            self.delegate.openPaths(paths)
        return None


class AppDelegate(NSObject):
    @objc.python_method
    def openPaths(self, paths):
        main = getattr(self, "main", None)
        if main is None:                             # launched by the service: not ready yet
            self.pendingPaths = list(paths)
            return
        main.revealPath(paths[0])
        if len(paths) > 1:
            main.status.setStringValue_(f"Showing the first of {len(paths)} items")

    def application_openFiles_(self, app, filenames):
        """Items dropped on the Dock icon (or `open -a "Restic Control" path`)."""
        self.openPaths([str(f) for f in filenames])
        app.replyToOpenOrPrint_(0)                   # NSApplicationDelegateReplySuccess

    def applicationDidFinishLaunching_(self, note):
        # outside an .app bundle (python main.py) macOS shows a generic icon: set ours
        icon = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "resources", "icon.png")
        if os.path.exists(icon) and not NSBundle.mainBundle().bundlePath().endswith(".app"):
            NSApp.setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(icon))
        self.main = MainController.alloc().init()
        buildMenus(self.main)
        self.main.start()
        self.services = ServiceProvider.alloc().init()
        self.services.delegate = self
        NSApp.setServicesProvider_(self.services)
        try:
            from AppKit import NSUpdateDynamicServices
            NSUpdateDynamicServices()
        except ImportError:
            pass
        NSApp.activateIgnoringOtherApps_(True)
        pending = getattr(self, "pendingPaths", None)
        if pending:
            self.pendingPaths = None
            self.openPaths(pending)

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    def applicationShouldTerminate_(self, app):
        main = getattr(self, "main", None)
        return 1 if main is None or main.shouldQuit() else 0    # NSTerminateNow / Cancel

    def applicationWillTerminate_(self, note):
        main = getattr(self, "main", None)
        if main is not None and main.restic is not None:
            main.restic.close()                       # no restic left running after we quit


def buildMenus(main):
    bar = NSMenu.alloc().init()

    def submenu(title, items):
        top = NSMenuItem.alloc().init()
        m = NSMenu.alloc().initWithTitle_(title)
        for it in items:
            if it is None:
                m.addItem_(NSMenuItem.separatorItem())
                continue
            t, action, key, target, mask = (it + (None, None))[:5]
            mi = m.addItemWithTitle_action_keyEquivalent_(t, action, key)
            if target is not None:
                mi.setTarget_(target)
            if mask:
                mi.setKeyEquivalentModifierMask_(mask)
        top.setSubmenu_(m)
        bar.addItem_(top)
        return m

    submenu("Restic Control", [
        ("About Restic Control", "orderFrontStandardAboutPanel:", ""),
        None,
        ("Settings…", "openSettings:", ",", main),
        None,
        ("Hide Restic Control", "hide:", "h"),
        ("Quit Restic Control", "terminate:", "q"),
    ])
    submenu("Edit", [
        ("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), None,
        ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ])
    submenu("View", [
        ("Quick Look", "toggleQuickLook:", "y", main),
        ("Keep Folders on Top", "toggleFoldersOnTop:", "", main),
        ("Show Hidden Files", "toggleHiddenFiles:", ".", main, (1 << 20) | (1 << 17)),   # ⌘⇧.
        ("Back", "goBack:", "[", main),
        None,
        ("Refresh Snapshots", "refresh:", "r", main),
        ("Clear Cache for Repository", "clearCache:", "", main),
        None,
        ("Open Backrest in Browser", "openBackrestInBrowser:", "", main),
    ])
    win = submenu("Window", [("Minimize", "performMiniaturize:", "m"), ("Close", "performClose:", "w")])
    NSApp.setMainMenu_(bar)
    NSApp.setWindowsMenu_(win)


def main():
    # Print the Python traceback of any error inside a Cocoa callback (otherwise a crash
    # shows only "SIGTRAP").  Started from the Dock there is no terminal, so keep a log.
    objc.setVerbose(True)
    if not sys.stderr.isatty():
        try:
            log_dir = os.path.expanduser("~/Library/Logs")
            os.makedirs(log_dir, exist_ok=True)
            log = open(os.path.join(log_dir, "Restic Control.log"), "a", buffering=1)
            sys.stdout = sys.stderr = log
            os.dup2(log.fileno(), 2)              # also native (Cocoa) messages
            print(f"--- started {datetime.now():%Y-%m-%d %H:%M:%S}", file=log)
        except OSError:
            pass
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()
