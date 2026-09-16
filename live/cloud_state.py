"""The bot's working state as ONE object in Cloud Storage, for runs with no disk of their own.

A Cloud Run job starts with an empty filesystem and loses it when it exits.
Each run therefore:

  1. takes a LEASE (a small object created only if absent), so two runs never
     work from the same state -- a scheduler can start a new execution while
     a slow one is still going;
  2. downloads state.tar.gz (price caches in data/, the ledger and executor
     state in state/, trained models in models/) and unpacks it;
  3. does its work;
  4. packs and uploads the state with ifGenerationMatch, so an upload based on
     an old copy is refused instead of silently overwriting a newer one;
  5. releases the lease.

One download and one upload per run keeps Cloud Storage operations far inside
the free tier (a per-file sync would cost dozens of writes an hour).

Locations are URIs: gs://bucket/prefix on Cloud Run (authenticated through the
metadata server, so no client library or key file), or file:///some/dir for
tests and local rehearsals. Only the standard library and requests are used.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import tarfile
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

STATE_DIRS = ("data", "state", "models")
SKIP_SUFFIXES = (".lock", "-shm")       # lock files; SQLite rebuilds its -shm index
BUNDLE = "state.tar.gz"
LEASE = "lease.json"
BACKUP = "backups/state-{day}.tar.gz"   # one a day; a bucket lifecycle rule deletes old ones
METADATA_TOKEN = ("http://metadata.google.internal/computeMetadata/v1/instance/"
                  "service-accounts/default/token")
GCS_API = "https://storage.googleapis.com/storage/v1"
GCS_UPLOAD = "https://storage.googleapis.com/upload/storage/v1"


class StateConflict(RuntimeError):
    """Someone else holds the lease, or wrote the state since we read it."""


# ------------------------------------------------------------------ backends
class FileStore:
    """Objects as files in a directory, with generations kept in a sidecar. For tests."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _gen_path(self, name: str) -> Path:
        path = self.root / name
        return path.with_name(f".{path.name}.generation")

    def generation(self, name: str) -> int:
        p = self._gen_path(name)
        return int(p.read_text()) if (self.root / name).exists() and p.exists() else 0

    def read(self, name: str) -> tuple[bytes | None, int]:
        p = self.root / name
        return (p.read_bytes(), self.generation(name)) if p.exists() else (None, 0)

    def write(self, name: str, data: bytes, if_generation: int) -> int:
        if self.generation(name) != if_generation:
            raise StateConflict(f"{name} changed (generation {self.generation(name)}, "
                                f"expected {if_generation})")
        gen = max(time.time_ns(), if_generation + 1)     # the clock can repeat on Windows
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_bytes(data)
        self._gen_path(name).write_text(str(gen))
        return gen

    def delete(self, name: str, if_generation: int) -> None:
        if self.generation(name) == if_generation:
            (self.root / name).unlink(missing_ok=True)
            self._gen_path(name).unlink(missing_ok=True)


def gcloud_token() -> tuple[str, float]:
    """An access token from the gcloud CLI, for use outside Google Cloud."""
    import shutil
    import subprocess

    exe = shutil.which("gcloud")
    if exe is None:
        raise RuntimeError("no Google Cloud credentials: not on Google Cloud and gcloud is not installed")
    token = subprocess.run([exe, "auth", "print-access-token"], capture_output=True, text=True,
                           check=True).stdout.strip()
    return token, time.time() + 1800          # tokens last an hour; refresh well before


class GcsStore:
    """Objects under gs://bucket/prefix through the JSON API."""

    def __init__(self, bucket: str, prefix: str = "", session=None, token_getter=None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.http = session or requests.Session()
        self._token_getter = token_getter or self._metadata_token
        self._token, self._token_until = None, 0.0

    def _metadata_token(self) -> tuple[str, float]:
        try:
            r = self.http.get(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"}, timeout=5)
            r.raise_for_status()
        except requests.RequestException:
            return gcloud_token()             # not on Google Cloud: the PC's gcloud login
        body = r.json()
        return body["access_token"], time.time() + float(body.get("expires_in", 300))

    def _headers(self) -> dict:
        if self._token is None or time.time() > self._token_until - 60:
            self._token, self._token_until = self._token_getter()
        return {"Authorization": f"Bearer {self._token}"}

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def _url(self, name: str) -> str:
        return f"{GCS_API}/b/{self.bucket}/o/{quote(self._key(name), safe='')}"

    def generation(self, name: str) -> int:
        r = self.http.get(self._url(name), headers=self._headers(), timeout=30)
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        return int(r.json()["generation"])

    def read(self, name: str) -> tuple[bytes | None, int]:
        r = self.http.get(self._url(name), params={"alt": "media"}, headers=self._headers(),
                          timeout=300)
        if r.status_code == 404:
            return None, 0
        r.raise_for_status()
        return r.content, int(r.headers["x-goog-generation"])

    def write(self, name: str, data: bytes, if_generation: int) -> int:
        r = self.http.post(f"{GCS_UPLOAD}/b/{self.bucket}/o",
                           params={"uploadType": "media", "name": self._key(name),
                                   "ifGenerationMatch": str(if_generation)},
                           headers={**self._headers(), "Content-Type": "application/octet-stream"},
                           data=data, timeout=600)
        if r.status_code == 412:
            raise StateConflict(f"gs://{self.bucket}/{self._key(name)} changed since it was read")
        r.raise_for_status()
        return int(r.json()["generation"])

    def delete(self, name: str, if_generation: int) -> None:
        r = self.http.delete(self._url(name), params={"ifGenerationMatch": str(if_generation)},
                             headers=self._headers(), timeout=30)
        if r.status_code not in (200, 204, 404, 412):
            r.raise_for_status()


def open_store(uri: str):
    if uri.startswith("gs://"):
        bucket, _, prefix = uri[5:].partition("/")
        return GcsStore(bucket, prefix)
    if uri.startswith("file://"):
        return FileStore(uri[7:])
    raise ValueError(f"state location {uri!r}: use gs://bucket/prefix or file:///dir")


# ------------------------------------------------------------------- bundle
def checkpoint_sqlite(root: Path) -> None:
    """Fold WAL files into the databases so the archive holds complete files."""
    for db in (root / "state").glob("*.db"):
        with sqlite3.connect(db, timeout=60) as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def pack(root: Path) -> bytes:
    checkpoint_sqlite(root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for d in STATE_DIRS:
            base = root / d
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.name.endswith(SKIP_SUFFIXES):
                    tar.add(path, arcname=str(path.relative_to(root)).replace(os.sep, "/"))
    return buf.getvalue()


def unpack(data: bytes, root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if (not member.isfile() or member.name.startswith("/") or ".." in parts
                    or not parts or parts[0] not in STATE_DIRS):
                raise ValueError(f"refusing unexpected archive entry {member.name!r}")
            try:
                tar.extract(member, root, filter="data")
            except TypeError:                   # Python without extraction filters; entries checked above
                tar.extract(member, root)
            count += 1
    return count


# -------------------------------------------------------------------- lease
@dataclass
class Session:
    store: object
    root: Path
    owner: str
    lease_generation: int = 0
    state_generation: int = 0


def acquire(store, root: Path, owner: str, ttl: timedelta, now: datetime | None = None) -> Session:
    """Take the lease and restore the state into `root`. Raises StateConflict if held."""
    now = now or datetime.now(timezone.utc)
    body, gen = store.read(LEASE)
    if body is not None:
        held = json.loads(body)
        until = datetime.fromisoformat(held["until"])
        if until > now:
            raise StateConflict(f"state is leased by {held.get('owner')} until {held['until']}")
        print(f"taking over an expired lease from {held.get('owner')} (expired {held['until']})")
    lease = json.dumps({"owner": owner, "since": now.isoformat(timespec="seconds"),
                        "until": (now + ttl).isoformat(timespec="seconds")}).encode()
    session = Session(store, root, owner, lease_generation=store.write(LEASE, lease, gen))
    data, session.state_generation = store.read(BUNDLE)
    if data is None:
        print("no saved state yet: starting empty")
    else:
        print(f"restored {unpack(data, root)} files ({len(data) / 1e6:.1f} MB)")
    return session


def save(session: Session) -> bytes:
    data = pack(session.root)
    session.state_generation = session.store.write(BUNDLE, data, session.state_generation)
    print(f"saved state ({len(data) / 1e6:.1f} MB)")
    return data


def backup(store, data: bytes, day: str) -> bool:
    """Keep the first state of each UTC day, so a bad run can be rolled back by hand."""
    try:
        store.write(BACKUP.format(day=day), data, 0)
    except StateConflict:
        return False
    print(f"daily backup {BACKUP.format(day=day)}")
    return True


def release(session: Session) -> None:
    session.store.delete(LEASE, session.lease_generation)


def add_files(store, files, root: Path, owner: str, wait_seconds: float = 900,
              ttl: timedelta = timedelta(minutes=15)) -> list[str]:
    """Put local files (under data/, state/ or models/ of `root`) into the saved state.

    Takes the same lease as a run, waiting up to `wait_seconds` for one to finish,
    so it never overwrites a run's work. Returns the archive paths added.
    """
    import shutil
    import tempfile

    root = Path(root).resolve()
    rels = []
    for f in files:
        rel = Path(f).resolve().relative_to(root).as_posix()
        if rel.split("/")[0] not in ("data", "state", "models"):
            raise ValueError(f"{f}: only files under data/, state/ or models/ belong in the state")
        rels.append(rel)
    deadline = time.monotonic() + wait_seconds
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        while True:
            try:
                session = acquire(store, work, owner, ttl)
                break
            except StateConflict as e:
                if time.monotonic() >= deadline:
                    raise
                print(f"waiting: {e}")
                time.sleep(20)
        try:
            if not session.state_generation:
                raise StateConflict("there is no saved state to add to at this location")
            for rel in rels:
                (work / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / rel, work / rel)
            save(session)
        finally:
            release(session)
    return rels


def main() -> int:
    ap = argparse.ArgumentParser(description="Pack or unpack the bot's state archive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_pack = sub.add_parser("pack", help="data/, state/, models/ under --root -> --out")
    p_pack.add_argument("--root", default=".")
    p_pack.add_argument("--out", default=BUNDLE)
    p_unpack = sub.add_parser("unpack", help="--archive -> --root")
    p_unpack.add_argument("--archive", default=BUNDLE)
    p_unpack.add_argument("--root", default=".")
    p_add = sub.add_parser("add", help="put local files into the saved state at --uri (waits for a running job)")
    p_add.add_argument("--uri", required=True, help="gs://bucket/prefix, e.g. gs://PROJECT-signal-bot/prod")
    p_add.add_argument("--root", default=".", help="project folder the files are under")
    p_add.add_argument("files", nargs="+", help="e.g. data/mt5_Exness-MT5Real10_EUR_USD_H1.parquet")
    a = ap.parse_args()
    if a.cmd == "add":
        import glob
        import socket

        files = sorted({p for f in a.files for p in (glob.glob(f) or [f])})
        added = add_files(open_store(a.uri), files, Path(a.root), f"add-files {socket.gethostname()}")
        print(f"added {len(added)} files to {a.uri}:")
        for rel in added:
            print(f"  {rel}")
        return 0
    if a.cmd == "pack":
        data = pack(Path(a.root))
        Path(a.out).write_bytes(data)
        print(f"wrote {a.out} ({len(data) / 1e6:.1f} MB)")
    else:
        print(f"unpacked {unpack(Path(a.archive).read_bytes(), Path(a.root))} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
