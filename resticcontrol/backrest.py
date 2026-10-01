"""Find, check and start a Backrest server (no AppKit; unit-tested).

Backrest listens on 127.0.0.1:9898 unless BACKREST_PORT / --bind-address say otherwise.
The official installer runs it as the launchd agent "com.backrest"
(/Library/LaunchAgents/com.backrest.plist, port in its EnvironmentVariables);
Homebrew runs it via `brew services`.  Its web UI's page title is "Backrest".
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

DEFAULT_PORT = 9898
LAUNCHD_PLISTS = [
    ("/Library/LaunchAgents/com.backrest.plist", "com.backrest"),              # install.sh
    (os.path.expanduser("~/Library/LaunchAgents/homebrew.mxcl.backrest.plist"),
     "homebrew.mxcl.backrest"),                                                # brew services
]
BIN_CANDIDATES = ["/usr/local/bin/backrest", "/opt/homebrew/bin/backrest"]
BREW_CANDIDATES = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]


class BackrestError(RuntimeError):
    pass


@dataclass
class Probe:
    reachable: bool          # something answered over HTTP
    is_backrest: bool        # ... and it is Backrest
    message: str


# ----------------------------------------------------------------------------
# addresses
# ----------------------------------------------------------------------------

def normalize_url(text: str) -> str:
    """'9898' / 'nas' / 'nas:9898' / 'https://backrest.example' -> a full URL."""
    t = (text or "").strip()
    if not t:
        return f"http://127.0.0.1:{DEFAULT_PORT}/"
    if t.isdigit():                                       # just a port
        return f"http://127.0.0.1:{t}/"
    if "://" not in t:
        host = t.split("/", 1)[0]
        has_port = re.search(r":\d+$", host) is not None
        t = "http://" + t + ("" if has_port else f":{DEFAULT_PORT}")
    parts = urlsplit(t)
    return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}".rstrip("/") + "/"


def is_local(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", ""):
        return True
    me = socket.gethostname().lower()
    return host in (me, me.split(".")[0], me.split(".")[0] + ".local")


def url_for_bind(bind: str) -> str:
    """'127.0.0.1:9898' / ':9898' / '0.0.0.0:9898' / '[::]:9898' -> URL to reach it locally."""
    host, _, port = bind.rpartition(":")
    host = host.strip("[]")
    if host in ("", "*", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port or DEFAULT_PORT}/"


# ----------------------------------------------------------------------------
# probing
# ----------------------------------------------------------------------------

def probe(url: str, timeout: float = 3.0) -> Probe:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a proxy
    req = urllib.request.Request(url, headers={"User-Agent": "ResticControl"})
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                  # it answered, just not 200
        body = e.read(65536).decode("utf-8", "replace") if e.fp else ""
        if "backrest" in body.lower():
            return Probe(True, True, f"Backrest answered (HTTP {e.code})")
        return Probe(True, False, f"The server answered with HTTP {e.code}, but not as Backrest")
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ConnectionRefusedError):
            return Probe(False, False, "Nothing is listening there (connection refused)")
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return Probe(False, False, "No answer (timed out)")
        if isinstance(reason, socket.gaierror):
            return Probe(False, False, "Unknown host name")
        if isinstance(reason, ssl.SSLError):
            return Probe(False, False, f"TLS problem: {reason}")
        return Probe(False, False, str(reason))
    except (socket.timeout, TimeoutError):
        return Probe(False, False, "No answer (timed out)")
    except OSError as e:
        return Probe(False, False, str(e))
    if "<title>backrest</title>" in body.lower() or "backrest" in body.lower():
        return Probe(True, True, "Backrest is running")
    return Probe(True, False, "Something answers there, but it doesn't look like Backrest")


def wait_until_up(url: str, seconds: float = 20.0, interval: float = 0.7) -> Probe:
    deadline = time.monotonic() + seconds
    p = probe(url, timeout=2)
    while not p.is_backrest and time.monotonic() < deadline:
        time.sleep(interval)
        p = probe(url, timeout=2)
    return p


# ----------------------------------------------------------------------------
# local discovery
# ----------------------------------------------------------------------------

_LSOF_ADDR = re.compile(r"TCP\s+(\S+?)\s+\(LISTEN\)")


def parse_lsof(text: str) -> list:
    """Listening addresses ('127.0.0.1:9898', '*:9898', '[::1]:9898') from lsof output."""
    out = []
    for line in text.splitlines():
        m = _LSOF_ADDR.search(line)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def running_urls() -> list:
    """URLs of Backrest processes listening on this Mac (via lsof)."""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        out = subprocess.run([lsof, "-nP", "-a", "-c", "backrest", "-iTCP", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    urls = []
    for addr in parse_lsof(out):
        u = url_for_bind(addr)
        if u not in urls:
            urls.append(u)
    return sorted(urls, key=lambda u: 0 if "127.0.0.1" in u else 1)


def configured_url(plist_path: str) -> Optional[str]:
    """The address a launchd-managed Backrest will use (BACKREST_PORT or --bind-address)."""
    try:
        with open(plist_path, "rb") as f:
            pl = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    args = pl.get("ProgramArguments") or []
    for i, a in enumerate(args):
        if a.startswith("--bind-address="):
            return url_for_bind(a.split("=", 1)[1])
        if a == "--bind-address" and i + 1 < len(args):
            return url_for_bind(args[i + 1])
    bind = (pl.get("EnvironmentVariables") or {}).get("BACKREST_PORT")
    return url_for_bind(bind) if bind else f"http://127.0.0.1:{DEFAULT_PORT}/"


@dataclass
class Installation:
    method: str              # "launchd", "brew", "binary"
    detail: str              # plist path, brew path or binary path
    label: str = ""          # launchd label
    url: Optional[str] = None


def installation() -> Optional[Installation]:
    for plist, label in LAUNCHD_PLISTS:
        if os.path.exists(plist):
            method = "brew" if label.startswith("homebrew.") else "launchd"
            detail = plist
            if method == "brew":
                detail = next((b for b in BREW_CANDIDATES if os.path.exists(b)), plist)
            return Installation(method, detail, label, configured_url(plist))
    brew = next((b for b in BREW_CANDIDATES if os.path.exists(b)), None)
    exe = shutil.which("backrest") or next((b for b in BIN_CANDIDATES if os.path.exists(b)), None)
    if exe and brew and os.path.realpath(exe).startswith(os.path.dirname(os.path.dirname(brew))):
        return Installation("brew", brew, "homebrew.mxcl.backrest", None)
    if exe:
        return Installation("binary", exe, "", None)
    return None


def start_local(inst: Installation) -> str:
    """Start the locally installed Backrest the way it was installed. Returns a description."""
    uid = os.getuid()
    if inst.method == "launchd":
        target = f"gui/{uid}/{inst.label}"
        r = subprocess.run(["launchctl", "kickstart", target], capture_output=True, text=True)
        if r.returncode != 0:                                       # not loaded yet
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", inst.detail],
                           capture_output=True, text=True)
            r = subprocess.run(["launchctl", "kickstart", target], capture_output=True, text=True)
        if r.returncode != 0:
            raise BackrestError((r.stderr or r.stdout).strip() or "launchctl failed")
        return f"Started the Backrest service ({inst.label})"
    if inst.method == "brew":
        r = subprocess.run([inst.detail, "services", "start", "backrest"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise BackrestError((r.stderr or r.stdout).strip() or "brew services failed")
        return "Started Backrest with brew services (it will also start at login)"
    if inst.method == "binary":
        log_dir = os.path.expanduser("~/Library/Logs/ResticControl")
        os.makedirs(log_dir, exist_ok=True)
        log = open(os.path.join(log_dir, "backrest.log"), "ab")
        subprocess.Popen([inst.detail], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True)                   # keeps running after we quit
        return "Started Backrest (until you log out; it has no service set up)"
    raise BackrestError(f"Don't know how to start Backrest installed via {inst.method}")


# ----------------------------------------------------------------------------
# links clicked inside the embedded Backrest page
# ----------------------------------------------------------------------------

def _origin(url: str) -> tuple:
    u = urlsplit(url or "")
    scheme = (u.scheme or "").lower()
    port = u.port or {"http": 80, "https": 443}.get(scheme)
    return scheme, (u.hostname or "").lower(), port


def same_origin(url: str, base: str) -> bool:
    return bool(base) and _origin(url) == _origin(base)


def link_target(url: str, base: str, new_window: bool, in_links_tab: bool) -> str:
    """Where a navigation inside a web view should go:

    "here"    load it in the view it happened in
    "links"   load it in the separate links tab (the Backrest tab keeps showing Backrest)
    "system"  hand it to macOS (mailto:, other apps' URL schemes)
    """
    scheme = urlsplit(url or "").scheme.lower()
    if scheme in ("about", "data", "blob", "javascript", ""):
        return "here"
    if scheme not in ("http", "https"):
        return "system"
    if in_links_tab:
        return "here"                       # the links tab is a small browser of its own
    if new_window or not same_origin(url, base):
        return "links"                      # "new tab" links and other sites
    return "here"                           # Backrest's own pages stay in its tab
