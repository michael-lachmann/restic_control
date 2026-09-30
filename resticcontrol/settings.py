"""Settings window: manage repositories (sftp URL, password in Keychain, options)."""
from __future__ import annotations

import copy

import objc
from AppKit import (
    NSBackingStoreBuffered, NSButton, NSColor, NSGridView, NSLayoutConstraint, NSMakeRect,
    NSPopUpButton, NSSecureTextField, NSStackView, NSTextField, NSWindow,
)
from Foundation import NSObject

from .app import alert, label, run_async
from .backend import Restic
from .config import RepoConfig

STYLE = 1 | 2          # titled | closable


def field(placeholder="", secure=False, width=420):
    cls = NSSecureTextField if secure else NSTextField
    f = cls.alloc().initWithFrame_(NSMakeRect(0, 0, width, 22))
    f.setPlaceholderString_(placeholder)
    f.setTranslatesAutoresizingMaskIntoConstraints_(False)
    f.widthAnchor().constraintGreaterThanOrEqualToConstant_(width).setActive_(True)
    return f


def parse_env(text: str) -> dict:
    env = {}
    for part in text.replace("\n", ";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                env[k.strip()] = v.strip()
    return env


class SettingsController(NSObject):

    @objc.python_method
    def setup(self, main):
        self.main = main
        self.config = main.config
        self.current = None
        self.build()
        return self

    @objc.python_method
    def build(self):
        w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(300, 300, 640, 420), STYLE, NSBackingStoreBuffered, False)
        w.setTitle_("Repositories")
        w.setReleasedWhenClosed_(False)
        self.window = w

        self.popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 260, 26), False)
        self.popup.setTarget_(self)
        self.popup.setAction_("pickRepo:")
        add = NSButton.buttonWithTitle_target_action_("Add", self, "addRepo:")
        rem = NSButton.buttonWithTitle_target_action_("Remove", self, "removeRepo:")
        topRow = NSStackView.stackViewWithViews_([label("Repository:"), self.popup, add, rem])

        self.fName = field("My backup")
        self.fRepo = field("sftp:user@host:/srv/restic-repo")
        self.fPassword = field("stored in Keychain", secure=True)
        self.fPwCmd = field("optional, e.g. security find-generic-password -s restic -w")
        self.fRestic = field("auto-detect (/opt/homebrew/bin/restic …)")
        self.fExtra = field("-o sftp.args='-oBatchMode=yes'")
        self.fEnv = field("KEY=value; KEY2=value (e.g. RESTIC_CACHE_DIR)")
        self.fBackrest = field("http://127.0.0.1:9898")
        rows = [
            [label("Name:"), self.fName],
            [label("Repository URL:"), self.fRepo],
            [label("Password:"), self.fPassword],
            [label("…or password command:"), self.fPwCmd],
            [label("restic binary:"), self.fRestic],
            [label("Extra restic options:"), self.fExtra],
            [label("Environment:"), self.fEnv],
            [label(""), label("")],
            [label("Backrest URL (all repos):"), self.fBackrest],
        ]
        grid = NSGridView.gridViewWithViews_(rows)
        grid.setRowSpacing_(8)
        grid.setColumnSpacing_(8)
        grid.columnAtIndex_(0).setXPlacement_(3)   # trailing

        self.testStatus = label("")
        self.testStatus.setTextColor_(NSColor.secondaryLabelColor())
        test = NSButton.buttonWithTitle_target_action_("Test Connection", self, "testConnection:")
        close = NSButton.buttonWithTitle_target_action_("Close", self, "closeWindow:")
        close.setKeyEquivalent_("\x1b")
        save = NSButton.buttonWithTitle_target_action_("Save", self, "save:")
        save.setKeyEquivalent_("\r")
        bottom = NSStackView.stackViewWithViews_([test, self.testStatus, close, save])
        self.testStatus.setContentCompressionResistancePriority_forOrientation_(1, 0)

        content = w.contentView()
        stack = NSStackView.stackViewWithViews_([topRow, grid, bottom])
        stack.setOrientation_(1)          # vertical
        stack.setAlignment_(1)            # leading
        stack.setSpacing_(16)
        stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        content.addSubview_(stack)
        NSLayoutConstraint.activateConstraints_([
            stack.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 20),
            stack.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 20),
            stack.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -20),
            stack.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -20),
            bottom.trailingAnchor().constraintEqualToAnchor_(stack.trailingAnchor()),
        ])

    # ---------------------------------------------------------------------
    @objc.python_method
    def show(self):
        if not self.config.repos:
            self.config.repos.append(RepoConfig())
        self.current = self.config.selected or self.config.repos[0]
        self.rebuildPopup()
        self.loadFields()
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def rebuildPopup(self):
        self.popup.removeAllItems()
        for r in self.config.repos:
            self.popup.addItemWithTitle_(r.name)
            self.popup.lastItem().setRepresentedObject_(r.id)
        idx = self.popup.indexOfItemWithRepresentedObject_(self.current.id)
        if idx >= 0:
            self.popup.selectItemAtIndex_(idx)

    @objc.python_method
    def loadFields(self):
        r = self.current
        self.fName.setStringValue_(r.name)
        self.fRepo.setStringValue_(r.repository)
        self.fPassword.setStringValue_("")
        self.fPassword.setPlaceholderString_(
            "•••••• (stored in Keychain — type to replace)" if r.get_password() else "not set")
        self.fPwCmd.setStringValue_(r.password_command)
        self.fRestic.setStringValue_(r.restic_path)
        self.fExtra.setStringValue_(r.extra_args)
        self.fEnv.setStringValue_("; ".join(f"{k}={v}" for k, v in r.env.items()))
        self.fBackrest.setStringValue_(self.config.backrest_url)
        self.testStatus.setStringValue_("")

    @objc.python_method
    def applyFields(self, r):
        r.name = self.fName.stringValue().strip() or "Repository"
        r.repository = self.fRepo.stringValue().strip()
        r.password_command = self.fPwCmd.stringValue().strip()
        r.restic_path = self.fRestic.stringValue().strip()
        r.extra_args = self.fExtra.stringValue().strip()
        r.env = parse_env(self.fEnv.stringValue())

    # -------------------------------------------------------------- actions
    def pickRepo_(self, sender):
        rid = sender.selectedItem().representedObject()
        self.current = next(r for r in self.config.repos if r.id == rid)
        self.loadFields()

    def addRepo_(self, sender):
        self.current = RepoConfig()
        self.config.repos.append(self.current)
        self.rebuildPopup()
        self.loadFields()
        self.window.makeFirstResponder_(self.fName)

    def removeRepo_(self, sender):
        if self.current is None:
            return
        self.current.set_password(None)
        self.config.repos = [r for r in self.config.repos if r.id != self.current.id]
        if not self.config.repos:
            self.config.repos.append(RepoConfig())
        self.current = self.config.repos[0]
        self.config.selected_id = self.current.id
        self.config.save()
        self.rebuildPopup()
        self.loadFields()
        self.main.settingsSaved()

    def save_(self, sender):
        r = self.current
        self.applyFields(r)
        pw = self.fPassword.stringValue()
        try:
            if pw:
                r.set_password(pw)
        except Exception as e:  # noqa: BLE001
            alert("Could not store password in Keychain", str(e))
            return
        self.config.backrest_url = self.fBackrest.stringValue().strip() or self.config.backrest_url
        self.config.selected_id = r.id
        self.config.save()
        self.rebuildPopup()
        self.loadFields()
        self.window.orderOut_(None)
        self.main.settingsSaved()

    def closeWindow_(self, sender):
        self.window.orderOut_(None)

    def testConnection_(self, sender):
        probe = copy.deepcopy(self.current)
        self.applyFields(probe)
        spec = probe.spec()
        if self.fPassword.stringValue():
            spec.password = self.fPassword.stringValue()
        self.testStatus.setStringValue_("Connecting …")

        def work():
            r = Restic(spec)
            try:
                return r.check_connection()
            finally:
                r.close()
        run_async(work, lambda msg: self.testStatus.setStringValue_(msg),
                  lambda e: self.testStatus.setStringValue_(f"Failed: {e}"))
