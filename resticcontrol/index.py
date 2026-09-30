"""Persistent, de-duplicated directory-listing cache (SQLite).

Snapshots are immutable, so a listing of (tree, directory) never goes stale.
Most directories are identical between consecutive snapshots, so listings are
stored once as compressed blobs keyed by content hash; each (tree, dir) row is
only a few integers.  An index of 50 snapshots x 100k folders stays small.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import zlib
from typing import Iterable, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS trees  (id INTEGER PRIMARY KEY, tree TEXT UNIQUE NOT NULL,
                                   complete INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dirs   (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS blobs  (id INTEGER PRIMARY KEY, hash BLOB UNIQUE NOT NULL,
                                   data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS listing(tree_id INTEGER NOT NULL, dir_id INTEGER NOT NULL,
                                   blob_id INTEGER NOT NULL,
                                   PRIMARY KEY (tree_id, dir_id)) WITHOUT ROWID;
-- folder fingerprint (= restic tree id of that folder) per snapshot root tree; '' = absent
CREATE TABLE IF NOT EXISTS fps    (root_id INTEGER NOT NULL, dir_id INTEGER NOT NULL,
                                   fp TEXT NOT NULL,
                                   PRIMARY KEY (root_id, dir_id)) WITHOUT ROWID;
-- restic find results: per path and snapshot, the matching node JSON ('' = absent)
CREATE TABLE IF NOT EXISTS finds  (dir_id INTEGER NOT NULL, snap TEXT NOT NULL, node TEXT NOT NULL,
                                   PRIMARY KEY (dir_id, snap)) WITHOUT ROWID;
-- total file bytes below a folder, per snapshot root tree
CREATE TABLE IF NOT EXISTS sizes  (root_id INTEGER NOT NULL, dir_id INTEGER NOT NULL,
                                   bytes INTEGER NOT NULL,
                                   PRIMARY KEY (root_id, dir_id)) WITHOUT ROWID;
-- raw restic tree blobs (JSON, zlib) by tree id
CREATE TABLE IF NOT EXISTS rawtrees(fp TEXT PRIMARY KEY, data BLOB NOT NULL) WITHOUT ROWID;
"""


class ListingIndex:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.executescript(SCHEMA)
        self._tree_ids: dict = {}
        self._dir_ids: dict = {}

    # -- id helpers (call with lock held) ----------------------------------
    def _tree_id(self, tree: str, create: bool = True) -> Optional[int]:
        tid = self._tree_ids.get(tree)
        if tid is None:
            row = self._db.execute("SELECT id FROM trees WHERE tree=?", (tree,)).fetchone()
            if row is None:
                if not create:
                    return None
                tid = self._db.execute("INSERT INTO trees(tree) VALUES (?)", (tree,)).lastrowid
            else:
                tid = row[0]
            self._tree_ids[tree] = tid
        return tid

    def _dir_id(self, path: str, create: bool = True) -> Optional[int]:
        did = self._dir_ids.get(path)
        if did is None:
            row = self._db.execute("SELECT id FROM dirs WHERE path=?", (path,)).fetchone()
            if row is None:
                if not create:
                    return None
                did = self._db.execute("INSERT INTO dirs(path) VALUES (?)", (path,)).lastrowid
            else:
                did = row[0]
            self._dir_ids[path] = did
        return did

    def _put(self, tid: int, directory: str, entries: list) -> None:
        data = json.dumps(entries, separators=(",", ":"), sort_keys=True).encode()
        h = hashlib.sha1(data).digest()
        row = self._db.execute("SELECT id FROM blobs WHERE hash=?", (h,)).fetchone()
        bid = row[0] if row else self._db.execute(
            "INSERT INTO blobs(hash, data) VALUES (?, ?)", (h, zlib.compress(data, 6))).lastrowid
        self._db.execute("INSERT OR REPLACE INTO listing VALUES (?, ?, ?)",
                         (tid, self._dir_id(directory), bid))

    # -- public API ----------------------------------------------------------
    def get(self, tree: str, directory: str) -> Optional[list]:
        """Cached entries (list of dicts), [] for a missing dir in a complete tree, or None."""
        with self._lock:
            tid = self._tree_id(tree, create=False)
            if tid is None:
                return None
            did = self._dir_id(directory, create=False)
            if did is not None:
                row = self._db.execute(
                    "SELECT b.data FROM listing l JOIN blobs b ON b.id=l.blob_id "
                    "WHERE l.tree_id=? AND l.dir_id=?", (tid, did)).fetchone()
                if row:
                    return json.loads(zlib.decompress(row[0]))
            if self.is_complete(tree):
                return []            # fully indexed and not there -> does not exist
            return None

    def put(self, tree: str, directory: str, entries: list) -> None:
        with self._lock:
            self._put(self._tree_id(tree), directory, entries)

    def put_many(self, tree: str, listings: Iterable, complete: bool = False) -> None:
        """Store many (directory, entries) pairs in one transaction."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                tid = self._tree_id(tree)
                for directory, entries in listings:
                    self._put(tid, directory, entries)
                if complete:
                    self._db.execute("UPDATE trees SET complete=1 WHERE id=?", (tid,))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                self._tree_ids.clear()
                self._dir_ids.clear()
                raise

    # -- folder fingerprints ---------------------------------------------------
    def get_fp(self, root_tree: str, directory: str) -> Optional[str]:
        """Cached fingerprint, '' if the folder is known to be absent, None if unknown."""
        with self._lock:
            tid = self._tree_id(root_tree, create=False)
            did = self._dir_id(directory, create=False)
            if tid is None or did is None:
                return None
            row = self._db.execute("SELECT fp FROM fps WHERE root_id=? AND dir_id=?",
                                   (tid, did)).fetchone()
            return row[0] if row else None

    def put_fps(self, root_tree: str, items: Iterable) -> None:
        """Store many (directory, fingerprint) pairs for one snapshot root."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                tid = self._tree_id(root_tree)
                self._db.executemany("INSERT OR REPLACE INTO fps VALUES (?, ?, ?)",
                                     [(tid, self._dir_id(d), fp) for d, fp in items])
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                self._tree_ids.clear()
                self._dir_ids.clear()
                raise

    def get_finds(self, path: str) -> dict:
        with self._lock:
            did = self._dir_id(path, create=False)
            if did is None:
                return {}
            return dict(self._db.execute("SELECT snap, node FROM finds WHERE dir_id=?", (did,)))

    def put_finds(self, path: str, items: Iterable) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                did = self._dir_id(path)
                self._db.executemany("INSERT OR REPLACE INTO finds VALUES (?, ?, ?)",
                                     [(did, sid, node) for sid, node in items])
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                self._dir_ids.clear()
                raise

    def get_size(self, root_tree: str, directory: str) -> Optional[int]:
        with self._lock:
            tid = self._tree_id(root_tree, create=False)
            did = self._dir_id(directory, create=False)
            if tid is None or did is None:
                return None
            row = self._db.execute("SELECT bytes FROM sizes WHERE root_id=? AND dir_id=?",
                                   (tid, did)).fetchone()
            return row[0] if row else None

    def put_size(self, root_tree: str, directory: str, nbytes: int) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO sizes VALUES (?, ?, ?)",
                             (self._tree_id(root_tree), self._dir_id(directory), int(nbytes)))

    def get_raw_tree(self, fp: str) -> Optional[bytes]:
        with self._lock:
            row = self._db.execute("SELECT data FROM rawtrees WHERE fp=?", (fp,)).fetchone()
            return zlib.decompress(row[0]) if row else None

    def put_raw_tree(self, fp: str, data: bytes) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO rawtrees VALUES (?, ?)", (fp, zlib.compress(data, 6)))

    def is_complete(self, tree: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT complete FROM trees WHERE tree=?", (tree,)).fetchone()
            return bool(row and row[0])

    def stats(self) -> dict:
        with self._lock:
            q = lambda s: self._db.execute(s).fetchone()[0]  # noqa: E731
            return {"trees": q("SELECT count(*) FROM trees WHERE complete=1"),
                    "listings": q("SELECT count(*) FROM listing"),
                    "blobs": q("SELECT count(*) FROM blobs"),
                    "bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0}

    def close(self) -> None:
        with self._lock:
            self._db.close()
