"""Thin, UI-independent wrapper around the restic CLI.

Everything here is plain Python so it can be unit-tested without AppKit.
All calls are blocking; the UI runs them on worker threads.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from .index import ListingIndex

# GUI apps on macOS start with a minimal PATH; add the usual install locations.
EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
               os.path.expanduser("~/bin"), os.path.expanduser("~/.local/bin")]


class ResticError(RuntimeError):
    pass


class Cancelled(ResticError):
    """A newer request in the same slot replaced this one."""


# ----------------------------------------------------------------------------
# data types
# ----------------------------------------------------------------------------

_FRAC_RE = re.compile(r"(\.\d{6})\d+")


def parse_time(value: Optional[str]) -> Optional[datetime]:
    """Parse restic's RFC3339 timestamps (nanosecond precision, 'Z' or offset)."""
    if not value:
        return None
    v = _FRAC_RE.sub(r"\1", value.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


@dataclass(frozen=True)
class Snapshot:
    id: str
    short_id: str
    time: datetime
    hostname: str
    username: str
    paths: tuple
    tags: tuple
    tree: str
    total_bytes: Optional[int] = None     # whole snapshot (restic >= 0.17 summary)
    total_files: Optional[int] = None

    @classmethod
    def from_json(cls, d: dict) -> "Snapshot":
        return cls(
            id=d["id"],
            short_id=d.get("short_id", d["id"][:8]),
            time=parse_time(d.get("time")) or datetime.fromtimestamp(0, timezone.utc),
            hostname=d.get("hostname", ""),
            username=d.get("username", ""),
            paths=tuple(d.get("paths") or ()),
            tags=tuple(d.get("tags") or ()),
            tree=d.get("tree", d["id"]),
            total_bytes=(d.get("summary") or {}).get("total_bytes_processed"),
            total_files=(d.get("summary") or {}).get("total_files_processed"),
        )

    def is_root(self, path: str) -> bool:
        """Is *path* one of the paths this snapshot backed up (so its totals apply exactly)?"""
        p = path.rstrip("/") or "/"
        return any((q.rstrip("/") or "/") == p for q in self.paths)

    def label(self) -> str:
        t = self.time.astimezone().strftime("%Y-%m-%d %H:%M")
        return f"{t} — {self.hostname} — {self.short_id}"


@dataclass(frozen=True)
class Node:
    name: str
    path: str
    type: str                 # "file", "dir", "symlink", ...
    size: Optional[int]
    mtime: Optional[datetime]
    permissions: str = ""
    link_target: str = ""
    hidden_flag: bool = False     # macOS "hidden" file flag (UF_HIDDEN), local files only

    @property
    def is_dir(self) -> bool:
        return self.type == "dir"

    @property
    def is_hidden(self) -> bool:
        """Hidden the way Finder hides it: dotfiles, or the UF_HIDDEN flag."""
        return self.hidden_flag or self.name.startswith(".")

    @classmethod
    def from_json(cls, d: dict) -> "Node":
        path = d["path"]
        return cls(
            name=d.get("name") or posixpath.basename(path),
            path=path,
            type=d.get("type", "file"),
            size=d.get("size"),
            mtime=parse_time(d.get("mtime")),
            permissions=d.get("permissions", ""),
            link_target=d.get("linktarget", ""),
        )

    def to_json(self) -> dict:
        return {"name": self.name, "path": self.path, "type": self.type,
                "size": self.size, "permissions": self.permissions,
                "linktarget": self.link_target,
                "mtime": self.mtime.isoformat() if self.mtime else None}

    def version_key(self):
        """Two nodes with the same key are treated as the same version."""
        return (self.type, self.size, self.mtime, self.link_target)


@dataclass
class MergedEntry:
    """A directory entry seen across several snapshots."""
    node: Node                      # the node from the newest snapshot containing it
    newest: Snapshot                # that snapshot
    count: int = 1                  # number of snapshots containing it
    in_latest: bool = True          # present in the newest snapshot of the set?


@dataclass
class Version:
    snapshot: Snapshot              # newest snapshot with this version
    node: Node
    count: int = 1                  # identical copies folded into this row
    oldest: Optional[Snapshot] = None   # oldest snapshot with this version

    def __post_init__(self):
        if self.oldest is None:
            self.oldest = self.snapshot


@dataclass
class FolderVersion:
    """A run of consecutive snapshots in which a folder is identical (same tree id)."""
    fp: str                          # restic tree id of the folder
    snapshots: list                  # newest first
    exact: bool = True               # False while the binary search is still narrowing

    @property
    def newest(self) -> Snapshot:
        return self.snapshots[0]

    @property
    def oldest(self) -> Snapshot:
        return self.snapshots[-1]

    @property
    def count(self) -> int:
        return len(self.snapshots)


@dataclass
class Changes:
    """Direct-children comparison of two versions of a folder."""
    status: dict                     # name -> "added" | "removed" | "changed" | "meta"

    def summary(self) -> str:
        c = {"file": {}, "dir": {}}
        for name, (st, typ) in self.status.items():
            if st == "meta":
                continue
            bucket = c["dir" if typ == "dir" else "file"]
            bucket[st] = bucket.get(st, 0) + 1
        parts = []
        for typ, one, many in (("file", "file", "files"), ("dir", "folder", "folders")):
            for st in ("changed", "added", "removed"):
                n = c[typ].get(st, 0)
                if n:
                    parts.append(f"{n} {one if n == 1 else many} {st}")
        if not parts:
            return "only dates/permissions changed" if self.status else "no changes"
        return ", ".join(parts)

    def of(self, name: str) -> str:
        return self.status.get(name, ("", ""))[0]


EMPTY_TREE = b'{"nodes":[]}'


@dataclass
class ChangeEntry:
    """One item that differs between two versions of a folder."""
    name: str
    path: str
    type: str
    status: str                      # "added" | "removed" | "changed" | "meta"
    old: Optional["Node"] = None     # the item in the older snapshot (None if added)
    new: Optional["Node"] = None     # ... in the newer one (None if removed)

    @property
    def is_dir(self) -> bool:
        return self.type == "dir"

    @property
    def node(self) -> "Node":
        return self.new or self.old


class ChangeTotals:
    """Everything one `restic diff --metadata` of a folder says, kept for the change tree:

    counts  per folder: changed / added / removed files below it (recursively)
    kids    per folder: its changed direct children, name -> (status, type)
            (dropped for a huge diff, to bound memory: then only the counts remain)

    Paths in restic's output are relative to the compared folder; they are stored
    absolute.  The object fills in while restic runs; *complete* says when it's done."""

    MAX_PATHS = 300_000                  # keep the per-folder lists up to this many lines

    def __init__(self, base: str):
        self.base = normalize_dir(base)
        self.counts: dict = {}           # absolute folder path -> [changed, added, removed]
        self.kids: Optional[dict] = {}   # absolute folder path -> {name: (status, type)}
        self.seen = 0                    # change lines read so far
        self.stats: Optional[dict] = None
        self.complete = False
        self.error: Optional[BaseException] = None
        self.done = threading.Event()
        self.listeners: list = []        # progress callbacks (called with self)

    def _abs(self, parts) -> str:
        return self.base.rstrip("/") + "/" + "/".join(parts) if parts else self.base

    def add(self, rel: str, modifier: str) -> None:
        self.seen += 1
        is_dir = rel.endswith("/")
        parts = [p for p in rel.strip("/").split("/") if p]
        if not parts:
            return                       # the compared folder itself
        if "+" in modifier:
            status, k = "added", 1
        elif "-" in modifier:
            status, k = "removed", 2
        elif "M" in modifier or "T" in modifier:
            status, k = "changed", 0
        else:
            status, k = "meta", None     # "U": dates / permissions only
        if self.kids is not None:
            if self.seen > self.MAX_PATHS:
                self.kids = None
            else:
                for n in range(len(parts) - 1):          # ancestors: changed folders
                    self.kids.setdefault(self._abs(parts[:n]), {}).setdefault(
                        parts[n], ("changed", "dir"))
                parent = self.kids.setdefault(self._abs(parts[:-1]), {})
                prev = parent.get(parts[-1])
                if prev is None or prev[0] in ("changed", "meta"):
                    parent[parts[-1]] = (status, "dir" if is_dir else "file")
        if is_dir or k is None:
            return                       # folders' files are listed separately
        for n in range(len(parts)):
            d = self._abs(parts[:n])
            c = self.counts.get(d)
            if c is None:
                c = self.counts[d] = [0, 0, 0]
            c[k] += 1

    def of(self, path: str) -> Optional[tuple]:
        """(changed, added, removed) below *path*; (0, 0, 0) once complete; None = not yet known."""
        c = self.counts.get(normalize_dir(path))
        if c is not None:
            return tuple(c)
        return (0, 0, 0) if self.complete else None

    def children(self, path: str) -> Optional[list]:
        """Changed direct children of *path* as [(name, status, type)], or None if this
        diff can't tell (still running, too big, or *path* is not below its folder)."""
        path = normalize_dir(path)
        if not self.complete or self.kids is None:
            return None
        if path != self.base and not path.startswith(self.base.rstrip("/") + "/"):
            return None
        out = []
        for name, (status, typ) in (self.kids.get(path) or {}).items():
            if typ == "dir" and status in ("changed", "meta"):
                # a folder is "changed" if something inside differs, else only its dates did
                inside = self.kids.get(path.rstrip("/") + "/" + name)
                status = "changed" if inside else "meta"
            out.append((name, status, typ))
        return out


def fmt_counts(c) -> str:
    """'3 changed, 1 added files' style summary of a ChangeTotals entry."""
    if c is None:
        return ""
    parts = [f"{n} {w}" for n, w in zip(c, ("changed", "added", "removed")) if n]
    if not parts:
        return "no file changes (dates/permissions only)"
    total = sum(c)
    return ", ".join(parts) + (" file" if total == 1 else " files")


# ----------------------------------------------------------------------------
# repository runner
# ----------------------------------------------------------------------------

def find_restic(explicit: str = "") -> str:
    if explicit:
        return explicit
    path = os.pathsep.join([os.environ.get("PATH", "")] + EXTRA_PATHS)
    found = shutil.which("restic", path=path)
    if not found:
        raise ResticError("restic executable not found. Install it (brew install restic) "
                          "or set its path in Settings.")
    return found


@dataclass
class RepoSpec:
    repository: str
    password: Optional[str] = None
    password_command: str = ""
    restic_path: str = ""
    extra_args: str = ""                         # e.g. -o sftp.args='-oBatchMode=yes'
    env: dict = field(default_factory=dict)
    no_lock: bool = True


class Restic:
    def __init__(self, spec: RepoSpec, cache_dir: Optional[str] = None, workers: int = 4):
        self.spec = spec
        self.exe = find_restic(spec.restic_path)
        self.cache_dir = cache_dir or default_cache_dir()
        self.repo_hash = hashlib.sha1(spec.repository.encode()).hexdigest()[:16]
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self._mem: dict = {}
        self._inflight: dict = {}
        self._lock = threading.Lock()
        self._procs: set = set()          # running indexer processes (killed on close)
        self._slots: dict = {}            # slot name -> running Popen (newest request wins)
        self._groups: dict = {}           # group name -> set of running Popen
        self._fp_mem: dict = {}           # (root tree, dir) -> folder fingerprint ('' = absent)
        self._raw_mem: dict = {}          # fingerprint -> raw tree JSON bytes
        self._version: Optional[tuple] = None
        self._closed = False
        self.index = ListingIndex(os.path.join(self.cache_dir, "index", self.repo_hash + ".sqlite"))

    # -- process plumbing ---------------------------------------------------

    def _env(self) -> dict:
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([env.get("PATH", "/usr/bin:/bin")] + EXTRA_PATHS)
        env["RESTIC_REPOSITORY"] = self.spec.repository
        env.pop("RESTIC_REPOSITORY_FILE", None)
        if self.spec.password_command:
            env["RESTIC_PASSWORD_COMMAND"] = self.spec.password_command
            env.pop("RESTIC_PASSWORD", None)
        elif self.spec.password is not None:
            env["RESTIC_PASSWORD"] = self.spec.password
            env.pop("RESTIC_PASSWORD_COMMAND", None)
        env.update(self.spec.env or {})
        return env

    def _cmd(self, *args: str, read_only: bool = True) -> list:
        cmd = [self.exe]
        if read_only and self.spec.no_lock:
            cmd.append("--no-lock")
        if self.spec.extra_args:
            cmd += shlex.split(self.spec.extra_args)
        cmd += list(args)
        return cmd

    def run(self, *args: str, read_only: bool = True, stdout=subprocess.PIPE,
            timeout: Optional[float] = None, slot: Optional[str] = None,
            group: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run restic. With *slot*, starting a new call kills the previous one in that slot."""
        cmd = self._cmd(*args, read_only=read_only)
        proc = subprocess.Popen(cmd, env=self._env(), stdout=stdout, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
        with self._lock:
            self._procs.add(proc)
            if group:
                self._groups.setdefault(group, set()).add(proc)
            if slot:
                old = self._slots.get(slot)
                self._slots[slot] = proc
                if old is not None and old.poll() is None:
                    old.kill()
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            proc.kill()
            proc.communicate()
            raise ResticError(f"restic {args[0]} timed out") from e
        finally:
            with self._lock:
                self._procs.discard(proc)
                if group:
                    self._groups.get(group, set()).discard(proc)
                replaced = slot is not None and self._slots.get(slot) is not proc
                if slot and not replaced:
                    del self._slots[slot]
        if proc.returncode != 0:
            if replaced or self._closed or proc.returncode < 0:
                raise Cancelled("cancelled")
            err = (err or b"").decode(errors="replace").strip()
            raise ResticError(err.splitlines()[-1] if err else f"restic exited with {proc.returncode}")
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def cancel_group(self, group: str) -> None:
        """Kill every running restic process started for *group* (e.g. a stale view)."""
        with self._lock:
            procs = list(self._groups.get(group, ()))
        for p in procs:
            if p.poll() is None:
                p.kill()

    def run_json_stream(self, *args: str, on_message: Optional[Callable[[dict], None]] = None,
                        slot: Optional[str] = None, read_only: bool = True,
                        group: Optional[str] = None) -> Optional[dict]:
        """Run restic with --json, feed each JSON line to *on_message*; return the summary.

        Starting a new call in the same *slot* (or cancel(slot)) kills the running one."""
        cmd = self._cmd(*args, "--json", read_only=read_only)
        proc = subprocess.Popen(cmd, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        with self._lock:
            self._procs.add(proc)
            if group:
                self._groups.setdefault(group, set()).add(proc)
            old = self._slots.get(slot) if slot else None
            if slot:
                self._slots[slot] = proc
        if old is not None and old.poll() is None:
            old.kill()
        summary = None
        try:
            for line in proc.stdout:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("message_type") == "summary":
                    summary = d
                if on_message:
                    on_message(d)
            proc.wait()
            if proc.returncode != 0:
                replaced = slot is not None and self._slots.get(slot) is not proc
                if proc.returncode < 0 or self._closed or replaced:
                    raise Cancelled("cancelled")
                err = proc.stderr.read().decode(errors="replace").strip()
                raise ResticError(err.splitlines()[-1] if err else f"restic {args[0]} failed")
        finally:
            with self._lock:
                self._procs.discard(proc)
                if group:
                    self._groups.get(group, set()).discard(proc)
                if slot and self._slots.get(slot) is proc:
                    del self._slots[slot]
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return summary

    def cancel(self, slot: str) -> None:
        with self._lock:
            p = self._slots.get(slot)
        if p is not None and p.poll() is None:
            p.kill()

    # -- high level operations ---------------------------------------------

    def check_connection(self) -> str:
        p = self.run("cat", "config", timeout=120)
        cfg = json.loads(p.stdout or b"{}")
        return f"OK — repository id {cfg.get('id', '?')[:12]}, version {cfg.get('version', '?')}"

    def snapshots(self, host: Optional[str] = None) -> list:
        args = ["snapshots", "--json"]
        if host:
            args += ["--host", host]
        p = self.run(*args)
        snaps = [Snapshot.from_json(d) for d in json.loads(p.stdout or b"[]")]
        snaps.sort(key=lambda s: s.time, reverse=True)
        return snaps

    def snapshot_ids(self) -> set:
        """IDs of all snapshots: just a directory listing of the repository (no snapshot
        files are read), so it is cheap enough to poll for new backups."""
        p = self.run("list", "snapshots", timeout=120)
        return {line.strip() for line in (p.stdout or b"").decode().splitlines() if line.strip()}

    def ls(self, snap: Snapshot, directory: str) -> list:
        """Direct children of *directory* in *snap* (cached by tree id; snapshots are immutable)."""
        directory = normalize_dir(directory)
        key = (snap.tree, directory)
        with self._lock:
            if key in self._mem:
                return self._mem[key]
            key_lock = self._inflight.setdefault(key, threading.Lock())
        with key_lock:   # snapshots sharing a tree only run restic once
            with self._lock:
                if key in self._mem:
                    return self._mem[key]
            return self._ls_uncached(snap, directory, key)

    def _ls_uncached(self, snap: Snapshot, directory: str, key) -> list:
        cached = self.index.get(snap.tree, directory)
        if cached is None:
            fp = self._fp_get(snap, directory)
            raw = self._raw_get(fp) if fp else None
            if raw is not None:
                cached = [n.to_json() for n in nodes_from_tree(raw, directory)]
        if cached is not None:
            nodes = [Node.from_json(d) for d in cached]
        else:
            p = self.run("ls", "--json", snap.id, directory)
            nodes = []
            for line in (p.stdout or b"").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                if d.get("struct_type") == "snapshot" or "path" not in d:
                    continue
                if posixpath.dirname(d["path"].rstrip("/")) == directory:
                    nodes.append(Node.from_json(d))
            nodes.sort(key=sort_key)
            self.index.put(snap.tree, directory, [n.to_json() for n in nodes])
        with self._lock:
            self._mem[key] = nodes
        return nodes

    def is_indexed(self, snap: Snapshot) -> bool:
        return self.index.is_complete(snap.tree)

    def index_snapshot(self, snap: Snapshot,
                       progress: Optional[Callable[[int], None]] = None,
                       batch_dirs: int = 2000) -> int:
        """List the whole snapshot with ONE restic process and store every directory.

        restic ls prints a pre-order depth-first walk, so a directory is complete as
        soon as the walk leaves it; we flush those in batches and keep memory flat.
        Returns the number of entries seen.
        """
        if self.index.is_complete(snap.tree):
            return 0
        cmd = self._cmd("ls", "--json", snap.id)
        proc = subprocess.Popen(cmd, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        with self._lock:
            self._procs.add(proc)
        stack: list = [("/", [])]          # open directories along the current path
        seen_closed: set = set()
        ready: list = []
        count = 0

        def close_top():
            path, entries = stack.pop()
            entries.sort(key=lambda d: (0 if d["type"] == "dir" else 1, d["name"].casefold()))
            ready.append((path, entries))
            seen_closed.add(path)

        try:
            for line in proc.stdout:
                if not line.strip():
                    continue
                d = json.loads(line)
                if d.get("struct_type") == "snapshot" or "path" not in d:
                    continue
                node = Node.from_json(d)
                parent = posixpath.dirname(node.path.rstrip("/")) or "/"
                while len(stack) > 1 and stack[-1][0] != parent:
                    close_top()
                if stack[-1][0] != parent:
                    if parent in seen_closed:
                        raise ResticError("unexpected restic ls ordering; indexing aborted")
                    stack.append((parent, []))       # parent never printed (shouldn't happen)
                stack[-1][1].append(node.to_json())
                if node.is_dir:
                    stack.append((node.path, []))
                count += 1
                if len(ready) >= batch_dirs:
                    self.index.put_many(snap.tree, ready)
                    ready.clear()
                if progress and count % 5000 == 0:
                    progress(count)
            proc.wait()
            if proc.returncode != 0:
                err = proc.stderr.read().decode(errors="replace").strip()
                raise ResticError(err.splitlines()[-1] if err else "restic ls failed")
            while stack:
                close_top()
            self.index.put_many(snap.tree, ready, complete=True)
        finally:
            with self._lock:
                self._procs.discard(proc)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if progress:
            progress(count)
        return count

    def lookup(self, snap: Snapshot, path: str) -> Optional[Node]:
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        name = posixpath.basename(path.rstrip("/"))
        for n in self._ls_quiet(snap, parent):
            if n.name == name:
                return n
        return None

    def _ls_quiet(self, snap: Snapshot, directory: str) -> list:
        try:
            return self.ls(snap, directory)
        except ResticError:
            return []   # directory does not exist in this snapshot

    def merged_ls(self, snaps: list, directory: str,
                  progress: Optional[Callable[[int, int], None]] = None) -> list:
        """Union of *directory* over all *snaps* (newest first), incl. deleted entries."""
        if not snaps:
            return []
        listings = self._map(lambda s: self._ls_quiet(s, directory), snaps, progress)
        merged: dict = {}
        newest = snaps[0]
        for snap, nodes in zip(snaps, listings):
            for n in nodes:
                e = merged.get(n.name)
                if e is None:
                    merged[n.name] = MergedEntry(node=n, newest=snap, count=1,
                                                 in_latest=snap is newest)
                else:
                    e.count += 1
        return sorted(merged.values(), key=lambda e: sort_key(e.node))

    def history(self, snaps: list, path: str, collapse: bool = True,
                progress: Optional[Callable[[int, int], None]] = None) -> list:
        """All versions of *path* across *snaps* (newest first)."""
        found = self._map(lambda s: self.lookup(s, path), snaps, progress)
        versions = [Version(s, n) for s, n in zip(snaps, found) if n is not None]
        return fold_versions(versions) if collapse else versions

    def find_versions(self, path: str, snaps: list, host: Optional[str] = None,
                      collapse: bool = True, slot: Optional[str] = "history") -> list:
        """Versions of a *file* across all snapshots with ONE restic process.

        `restic find /abs/path` only descends into directories on the way to *path*,
        so this is cheap even for huge snapshots: one index load plus a short tree
        walk per snapshot.  (Don't use it for directories: a matching directory is
        walked completely.)
        """
        if host:                           # a machine: all its hostnames (.local, .lan, …)
            snaps = [sn for sn in snaps if same_machine(sn.hostname, host)]
        found = self.cached_versions(path, snaps)
        missing = [sn for sn in snaps if sn.id not in found]
        if missing:
            args = ["find", "--json"]
            if len(missing) < len(snaps):              # only the new snapshots
                for sn in missing:
                    args += ["--snapshot", sn.id]
            elif host:
                for h in hostnames_of(snaps, host):
                    args += ["--host", h]
            args.append(escape_pattern(path))
            out = self.run(*args, slot=slot).stdout or b""
            new = {sn.id: "" for sn in missing}        # searched and not found = absent
            for grp in json.loads(out.strip() or b"[]"):
                sid = grp.get("snapshot")
                for m in grp.get("matches") or []:
                    if m.get("path") == path and sid in new:
                        new[sid] = json.dumps(m)
                        break
            self.index.put_finds(path, new.items())
            found.update(new)
        by_id = {s.id: s for s in snaps}
        versions = [Version(by_id[sid], Node.from_json(json.loads(m)))
                    for sid, m in found.items() if m and sid in by_id]
        versions.sort(key=lambda v: v.snapshot.time, reverse=True)
        return fold_versions(versions) if collapse else versions

    def cached_versions(self, path: str, snaps: list) -> dict:
        """snapshot id -> match JSON ('' = absent) for snapshots already searched."""
        want = {s.id for s in snaps}
        return {k: v for k, v in self.index.get_finds(path).items() if k in want}

    # -- folder fingerprints & versions --------------------------------------

    def restic_version(self) -> tuple:
        if self._version is None:
            try:
                out = subprocess.run([self.exe, "version"], capture_output=True, text=True,
                                     timeout=20).stdout
                m = re.search(r"restic (\d+)\.(\d+)\.(\d+)", out)
                self._version = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
            except (OSError, subprocess.SubprocessError):
                self._version = (0, 0, 0)
        return self._version

    def _fp_get(self, snap: Snapshot, path: str) -> Optional[str]:
        if path == "/":
            return snap.tree
        key = (snap.tree, path)
        with self._lock:
            fp = self._fp_mem.get(key)
        if fp is None:
            fp = self.index.get_fp(snap.tree, path)
            if fp is not None:
                with self._lock:
                    self._fp_mem[key] = fp
        return fp

    def _fp_put(self, snap: Snapshot, items: list) -> None:
        with self._lock:
            for path, fp in items:
                self._fp_mem[(snap.tree, path)] = fp
        self.index.put_fps(snap.tree, items)

    def _raw_get(self, fp: str) -> Optional[bytes]:
        with self._lock:
            raw = self._raw_mem.get(fp)
        if raw is None:
            raw = self.index.get_raw_tree(fp)
            if raw is not None:
                with self._lock:
                    self._raw_mem[fp] = raw
        return raw

    def folder_tree(self, snap: Snapshot, path: str) -> Optional[tuple]:
        """(fingerprint, raw tree JSON) of folder *path* in *snap*, or None if absent.

        One `restic cat tree snap:path` at most; the result also yields the
        fingerprints of all subfolders for free, and everything is cached forever.
        """
        path = normalize_dir(path)
        fp = self._fp_get(snap, path)
        if fp == "":
            return None
        if fp:
            raw = self._raw_get(fp)
            if raw is not None:
                return fp, raw
        elif not snapshots_covering([snap], path):
            self._fp_put(snap, [(path, "")])
            return None
        try:
            raw = self.run("cat", "tree", f"{snap.id}:{path}", group="view").stdout or b""
        except Cancelled:
            raise
        except ResticError as e:
            if "not found" in str(e) or "not a directory" in str(e):
                self._fp_put(snap, [(path, "")])
                return None
            raise
        fp = hashlib.sha256(raw).hexdigest()
        with self._lock:
            self._raw_mem[fp] = raw
        self.index.put_raw_tree(fp, raw)
        items = [(path, fp)]
        for d in json.loads(raw).get("nodes") or []:
            child = posixpath.join(path, d["name"])
            items.append((child, d["subtree"] if d.get("type") == "dir" and d.get("subtree") else ""))
        self._fp_put(snap, items)
        return fp, raw

    def fingerprint(self, snap: Snapshot, path: str) -> Optional[str]:
        """Folder fingerprint (restic tree id) — equal means identical, recursively."""
        path = normalize_dir(path)
        fp = self._fp_get(snap, path)
        if fp is not None:
            return fp or None
        t = self.folder_tree(snap, path)
        return t[0] if t else None

    def folder_versions(self, snaps: list, path: str, full: bool = False,
                        progress: Optional[Callable] = None, parallel: int = 4,
                        stop: Optional[Callable[[], bool]] = None) -> list:
        """Distinct versions of a folder across *snaps* (newest first) by binary search.

        Probe the newest and oldest snapshot; wherever two probed snapshots differ,
        probe points in between (k-ary, *parallel* at a time) until every change is
        pinned between two adjacent snapshots.  An unchanged folder costs 2 calls.
        Assumes a folder does not change and then change back between two probes
        that agree; pass full=True to fingerprint every snapshot instead.
        """
        path = normalize_dir(path)
        snaps = list(snaps)
        n = len(snaps)
        if n == 0:
            return []
        UNKNOWN = object()
        fps: list = [UNKNOWN] * n
        calls = [0]

        def probe(idxs):
            if stop is not None and stop():
                raise Cancelled("superseded")
            idxs = sorted({i for i in idxs if fps[i] is UNKNOWN})
            if not idxs:
                return
            for i, fp in zip(idxs, self._map(lambda i: self.fingerprint(snaps[i], path), idxs)):
                fps[i] = fp
            calls[0] += len(idxs)
            if progress:
                progress(build(exact=False), calls[0])

        def build(exact=True):
            out, cur = [], None
            last_fp = UNKNOWN
            for i in range(n):
                fp = fps[i]
                if fp is UNKNOWN:            # between two agreeing probes, or still unknown
                    fp = last_fp
                last_fp = fp
                if fp is UNKNOWN or fp is None:
                    cur = None
                    continue
                if cur is not None and cur.fp == fp:
                    cur.snapshots.append(snaps[i])
                else:
                    cur = FolderVersion(fp=fp, snapshots=[snaps[i]], exact=exact)
                    out.append(cur)
            return out

        probe([0, n - 1])
        if full:
            probe(range(n))
        while True:
            known = [i for i in range(n) if fps[i] is not UNKNOWN]
            segs = [(a, b) for a, b in zip(known, known[1:]) if b - a > 1 and fps[a] != fps[b]]
            if not segs:
                break
            per = max(1, parallel // len(segs))
            mids = []
            for a, b in segs:
                k = min(per, b - a - 1)
                mids += [a + (b - a) * j // (k + 1) for j in range(1, k + 1)]
            probe(mids)
        result = build()
        with self._lock:                     # inferred (in memory only): identical run members
            for v in result:
                for sn in v.snapshots:
                    self._fp_mem.setdefault((sn.tree, path), v.fp)
        return result

    def folder_changes(self, new: Snapshot, old: Snapshot, path: str) -> Optional[Changes]:
        """Which direct children differ between two snapshots (2 cached `cat tree`s)."""
        a, b = self.folder_tree(old, path), self.folder_tree(new, path)
        if b is None:
            return None
        if a is None:
            return Changes({d["name"]: ("added", d.get("type", "file"))
                            for d in json.loads(b[1]).get("nodes") or []})
        if a[0] == b[0]:
            return Changes({})
        return compare_trees(a[1], b[1])

    def change_entries(self, new: Optional[Snapshot], old: Optional[Snapshot],
                       path: str) -> list:
        """What differs directly inside folder *path* between two snapshots, as a list
        of ChangeEntry (unchanged children left out).  Two cached `cat tree`s at most;
        a folder missing on one side counts as empty, so everything is added/removed."""
        path = normalize_dir(path)
        b = self.folder_tree(new, path) if new is not None else None
        a = self.folder_tree(old, path) if old is not None else None
        if a is not None and b is not None and a[0] == b[0]:
            return []
        ra = a[1] if a is not None else EMPTY_TREE
        rb = b[1] if b is not None else EMPTY_TREE
        olds = {n.name: n for n in nodes_from_tree(ra, path)}
        news = {n.name: n for n in nodes_from_tree(rb, path)}
        out = [ChangeEntry(name=name, path=posixpath.join(path, name), type=typ, status=st,
                           old=olds.get(name), new=news.get(name))
               for name, (st, typ) in compare_trees(ra, rb).status.items()]
        out.sort(key=lambda e: (not e.is_dir, e.name.casefold()))
        return out

    def change_totals(self, old: Snapshot, new: Snapshot, path: str,
                      progress: Optional[Callable] = None,
                      group: Optional[str] = None) -> Optional[ChangeTotals]:
        """One streamed `restic diff --metadata` of *path* (identical subtrees are skipped),
        kept as a ChangeTotals: counts below every folder and the changed items of each
        folder, so the change tree needs no further restic calls.  None for restic < 0.16.
        Cached per pair of snapshot trees; a second caller while restic runs shares the run."""
        if self.restic_version() < (0, 16, 0):
            return None
        path = normalize_dir(path)
        key = ("totals", old.tree, new.tree, path)
        with self._lock:
            totals = self._mem.get(key)
            running = totals is not None
            if not running:
                totals = self._mem[key] = ChangeTotals(path)
            if progress and not totals.done.is_set():
                totals.listeners.append(progress)
        if progress:
            progress(totals)
        if running:
            totals.done.wait()
            if totals.error is not None:
                raise totals.error
            return totals

        def on_message(d):
            if d.get("message_type") == "change":
                totals.add(d.get("path", ""), d.get("modifier", ""))
                if totals.seen % 2000 == 0:
                    for cb in list(totals.listeners):
                        cb(totals)
            elif d.get("message_type") == "statistics":
                totals.stats = {"changed": d.get("changed_files", 0),
                                "added": (d.get("added") or {}).get("files", 0),
                                "removed": (d.get("removed") or {}).get("files", 0)}
        try:
            self.run_json_stream("diff", "--metadata", f"{old.id}:{path}", f"{new.id}:{path}",
                                 on_message=on_message, group=group)
            totals.complete = True
        except BaseException as e:
            totals.error = e
            with self._lock:
                if self._mem.get(key) is totals:
                    del self._mem[key]           # try again next time
            raise
        finally:
            totals.listeners.clear()
            totals.done.set()
        return totals

    def cached_totals(self, old: Snapshot, new: Snapshot, path: str) -> Optional[ChangeTotals]:
        """The finished diff of *path* between these snapshots, if one was run."""
        with self._lock:
            t = self._mem.get(("totals", old.tree, new.tree, normalize_dir(path)))
        return t if t is not None and t.complete else None

    def diff_stats(self, old: Snapshot, new: Snapshot, path: str) -> Optional[dict]:
        """Recursive change counts of *path* (from change_totals, so the same diff also
        serves the change tree later)."""
        t = self.change_totals(old, new, path, group="view")
        return t.stats if t is not None else None

    def quick_change_entries(self, new: Snapshot, old: Snapshot, path: str,
                             totals: Optional[ChangeTotals]) -> Optional[list]:
        """change_entries() without any restic call, from a finished diff: names, types
        and statuses are exact; sizes and dates are not known (nodes carry None)."""
        items = totals.children(path) if totals is not None else None
        if items is None:
            return None
        path = normalize_dir(path)
        out = []
        for name, status, typ in items:
            p = posixpath.join(path, name)
            bare = Node(name=name, path=p, type=typ, size=None, mtime=None)
            out.append(ChangeEntry(name=name, path=p, type=typ, status=status,
                                   old=None if status == "added" else bare,
                                   new=None if status == "removed" else bare))
        out.sort(key=lambda e: (not e.is_dir, e.name.casefold()))
        return out

    def dump(self, snap: Snapshot, node: Node, dest: str) -> str:
        """Write a single file from the snapshot to *dest* (atomically)."""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".restic-")
        try:
            with os.fdopen(fd, "wb") as f:
                self.run("dump", snap.id, node.path, stdout=f)
            os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        if node.mtime:
            ts = node.mtime.timestamp()
            os.utime(dest, (ts, ts))
        return dest

    def restore(self, snap: Snapshot, node: Node, target_dir: str,
                conflict: str = "rename", trash: Optional[Callable[[str], None]] = None,
                journal: Optional["RestoreJournal"] = None,
                progress: Optional[Callable[[dict], None]] = None) -> str:
        """Restore *node* into *target_dir*; returns the path it ended up at.

        If an item with that name exists, *conflict* decides:
          "rename"          restore under a new name  "name (restored <date>).ext"
          "replace"         put the existing item away with *trash*, restore under the name
          "rename-current"  rename the existing item to "name (before restore <date>).ext"
        The restore always goes to a hidden temporary item (".name.restoring-…") next to
        the target first; the existing item is only touched after it has completed.
        A *journal* lets the next launch clean up after a crash / power loss.
        """
        dest = os.path.join(target_dir, node.name)
        exists = os.path.lexists(dest)
        if exists and conflict not in ("rename", "replace", "rename-current"):
            raise ValueError(f"unknown conflict mode {conflict!r}")
        if exists and conflict == "replace" and trash is None:
            raise ValueError("replace needs a trash function")
        tmp = os.path.join(target_dir, f".{node.name}.restoring-{os.getpid()}-{time.time_ns()}")
        if journal:
            journal.begin(tmp, dest)
        try:
            self._restore_into(snap, node, tmp, progress)
        except BaseException:
            _remove(tmp)
            if journal:
                journal.end(tmp)
            raise
        if exists and conflict == "rename":
            dest = unique_path(dest)
        if journal:
            journal.swapping(tmp, dest)
        if exists and conflict == "replace":
            trash(dest)
        elif exists and conflict == "rename-current":
            os.rename(dest, unique_path(dest, "before restore"))
        os.rename(tmp, dest)
        if journal:
            journal.end(tmp)
        return dest

    def _restore_into(self, snap: Snapshot, node: Node, dest: str,
                      progress: Optional[Callable[[dict], None]] = None) -> None:
        if node.is_dir:
            # snapshot:subfolder restores the *contents* of subfolder into --target
            self.run_json_stream("restore", f"{snap.id}:{node.path}", "--target", dest,
                                 on_message=_status_filter(progress), slot="restore",
                                 read_only=False)
        else:
            with open(dest, "wb") as f:                # dest is already a temp name
                self.run("dump", snap.id, node.path, stdout=f, slot="restore")
            if node.mtime:
                ts = node.mtime.timestamp()
                os.utime(dest, (ts, ts))

    def restore_size(self, snap: Snapshot, node: Node,
                     progress: Optional[Callable[[int, int], None]] = None) -> int:
        """Bytes a restore of *node* will write (files: known; folders: one streamed
        `restic ls --recursive` of just that folder).  Cached per snapshot tree."""
        cached = self.cached_size(snap, node)
        if cached is not None:
            return cached
        key = ("size", snap.tree, node.path)
        cmd = self._cmd("ls", "--json", "--recursive", snap.id, node.path)
        proc = subprocess.Popen(cmd, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        with self._lock:
            self._procs.add(proc)
            old = self._slots.get("measure")
            self._slots["measure"] = proc
        if old is not None and old.poll() is None:
            old.kill()
        prefix = node.path.rstrip("/") + "/"
        total = files = 0
        try:
            for line in proc.stdout:
                if b'"type":"file"' not in line:
                    continue
                d = json.loads(line)
                if d.get("type") == "file" and d.get("path", "").startswith(prefix):
                    total += d.get("size") or 0
                    files += 1
                    if progress and files % 2000 == 0:
                        progress(files, total)
            proc.wait()
            if proc.returncode != 0:
                if proc.returncode < 0 or self._closed:
                    raise Cancelled("cancelled")
                err = proc.stderr.read().decode(errors="replace").strip()
                raise ResticError(err.splitlines()[-1] if err else "restic ls failed")
        finally:
            with self._lock:
                self._procs.discard(proc)
                if self._slots.get("measure") is proc:
                    del self._slots["measure"]
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if progress:
            progress(files, total)
        with self._lock:
            self._mem[key] = total
        self.index.put_size(snap.tree, node.path, total)
        return total

    def cached_size(self, snap: Snapshot, node: Node) -> Optional[int]:
        """Size without any restic call, if known: files, backup roots (snapshot total),
        or folders measured before (kept permanently — snapshots never change)."""
        if not node.is_dir:
            return node.size or 0
        if snap.total_bytes is not None and snap.is_root(node.path):
            return snap.total_bytes
        key = ("size", snap.tree, node.path)
        with self._lock:
            if key in self._mem:
                return self._mem[key]
        v = self.index.get_size(snap.tree, node.path)
        if v is not None:
            with self._lock:
                self._mem[key] = v
        return v

    def in_place_plan(self, snap: Snapshot, node: Node, dest: str,
                      progress: Optional[Callable[[dict], None]] = None) -> Optional[dict]:
        """What restoring *node* over the existing folder *dest* would write.

        `restic restore --overwrite if-changed --dry-run` compares backup and disk and
        changes nothing.  Returns {"files": n, "bytes": b, "skipped": m} or None when
        (n counts folders whose dates get reset too; b is exact)
        restic is older than 0.17 (no --overwrite / --dry-run).
        """
        if not node.is_dir or self.restic_version() < (0, 17, 0):
            return None
        cmd = self._cmd("restore", f"{snap.id}:{node.path}", "--target", dest,
                        "--overwrite", "if-changed", "--dry-run", "--json")
        proc = subprocess.Popen(cmd, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        with self._lock:
            self._procs.add(proc)
            old = self._slots.get("measure-inplace")
            self._slots["measure-inplace"] = proc
        if old is not None and old.poll() is None:
            old.kill()
        summary = None
        try:
            for line in proc.stdout:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("message_type") == "summary":
                    summary = d
                elif d.get("message_type") == "status" and progress:
                    progress(d)          # percent_done, bytes_restored (= differs), bytes_skipped
            proc.wait()
            if proc.returncode != 0:
                if proc.returncode < 0 or self._closed:
                    raise Cancelled("cancelled")
                err = proc.stderr.read().decode(errors="replace").strip()
                raise ResticError(err.splitlines()[-1] if err else "restic restore --dry-run failed")
        finally:
            with self._lock:
                self._procs.discard(proc)
                if self._slots.get("measure-inplace") is proc:
                    del self._slots["measure-inplace"]
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if summary is None:
            return None
        restored, skipped = summary.get("bytes_restored", 0), summary.get("bytes_skipped", 0)
        self.index.put_size(snap.tree, node.path, restored + skipped)
        return {"files": summary.get("files_restored", 0), "bytes": restored,
                "skipped": summary.get("files_skipped", 0), "total": restored + skipped}

    def restore_in_place(self, snap: Snapshot, node: Node, dest: str,
                         progress: Optional[Callable[[dict], None]] = None,
                         delete: bool = False) -> str:
        """Rewrite only the files in *dest* that differ from the backup (restic >= 0.17).
        Files that exist only on disk are kept.  Not undoable: changes the folder directly."""
        if not node.is_dir:
            raise ValueError("in-place restore is for folders")
        if self.restic_version() < (0, 17, 0):
            raise ResticError("updating in place needs restic 0.17 or newer")
        extra = ["--delete"] if delete else []        # delete=True: exactly like the backup
        self.run_json_stream("restore", f"{snap.id}:{node.path}", "--target", dest,
                             "--overwrite", "if-changed", *extra,
                             on_message=_status_filter(progress), slot="restore", read_only=False)
        return dest

    def busy(self) -> bool:
        """Is any restic process of ours still running?"""
        with self._lock:
            return any(p.poll() is None for p in self._procs)

    def cache_path(self, snap: Snapshot, node: Node) -> str:
        """Where materialize() puts (or has put) the preview copy of *node*."""
        return os.path.join(self.cache_dir, "files", self.repo_hash, snap.id[:16],
                            node.path.lstrip("/"))

    def materialize(self, snap: Snapshot, node: Node) -> str:
        """Local copy of a file for Quick Look, kept in the cache (content is immutable)."""
        dest = self.cache_path(snap, node)
        if node.is_dir:
            os.makedirs(dest, exist_ok=True)
            return dest
        if not os.path.exists(dest):
            self.dump(snap, node, dest)
            try:
                os.chmod(dest, 0o444)     # a preview copy, not a restore: keep it read-only
            except OSError:
                pass
        return dest

    # -- helpers ------------------------------------------------------------

    def _map(self, fn, items: list, progress=None) -> list:
        total, done = len(items), [0]
        lock = threading.Lock()

        def wrapped(x):
            r = fn(x)
            if progress:
                with lock:
                    done[0] += 1
                    progress(done[0], total)
            return r
        return list(self._pool.map(wrapped, items))

    def clear_cache(self) -> None:
        self.stop_indexing()
        with self._lock:
            self._mem.clear()
        path = self.index.path
        self.index.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)
        shutil.rmtree(os.path.join(self.cache_dir, "files", self.repo_hash), ignore_errors=True)
        self.index = ListingIndex(path)

    def stop_indexing(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            if p.poll() is None:
                p.kill()

    def close(self) -> None:
        self._closed = True
        self.stop_indexing()
        self._pool.shutdown(wait=False, cancel_futures=True)


# ----------------------------------------------------------------------------
# utilities
# ----------------------------------------------------------------------------

def default_cache_dir() -> str:
    base = os.path.expanduser("~/Library/Caches")
    if not os.path.isdir(base):                       # non-macOS (tests)
        base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return os.path.join(base, "ResticControl")


def normalize_dir(d: str) -> str:
    d = "/" + d.strip("/")
    return d


def sort_key(n: Node):
    return (0 if n.is_dir else 1, n.name.casefold())


def unique_path(path: str, label: str = "restored") -> str:
    """*path* if free, else "name (<label> <date time>).ext"."""
    if not os.path.lexists(path):
        return path
    base, ext = os.path.splitext(path.rstrip("/"))
    stamp = datetime.now().strftime("%Y-%m-%d %H.%M.%S")
    cand = f"{base} ({label} {stamp}){ext}"
    i = 2
    while os.path.lexists(cand):
        cand = f"{base} ({label} {stamp} {i}){ext}"
        i += 1
    return cand


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)


def restore_conflicts(items: Iterable, target_for: Callable) -> list:
    """Paths that already exist where (node, target_dir) pairs would be restored."""
    out = []
    for node in items:
        p = os.path.join(target_for(node), node.name)
        if os.path.lexists(p):
            out.append(p)
    return out


def machine_of(hostname: str) -> str:
    """The machine part of a hostname: macOS reports "Mac.local" or "Mac.lan" depending on
    the network, so snapshots of one Mac carry several hostnames."""
    return (hostname or "").split(".")[0]


def same_machine(a: str, b: str) -> bool:
    return machine_of(a).casefold() == machine_of(b).casefold()


def hosts_of(snaps: Iterable[Snapshot]) -> list:
    """One entry per machine (hostnames differing only after the first dot are merged)."""
    seen = {}
    for s in snaps:
        seen.setdefault(machine_of(s.hostname).casefold(), machine_of(s.hostname))
    return sorted(seen.values(), key=str.casefold)


def hostnames_of(snaps: Iterable[Snapshot], machine: str) -> list:
    """The actual hostnames restic recorded for *machine*."""
    return sorted({s.hostname for s in snaps if same_machine(s.hostname, machine)})


def fold_versions(versions: list) -> list:
    """Fold consecutive identical versions (newest first) into one row with a count."""
    out: list = []
    for v in versions:
        if out and out[-1].node.version_key() == v.node.version_key() and not v.node.is_dir:
            out[-1].count += 1
            out[-1].oldest = v.snapshot
        else:
            out.append(Version(v.snapshot, v.node, v.count, v.oldest))
    return out


def nodes_from_tree(raw: bytes, directory: str) -> list:
    """Nodes of a raw restic tree blob (as printed by `restic cat tree`)."""
    out = []
    for d in json.loads(raw).get("nodes") or []:
        d = dict(d, path=posixpath.join(directory, d["name"]))
        if d.get("type") == "dir":
            d.pop("size", None)
        out.append(Node.from_json(d))
    out.sort(key=sort_key)
    return out


def compare_trees(old_raw: bytes, new_raw: bytes) -> Changes:
    old = {d["name"]: d for d in json.loads(old_raw).get("nodes") or []}
    new = {d["name"]: d for d in json.loads(new_raw).get("nodes") or []}
    status = {}
    for name, d in new.items():
        o = old.get(name)
        typ = d.get("type", "file")
        if o is None:
            status[name] = ("added", typ)
        elif o.get("type") != typ:
            status[name] = ("changed", typ)
        elif typ == "dir":
            if o.get("subtree") != d.get("subtree"):
                status[name] = ("changed", typ)
        elif (o.get("content"), o.get("size"), o.get("linktarget")) != \
                (d.get("content"), d.get("size"), d.get("linktarget")):
            status[name] = ("changed", typ)
        elif o != d:
            status[name] = ("meta", typ)
    for name, o in old.items():
        if name not in new:
            status[name] = ("removed", o.get("type", "file"))
    return Changes(status)


def escape_pattern(path: str) -> str:
    """Escape glob characters so restic's filter matches *path* literally."""
    return re.sub(r"([*?\[\]\\])", r"\\\1", path)


def snapshots_containing(snaps: Iterable[Snapshot], path: str) -> list:
    """Snapshots that backed up all of *path* (one of their paths is *path* or above it)."""
    path = path.rstrip("/") or "/"
    out = []
    for s in snaps:
        for p in s.paths:
            p = p.rstrip("/") or "/"
            if p == "/" or path == p or path.startswith(p + "/"):
                out.append(s)
                break
    return out


def snapshots_for_folder(snaps: Iterable[Snapshot], path: str) -> list:
    """The snapshots to compare as versions of folder *path*: those holding all of it.

    A snapshot of only a subfolder (another backup plan, say) holds just part of *path*;
    counting it as a version would make everything else look deleted and re-added.
    Only if no snapshot holds all of *path* (e.g. /Users above a backed-up home folder)
    are the partial ones used, since those are all there is."""
    snaps = list(snaps)
    return snapshots_containing(snaps, path) or snapshots_covering(snaps, path)


def plan_of(snap: Snapshot) -> str:
    """Backrest plan that made *snap* (its "plan:" tag), or ""."""
    for t in snap.tags:
        if t.startswith("plan:"):
            return t[5:]
    return ""


def snapshots_covering(snaps: Iterable[Snapshot], path: str) -> list:
    """Snapshots whose backup paths contain *path* (or lie below it)."""
    path = path.rstrip("/") or "/"
    out = []
    for s in snaps:
        for p in s.paths:
            p = p.rstrip("/") or "/"
            if p == "/" or path == p or path.startswith(p + "/") or p.startswith(path.rstrip("/") + "/"):
                out.append(s)
                break
    return out


# ----------------------------------------------------------------------------
# local file system (left pane)
# ----------------------------------------------------------------------------

def local_node(path: str, name: Optional[str] = None) -> Optional[Node]:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    import stat as _stat
    if _stat.S_ISDIR(st.st_mode):
        typ = "dir"
    elif _stat.S_ISLNK(st.st_mode):
        typ = "symlink"
    elif _stat.S_ISREG(st.st_mode):
        typ = "file"
    else:
        typ = "other"
    return Node(name=name or posixpath.basename(path.rstrip("/")) or "/", path=path, type=typ,
                size=st.st_size if typ != "dir" else None,
                mtime=datetime.fromtimestamp(st.st_mtime, timezone.utc),
                hidden_flag=bool(getattr(st, "st_flags", 0) & 0x8000))      # UF_HIDDEN


def list_local(directory: str) -> list:
    out = []
    try:
        with os.scandir(directory) as it:
            for e in it:
                n = local_node(e.path, e.name)
                if n is not None:
                    out.append(n)
    except OSError:
        return []
    out.sort(key=sort_key)
    return out


def disk_status(node: Node) -> str:
    """Compare a backed-up node with what is on disk now: '', 'same', 'changed', 'missing'."""
    cur = local_node(node.path)
    if cur is None:
        return "missing"
    if node.is_dir or cur.is_dir:
        return "" if node.is_dir == cur.is_dir else "changed"
    same_size = node.size == cur.size
    same_time = (node.mtime is not None and cur.mtime is not None
                 and abs(node.mtime.timestamp() - cur.mtime.timestamp()) < 1.0)
    return "same" if same_size and same_time else "changed"


def fmt_delta(new: Optional[int], old: Optional[int]) -> str:
    """Human size change between two versions of a file."""
    if new is None or old is None:
        return "changed"
    d = new - old
    if d == 0:
        return "modified (same size)"
    n = abs(d)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            num = f"{n:.0f}" if unit == "B" else f"{n:.1f}"
            return f"{'+' if d > 0 else '−'}{num} {unit}"
        n /= 1000
    return ""


class RestoreJournal:
    """Remembers restores in progress, so an interrupted one can be cleaned up.

    Each entry: temp path, final path, phase ("restoring" or "swapping").
    recover(): "restoring" -> delete the partial temp item;
               "swapping"  -> the temp is complete: finish the rename if the name is free.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _set(self, tmp: str, value: Optional[dict]) -> None:
        with self._lock:
            d = self._load()
            if value is None:
                d.pop(tmp, None)
            else:
                d[tmp] = value
            self._save(d)

    def begin(self, tmp: str, dest: str) -> None:
        self._set(tmp, {"dest": dest, "phase": "restoring"})

    def swapping(self, tmp: str, dest: str) -> None:
        self._set(tmp, {"dest": dest, "phase": "swapping"})

    def end(self, tmp: str) -> None:
        self._set(tmp, None)

    def pending(self) -> dict:
        with self._lock:
            return self._load()

    def recover(self) -> list:
        """Clean up after interrupted restores; returns human-readable notes."""
        notes = []
        for tmp, info in self.pending().items():
            dest, phase = info.get("dest", ""), info.get("phase")
            name = os.path.basename(tmp)
            if not name.startswith(".") or ".restoring-" not in name:
                self.end(tmp)                          # not ours: never touch it
                continue
            if phase == "swapping" and os.path.lexists(tmp) and not os.path.lexists(dest):
                os.rename(tmp, dest)
                notes.append(f"Finished an interrupted restore: {dest}")
            elif os.path.lexists(tmp):
                _remove(tmp)
                notes.append(f"Removed an incomplete restore of {os.path.basename(dest)}")
            self.end(tmp)
        return notes


def existing_dir(path: str) -> str:
    """*path* or its nearest existing ancestor."""
    while path not in ("", "/") and not os.path.isdir(path):
        path = os.path.dirname(path)
    return path or "/"


def free_space(path: str) -> int:
    """Bytes available on the volume holding *path* (statvfs; the app may use a better API)."""
    st = os.statvfs(existing_dir(path))
    return st.f_bavail * st.f_frsize


def volume_id(path: str) -> int:
    return os.stat(existing_dir(path)).st_dev


def space_shortfalls(needs: Iterable, free: Callable[[str], int] = free_space,
                     margin: float = 0.02, min_margin: int = 100 * 1024 * 1024) -> list:
    """Group (target_dir, bytes) by volume; return [(target_dir, needed, available)] that don't fit.

    A safety margin (2 %, at least 100 MB) is added: restic needs a little room for
    metadata and macOS gets unhappy on a completely full disk.
    """
    per_volume: dict = {}
    for target, size in needs:
        vid = volume_id(target)
        tgt, total = per_volume.get(vid, (target, 0))
        per_volume[vid] = (tgt, total + size)
    out = []
    for target, total in per_volume.values():
        needed = total + max(int(total * margin), min_margin)
        avail = free(target)
        if needed > avail:
            out.append((target, needed, avail))
    return out


def _status_filter(progress: Optional[Callable[[dict], None]]):
    """Pass restic's periodic "status" messages (percent_done, bytes_restored, …) on."""
    if progress is None:
        return None

    def on_message(d: dict) -> None:
        if d.get("message_type") == "status":
            progress(d)
    return on_message


def local_size(path: str, progress: Optional[Callable[[int, int], None]] = None,
               stop: Optional[Callable[[], bool]] = None) -> int:
    """Bytes of all files below *path* on this disk (no symlinks followed)."""
    total = files = 0
    stack = [path]
    while stack:
        if stop is not None and stop():
            raise Cancelled("stopped")
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
        if progress and files and files % 5000 < 50:
            progress(files, total)
    if progress:
        progress(files, total)
    return total


# ----------------------------------------------------------------------------
# revealing a path given from Finder
# ----------------------------------------------------------------------------

def normalize_local_path(path: str) -> str:
    """Finder / NSURL may say /System/Volumes/Data/Users/…, /private/var/…, or end in '/'."""
    p = os.path.abspath(path.rstrip("/") or "/")
    for prefix in ("/System/Volumes/Data",):
        if p == prefix:
            return "/"
        if p.startswith(prefix + "/"):
            p = p[len(prefix):]
    return p


def reveal_chain(target: str, roots: Iterable[str]) -> Optional[tuple]:
    """(root, [each deeper path down to target]) for the deepest root containing target,
    or None if target is in none of them."""
    target = normalize_local_path(target)
    best = None
    for r in roots:
        r = normalize_local_path(r)
        if target == r or target.startswith(r.rstrip("/") + "/"):
            if best is None or len(r) > len(best):
                best = r
    if best is None:
        return None
    rest = target[len(best):].strip("/")
    chain, cur = [], best
    for part in (rest.split("/") if rest else []):
        cur = posixpath.join(cur, part)
        chain.append(cur)
    return best, chain
