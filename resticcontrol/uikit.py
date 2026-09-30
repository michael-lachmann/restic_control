"""Reusable UI helpers: Quick Look placeholder items, column chooser, Open With, sorting."""
from __future__ import annotations

import html
import os
from datetime import datetime, timezone

import objc
from AppKit import (NSButton, NSFont, NSLayoutConstraint, NSMakeRect, NSMenu, NSMenuItem,
                    NSPanel, NSProgressIndicator, NSStackView, NSTextField, NSWorkspace)
from Foundation import NSObject, NSURL, NSUserDefaults

# ----------------------------------------------------------------------------
# Quick Look
# ----------------------------------------------------------------------------


class PreviewItem(NSObject):
    """QLPreviewItem with its own title, so a placeholder can carry the real file name."""

    @objc.python_method
    def setup(self, path, title):
        self._url = NSURL.fileURLWithPath_(path)
        self._title = title
        return self

    def previewItemURL(self):
        return self._url

    def previewItemTitle(self):
        return self._title


def preview_item(path, title=None):
    return PreviewItem.alloc().init().setup(path, title or os.path.basename(path))


PLACEHOLDER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px -apple-system, sans-serif; display: flex; height: 92vh; margin: 0;
        align-items: center; justify-content: center; text-align: center; color: #888; }}
 .name {{ font-size: 19px; font-weight: 600; color: CanvasText; margin: 10px 0 4px; }}
 .spin {{ width: 26px; height: 26px; border: 3px solid #8884; border-top-color: #888;
         border-radius: 50%; margin: 0 auto 14px; animation: s 0.9s linear infinite; }}
 @keyframes s {{ to {{ transform: rotate(360deg); }} }}
</style></head><body><div>
 <div class="spin"></div>
 <div>{verb} from the backup of {when}</div>
 <div class="name">{name}</div>
 <div>{detail}</div>
</div></body></html>"""


def placeholder_page(cache_dir, name, when, detail, verb="Fetching"):
    """Write (once) and return an HTML page for Quick Look to show while a file downloads."""
    d = os.path.join(cache_dir, "placeholders")
    os.makedirs(d, exist_ok=True)
    body = PLACEHOLDER_HTML.format(name=html.escape(name), when=html.escape(when),
                                   detail=html.escape(detail), verb=html.escape(verb))
    fn = os.path.join(d, f"{abs(hash(body)) & 0xFFFFFFFF:08x}.html")
    if not os.path.exists(fn):
        with open(fn, "w", encoding="utf-8") as f:
            f.write(body)
    return fn


# ----------------------------------------------------------------------------
# Column chooser (right-click on a table header)
# ----------------------------------------------------------------------------

class ColumnChooser(NSObject):
    """Header context menu with a checkmark per column; choice persisted in user defaults.

    The effective visibility is "chosen by the user" AND "allowed by the current mode".
    """

    @objc.python_method
    def setup(self, table, defaults_key, columns, fixed=("name", "icon"), default_hidden=()):
        self.table = table
        self.key = defaults_key
        self.columns = [(i, t) for i, t in columns if i not in fixed and t]
        saved = NSUserDefaults.standardUserDefaults().arrayForKey_(defaults_key)
        self.userHidden = set(saved) if saved is not None else set(default_hidden)
        self.modeHidden = set()
        menu = NSMenu.alloc().initWithTitle_("Columns")
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(self)
        table.headerView().setMenu_(menu)
        self.apply()
        return self

    def menuNeedsUpdate_(self, menu):
        menu.removeAllItems()
        for ident, title in self.columns:
            item = menu.addItemWithTitle_action_keyEquivalent_(title, "toggleColumn:", "")
            item.setTarget_(self)
            item.setRepresentedObject_(ident)
            item.setState_(0 if ident in self.userHidden else 1)
            item.setEnabled_(ident not in self.modeHidden)

    def toggleColumn_(self, sender):
        ident = sender.representedObject()
        self.userHidden ^= {ident}
        NSUserDefaults.standardUserDefaults().setObject_forKey_(sorted(self.userHidden), self.key)
        self.apply()

    @objc.python_method
    def setModeHidden(self, idents):
        self.modeHidden = set(idents)
        self.apply()

    @objc.python_method
    def apply(self):
        for col in self.table.tableColumns():
            ident = col.identifier()
            col.setHidden_(ident in self.userHidden or ident in self.modeHidden)


# ----------------------------------------------------------------------------
# Open With
# ----------------------------------------------------------------------------

def apps_for_extension(cache_dir, name):
    """Applications that can open files like *name*, default app first: [(title, app_path)]."""
    ext = os.path.splitext(name)[1]
    probe_dir = os.path.join(cache_dir, "probe")
    os.makedirs(probe_dir, exist_ok=True)
    probe = os.path.join(probe_dir, "probe" + ext)
    if not os.path.exists(probe):
        open(probe, "wb").close()
    url = NSURL.fileURLWithPath_(probe)
    ws = NSWorkspace.sharedWorkspace()
    default = ws.URLForApplicationToOpenURL_(url)
    urls = []
    if hasattr(ws, "URLsForApplicationsToOpenURL_"):          # macOS 12+
        urls = list(ws.URLsForApplicationsToOpenURL_(url) or [])
    elif default is not None:
        urls = [default]
    seen, out = set(), []
    for u in ([default] if default is not None else []) + urls:
        p = u.path()
        if p in seen:
            continue
        seen.add(p)
        title = os.path.splitext(os.path.basename(p))[0]
        out.append((title + (" (default)" if u is default else ""), p))
    return out


def open_with(path, app_path=None):
    ws = NSWorkspace.sharedWorkspace()
    if app_path is None:
        ws.openURL_(NSURL.fileURLWithPath_(path))
    else:
        ws.openFile_withApplication_(path, app_path)


def add_open_with_submenu(menu, target, action, apps):
    sub = NSMenu.alloc().initWithTitle_("Open With")
    for i, (title, app_path) in enumerate(apps):
        it = sub.addItemWithTitle_action_keyEquivalent_(title, action, "")
        it.setTarget_(target)
        it.setRepresentedObject_(app_path)
        it.setImage_(_app_icon(app_path))
        if i == 0 and len(apps) > 1:
            sub.addItem_(NSMenuItem.separatorItem())
    if not apps:
        sub.addItemWithTitle_action_keyEquivalent_("No applications", None, "").setEnabled_(False)
    parent = menu.addItemWithTitle_action_keyEquivalent_("Open With", None, "")
    menu.setSubmenu_forItem_(sub, parent)
    return parent


def _app_icon(path):
    img = NSWorkspace.sharedWorkspace().iconForFile_(path).copy()
    img.setSize_((16, 16))
    return img


# ----------------------------------------------------------------------------
# Sorting
# ----------------------------------------------------------------------------

_EPOCH = datetime.fromtimestamp(0, timezone.utc)


def kind_of(node):
    if node is None:
        return ""
    if node.is_dir:
        return "Folder"
    if node.type == "symlink":
        return "Alias"
    ext = os.path.splitext(node.name)[1].lstrip(".")
    return f"{ext.upper()} file" if ext else "Document"


def node_sort_key(node, key):
    if key == "mtime":
        return node.mtime or _EPOCH
    if key == "size":
        return -1 if node.size is None else node.size
    if key == "kind":
        return (kind_of(node).casefold(), node.name.casefold())
    return node.name.casefold()


def sort_nodes(nodes, key="name", ascending=True, folders_first=True, get=lambda x: x):
    """Finder-like sort; with *folders_first* folders stay on top whatever the direction."""
    items = sorted(nodes, key=lambda x: node_sort_key(get(x), key), reverse=not ascending)
    if folders_first:
        items.sort(key=lambda x: 0 if get(x).is_dir else 1)   # stable: keeps the order above
    return items


# ----------------------------------------------------------------------------
# Progress sheet
# ----------------------------------------------------------------------------

class ProgressSheet(NSObject):
    """A sheet on the main window: title, what is happening, a progress bar, Cancel.

        sheet = ProgressSheet.alloc().init().setup(window, "Restoring …", on_cancel)
        sheet.show(); sheet.update("12 % …", 0.12); sheet.close()
    """

    @objc.python_method
    def setup(self, window, title, on_cancel=None, cancel_title="Cancel"):
        self.window, self.on_cancel = window, on_cancel
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 480, 150), 1, 2, False)            # titled, buffered
        self.title = NSTextField.labelWithString_(title)
        self.title.setFont_(NSFont.boldSystemFontOfSize_(14))
        self.detail = NSTextField.wrappingLabelWithString_("")
        self.detail.setPreferredMaxLayoutWidth_(440)
        self.bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, 0, 440, 20))
        self.bar.setIndeterminate_(True)
        self.bar.setMinValue_(0.0)
        self.bar.setMaxValue_(1.0)
        self.cancel = NSButton.buttonWithTitle_target_action_(cancel_title, self, "cancelPressed:")
        self.cancel.setKeyEquivalent_("\x1b")
        stack = NSStackView.stackViewWithViews_([self.title, self.detail, self.bar, self.cancel])
        stack.setOrientation_(1)
        stack.setAlignment_(1)
        stack.setSpacing_(10)
        stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        content = self.panel.contentView()
        content.addSubview_(stack)
        NSLayoutConstraint.activateConstraints_([
            stack.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 18),
            stack.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 20),
            stack.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -20),
            stack.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -18),
            self.bar.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()),
            self.cancel.trailingAnchor().constraintEqualToAnchor_(stack.trailingAnchor()),
        ])
        self.shown = False
        return self

    @objc.python_method
    def show(self, detail=""):
        self.detail.setStringValue_(detail)
        self.bar.startAnimation_(None)
        self.window.beginSheet_completionHandler_(self.panel, None)
        self.shown = True

    @objc.python_method
    def update(self, detail=None, fraction=None, title=None):
        if not self.shown:
            return
        if title is not None:
            self.title.setStringValue_(title)
        if detail is not None:
            self.detail.setStringValue_(detail)
        if fraction is None:
            if not self.bar.isIndeterminate():
                self.bar.setIndeterminate_(True)
                self.bar.startAnimation_(None)
        else:
            if self.bar.isIndeterminate():
                self.bar.stopAnimation_(None)
                self.bar.setIndeterminate_(False)
            self.bar.setDoubleValue_(max(0.0, min(1.0, float(fraction))))

    @objc.python_method
    def close(self):
        if self.shown:
            self.shown = False
            self.bar.stopAnimation_(None)
            self.window.endSheet_(self.panel)
            self.panel.orderOut_(None)

    def cancelPressed_(self, sender):
        self.cancel.setEnabled_(False)
        self.detail.setStringValue_("Stopping …")
        if self.on_cancel:
            self.on_cancel()


# ----------------------------------------------------------------------------
# Live choice sheet (question + running measurement + buttons that can change)
# ----------------------------------------------------------------------------

class ChoiceSheet(NSObject):
    """A sheet with a title, explanatory text, a live status line with a progress bar,
    and a row of buttons that can be replaced at any time (set_buttons).

    on_choice(key) is called once with the key of the pressed button."""

    @objc.python_method
    def setup(self, window, on_choice):
        self.window, self.on_choice = window, on_choice
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 560, 260), 1, 2, False)
        self.title = NSTextField.wrappingLabelWithString_("")
        self.title.setFont_(NSFont.boldSystemFontOfSize_(14))
        self.title.setPreferredMaxLayoutWidth_(520)
        self.text = NSTextField.wrappingLabelWithString_("")
        self.text.setPreferredMaxLayoutWidth_(520)
        self.live = NSTextField.wrappingLabelWithString_("")
        self.live.setPreferredMaxLayoutWidth_(520)
        self.live.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(
            NSFont.smallSystemFontSize(), 0))
        self.bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, 0, 520, 12))
        self.bar.setIndeterminate_(True)
        self.bar.setControlSize_(1)
        self.buttonRow = NSStackView.stackViewWithViews_([])
        self.buttonRow.setSpacing_(8)
        self.stack = NSStackView.stackViewWithViews_(
            [self.title, self.text, self.live, self.bar, self.buttonRow])
        self.stack.setOrientation_(1)
        self.stack.setAlignment_(1)
        self.stack.setSpacing_(12)
        self.stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        content = self.panel.contentView()
        content.addSubview_(self.stack)
        NSLayoutConstraint.activateConstraints_([
            self.stack.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 18),
            self.stack.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 20),
            self.stack.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -20),
            self.stack.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -18),
            self.bar.widthAnchor().constraintEqualToAnchor_(self.stack.widthAnchor()),
            self.buttonRow.trailingAnchor().constraintEqualToAnchor_(self.stack.trailingAnchor()),
        ])
        self._layout = None
        self.shown = False
        return self

    @objc.python_method
    def show(self):
        self.bar.startAnimation_(None)
        self.window.beginSheet_completionHandler_(self.panel, None)
        self.shown = True

    @objc.python_method
    def set_text(self, title=None, text=None, live=None):
        if title is not None:
            self.title.setStringValue_(title)
        if text is not None:
            self.text.setStringValue_(text)
        if live is not None:
            self.live.setStringValue_(live)

    @objc.python_method
    def set_progress(self, fraction=None, busy=True):
        self.bar.setHidden_(not busy)
        if not busy:
            self.bar.stopAnimation_(None)
            return
        if fraction is None:
            if not self.bar.isIndeterminate():
                self.bar.setIndeterminate_(True)
                self.bar.startAnimation_(None)
        else:
            if self.bar.isIndeterminate():
                self.bar.stopAnimation_(None)
                self.bar.setIndeterminate_(False)
                self.bar.setMinValue_(0.0)
                self.bar.setMaxValue_(1.0)
            self.bar.setDoubleValue_(max(0.0, min(1.0, float(fraction))))

    @objc.python_method
    def set_buttons(self, buttons):
        """buttons: [(title, key, enabled)], left to right. A key of None means Cancel
        (Esc). Return is never bound, so nothing destructive happens by accident.
        Rebuilt only when titles/keys change, so enabling/disabling does not flicker."""
        layout = [(t, k) for t, k, _e in buttons]
        if layout != self._layout:
            for v in list(self.buttonRow.views()):
                self.buttonRow.removeView_(v)
            self._buttons = []
            for title, key, _e in buttons:
                b = NSButton.buttonWithTitle_target_action_(title, self, "pressed:")
                b.setTag_(len(self._buttons))
                if key is None:
                    b.setKeyEquivalent_("\x1b")
                self.buttonRow.addView_inGravity_(b, 3)       # trailing
                self._buttons.append((b, key))
            self._layout = layout
        for (b, _k), (_t, _key, enabled) in zip(self._buttons, buttons):
            b.setEnabled_(bool(enabled))

    @objc.python_method
    def close(self):
        if self.shown:
            self.shown = False
            self.bar.stopAnimation_(None)
            self.window.endSheet_(self.panel)
            self.panel.orderOut_(None)

    def pressed_(self, sender):
        key = self._buttons[sender.tag()][1]
        self.close()
        self.on_choice(key)
