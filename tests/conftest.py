"""Shared test setup: the Cocoa UI modules imported with AppKit & co. stubbed out."""
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def app():
    class NSObjectStub:
        @classmethod
        def alloc(cls): return cls()
        def init(self): return self
    saved = {k: sys.modules.get(k) for k in ("objc", "AppKit", "Foundation", "Quartz", "WebKit",
                                              "PyObjCTools", "PyObjCTools.AppHelper",
                                              "resticcontrol.app", "resticcontrol.uikit")}
    objc = types.ModuleType("objc")
    objc.python_method = lambda f: f
    objc.typedSelector = lambda sig: (lambda f: f)
    objc.super = super
    sys.modules["objc"] = objc
    for name in ("AppKit", "Foundation", "Quartz", "WebKit"):
        m = MagicMock(); m.NSObject = NSObjectStub
        for cls in ("NSTableView", "NSOutlineView"):
            setattr(m, cls, type(cls, (NSObjectStub,), {}))
        sys.modules[name] = m
    sys.modules["PyObjCTools"] = MagicMock(); sys.modules["PyObjCTools.AppHelper"] = MagicMock()
    for mod in ("resticcontrol.app", "resticcontrol.uikit"):
        sys.modules.pop(mod, None)
    from resticcontrol import app as appmod
    yield appmod
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

