"""Build a standalone .app:  pip install py2app && python setup.py py2app

Works with a python.org / Homebrew Python in a venv (recommended, smallest app) and
with Anaconda: conda's extension modules load shared libraries (libffi, libssl,
libsqlite3, …) via @rpath from the conda env, which py2app does not copy by itself.
They are found here with `otool -L` and bundled into Contents/Frameworks.
"""
import os
import subprocess
import sys

from setuptools import setup

# modulegraph walks the AST of every imported module recursively; long if/elif
# chains in some packages (common in Anaconda installs) overflow the default limit.
sys.setrecursionlimit(20000)

SKIP_EXTENSIONS = ("_tkinter",)          # not used; would drag in Tcl/Tk


def rpath_libraries():
    """Shared libraries that the stdlib extension modules load via @rpath (conda)."""
    if not os.path.isdir(os.path.join(sys.prefix, "conda-meta")):
        return []                                     # not Anaconda: nothing to do
    libdir = os.path.join(sys.prefix, "lib")
    dynload = os.path.join(libdir, f"python{sys.version_info[0]}.{sys.version_info[1]}",
                           "lib-dynload")
    todo = [os.path.join(dynload, f) for f in os.listdir(dynload)
            if f.endswith(".so") and not f.startswith(SKIP_EXTENSIONS)]
    seen, found = set(), []
    while todo:
        binary = todo.pop()
        out = subprocess.run(["otool", "-L", binary], capture_output=True, text=True).stdout
        for line in out.splitlines()[1:]:
            name = line.strip().split(" (")[0]
            if not name.startswith("@rpath/"):
                continue
            lib = os.path.realpath(os.path.join(libdir, name[len("@rpath/"):]))
            if os.path.exists(lib) and lib not in seen:
                seen.add(lib)
                found.append(os.path.join(libdir, name[len("@rpath/"):]))
                todo.append(lib)                     # libssl -> libcrypto, …
    if found:
        print("Bundling conda libraries:", ", ".join(os.path.basename(f) for f in found))
    return found


setup(
    app=["main.py"],
    name="Restic Control",
    options={"py2app": {
        "iconfile": "resources/ResticControl.icns",   # regenerate: python resources/make_icon.py
        "packages": ["resticcontrol", "keyring"],
        "frameworks": rpath_libraries(),
        # keep Anaconda's scientific stack out of the bundle
        "excludes": ["numpy", "scipy", "pandas", "matplotlib", "IPython", "jupyter",
                     "notebook", "PyQt5", "PyQt6", "tkinter", "sympy", "numba",
                     "llvmlite", "tornado", "zmq", "sphinx", "pytest"],
        "plist": {
            "CFBundleName": "Restic Control",
            "CFBundleIdentifier": "com.claritype.resticcontrol",
            "CFBundleShortVersionString": "0.1.0",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # allow the embedded Backrest UI on http://localhost / LAN
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True,
                                       "NSAllowsArbitraryLoadsInWebContent": True},
            # Finder: right-click → Services → "Check in Restic Control"
            "NSServices": [{
                "NSMenuItem": {"default": "Check in Restic Control"},
                "NSMessage": "checkInResticControl",
                "NSPortName": "Restic Control",
                "NSSendFileTypes": ["public.item"],          # any file or folder
                # also declare the file-URL pasteboard type, as current examples do
                "NSSendTypes": ["public.file-url", "NSFilenamesPboardType"],
                "NSRequiredContext": {},                     # enabled by default
            }],
            # accept files/folders dropped on the Dock icon — without ever becoming the
            # default app for anything (rank None)
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Files and Folders",
                "CFBundleTypeRole": "Viewer",
                "LSItemContentTypes": ["public.item"],
                "LSHandlerRank": "None",
            }],
        },
    }},
)
