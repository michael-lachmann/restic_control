"""Static safety net for the Cocoa code, which can't run here: every method called on a
class's own instance (self.x(...)) or on the main controller (main.x(...),
self.main.x(...)) must exist.  Catches e.g. a helper lost in an edit."""
import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "..", "resticcontrol")
FILES = ["app.py", "uikit.py", "settings.py"]

# Methods inherited from NSObject / NSView / NSTableView … that the code calls on self
INHERITED = {"init", "alloc", "performSelector_withObject_afterDelay_", "respondsToSelector_",
             "window", "setNeedsDisplay_", "reloadData", "selectedRow", "keyDown_"}


def classes(tree):
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def methods(cls):
    names = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    # instance attributes assigned in the class (callbacks such as self.on_choice = …)
    for n in ast.walk(cls):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                for el in (t.elts if isinstance(t, ast.Tuple) else [t]):
                    if isinstance(el, ast.Attribute) and isinstance(el.value, ast.Name) \
                            and el.value.id == "self":
                        names.add(el.attr)
    return names


def calls_on(node, receiver):
    """Names X of calls receiver.X(...) inside node; receiver is 'self', 'main' or 'self.main'."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            v = n.func.value
            if receiver in ("self", "main") and isinstance(v, ast.Name) and v.id == receiver:
                out.append((n.func.attr, n.lineno))
            if receiver == "self.main" and isinstance(v, ast.Attribute) and v.attr == "main" \
                    and isinstance(v.value, ast.Name) and v.value.id == "self":
                out.append((n.func.attr, n.lineno))
    return out


@pytest.mark.parametrize("fname", FILES)
def test_self_calls_exist(fname):
    tree = ast.parse(open(os.path.join(PKG, fname)).read())
    missing = []
    for name, cls in classes(tree).items():
        have = methods(cls) | INHERITED
        for attr, line in calls_on(cls, "self"):
            if attr not in have:
                missing.append(f"{fname}:{line} {name}: self.{attr}()")
    assert not missing, "\n".join(missing)


def test_calls_on_main_controller_exist():
    trees = {f: ast.parse(open(os.path.join(PKG, f)).read()) for f in FILES}
    main_cls = classes(trees["app.py"])["MainController"]
    have = methods(main_cls) | INHERITED
    missing = []
    for fname, tree in trees.items():
        for name, cls in classes(tree).items():
            if name == "MainController":
                continue
            for recv in ("main", "self.main"):
                for attr, line in calls_on(cls, recv):
                    if attr not in have:
                        missing.append(f"{fname}:{line} {name}: {recv}.{attr}()")
    assert not missing, "\n".join(missing)
