#!/usr/bin/env python3
"""Durable local journals, owned by the unprivileged coordinator.
No temporary directories, no effective configs, no automatic recreation of runs.
The registry detects missing journals on later processes as well as the active run.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path

from . import core as S
from .core import FORMAT, VERSION, canonical_id, instant, require, sha256, timestamp

MAX_JSON = 32 * 1024 * 1024
TERMINAL = {"completed", "audit", "restored", "aborted"}


def encode(value):
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    ).encode()


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        require(
            stat.S_ISREG(st.st_mode) and st.st_nlink == 1,
            "JSON non regolare: " + str(path),
        )
        require(st.st_size <= MAX_JSON, "JSON troppo grande: " + str(path))
        return json.load(f)


def write(path, data):
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(encode(data))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def fingerprint(path):
    st = path.lstat()
    require(
        stat.S_ISDIR(st.st_mode) and not path.is_symlink(),
        "Directory non canonica: " + str(path),
    )
    require(
        st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o700,
        "Journal deve appartenere al chiamante con permessi 0700: " + str(path),
    )
    return [st.st_dev, st.st_ino]


def entries(root):
    p = root / "runs.jsonl"
    if not p.exists():
        return []
    with os.fdopen(os.open(p, os.O_RDONLY | os.O_NOFOLLOW), "r") as f:
        return [json.loads(x) for x in f if x.strip()]


def append(root, value):
    fd = os.open(
        root / "runs.jsonl",
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        require(
            os.fstat(fd).st_uid == os.geteuid(),
            "Registro appartenente a un altro utente",
        )
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode()
        while data:
            n = os.write(fd, data)
            require(n > 0, "Scrittura registro incompleta")
            data = data[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(root)


def proof(path):
    path = Path(path).expanduser().absolute()
    require(path.resolve() == path, "Symlink nel percorso journal")
    identity = fingerprint(path)
    root_id = fingerprint(path.parent)
    marker = read(path / "marker.json")
    info = read(path / "run.json")
    require(
        marker.get("format") == FORMAT and info.get("format") == FORMAT,
        "Versione journal non riconosciuta",
    )
    require(marker.get("owner_uid") == os.geteuid(), "Identità journal diversa")
    records = [
        r
        for r in entries(path.parent)
        if r.get("event") == "created" and r.get("path") == str(path)
    ]
    require(len(records) == 1, "Run non registrata univocamente")
    record = records[0]
    digest = sha256(encode(marker))
    require(
        record["directory_identity"] == identity
        and record["root_identity"] == root_id
        and record["marker_sha256"] == digest,
        "Directory o marker cambiati rispetto alla creazione",
    )
    for value in info.get("durable_pairs", []):
        p = path / (str(uuid.UUID(value)) + ".json")
        pair = read(p)
        require(pair.get("id") == value, "Record coppia non valido: " + str(p))
    return dict(
        ok=True,
        path=str(path),
        observer_uid=os.geteuid(),
        directory_identity=identity,
        root_identity=root_id,
        marker_sha256=digest,
        status=info.get("status"),
        same_run_as_creation=True,
        selection=info.get("selection", []),
        durable_pairs=info.get("durable_pairs", []),
    )


LEGACY_CURSOR_SCHEMA = "immich-still-native/cursor-1"
CURSOR_SCHEMA = "immich-still-native/cursor-2"
CURSOR_FILE = "cursor.json"


def load_cursor(state_root: Path):
    path = state_root / CURSOR_FILE
    if not path.exists():
        return None
    data = read(path)
    require(isinstance(data, dict), "Cursore non oggetto JSON")
    require(
        data.get("schema") in (LEGACY_CURSOR_SCHEMA, CURSOR_SCHEMA),
        "Cursor non riconosciuto: cancellare " + str(path) + " oppure aggiornarlo",
    )
    canonical_id(data["id"])
    instant(data["fileCreatedAt"])
    if data["schema"] == CURSOR_SCHEMA:
        require("discovery" in data, "Cursore senza riferimento ai caricamenti")
        if data["discovery"] is not None:
            canonical_id(data["discovery"]["id"])
            instant(data["discovery"]["createdAt"])
        require(
            isinstance(data.get("scope"), dict), "Ambito cursore assente/non valido"
        )
    return data


def recover_cursor(state_root: Path):
    """A cursor is an optimization, never the authorization for remote writes."""
    try:
        return load_cursor(state_root)
    except (S.Stop, ValueError, KeyError, TypeError) as error:
        path = state_root / CURSOR_FILE
        st = path.lstat()
        require(
            stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid(),
            "Cursore non regolare/proprio: ripristino automatico non consentito",
        )
        backup = path.with_name("cursor-invalid-" + uuid.uuid4().hex + ".json")
        os.replace(path, backup)
        fsync_dir(state_root)
        print(S.brief("CURSOR reset backup=" + backup.name + " " + str(error)))
        return None


def save_cursor(
    state_root: Path,
    *,
    asset_id: str,
    file_created_at: str,
    created_at: str,
    scope: dict,
    note: str,
    run_path: Path | None,
    discovery_start=None,
    recent=False,
):
    canonical_id(asset_id)
    timeline_key = (instant(file_created_at), asset_id)
    upload_key = (instant(created_at), asset_id)
    previous = load_cursor(state_root)
    same_scope = (
        previous and previous["schema"] == CURSOR_SCHEMA and previous["scope"] == scope
    )
    if same_scope:
        discovery = previous["discovery"]
        source_id, capture = previous["id"], previous["fileCreatedAt"]
    else:
        discovery = discovery_start
        source_id, capture = asset_id, file_created_at
    if recent:
        require(same_scope, "Recupero caricamenti senza cursore timeline")
        if discovery is None or upload_key > (
            instant(discovery["createdAt"]),
            discovery["id"],
        ):
            discovery = {"id": asset_id, "createdAt": created_at}
    elif not same_scope or timeline_key > (instant(capture), source_id):
        source_id, capture = asset_id, file_created_at
    if (
        same_scope
        and source_id == previous["id"]
        and capture == previous["fileCreatedAt"]
        and discovery == previous["discovery"]
    ):
        return previous
    payload = {
        "schema": CURSOR_SCHEMA,
        "updatedAt": timestamp(),
        "id": source_id,
        "fileCreatedAt": capture,
        "discovery": discovery,
        "scope": scope,
        "note": note,
        "run": str(run_path) if run_path else None,
    }
    write(state_root / CURSOR_FILE, payload)
    return payload


def pending_runs(root):
    """Enumerate durable unfinished conversions without modifying their state."""
    result = []
    seen = set()
    for record in entries(root):
        if record.get("event") != "created" or record.get("mode") in (
            "audit",
            "check-write",
        ):
            continue
        path = Path(record["path"])
        require(path.parent == root, "Registro con run fuori root")
        require(path not in seen, "Run registrata più volte")
        seen.add(path)
        # Lost/corrupted evidence cannot safely be invented. A temporary mount
        # problem is retried by the next normal invocation, not by forced writes.
        proof(path)
        info = read(path / "run.json")
        if info.get("status") not in TERMINAL:
            result.append((path, info))
    return result


class Journal:
    def __init__(self, cfg, mode, existing=None):
        self.root = cfg.state
        require(self.root.resolve() == self.root, "Root journal non canonica")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root_id = fingerprint(self.root)
        lockfd = os.open(
            self.root / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        self.lock = os.fdopen(lockfd, "a")
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if existing:
            self.path = Path(existing).expanduser().absolute()
            require(
                self.path.parent == self.root,
                "RUN deve essere figlio diretto della root journal",
            )
            proof(self.path)
            self.info = read(self.path / "run.json")
            old = self.info["config"]
            for k in cfg.public():
                require(
                    old.get(k) == cfg.data.get(k), "Config diversa dal journal: " + k
                )
            self.marker = read(self.path / "marker.json")
        else:
            name = (
                dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
                + uuid.uuid4().hex[:10]
            )
            self.path = self.root / name
            self.path.mkdir(mode=0o700)
            fsync_dir(self.root)
            self.marker = dict(
                format=FORMAT,
                version=VERSION,
                owner_uid=os.geteuid(),
                run_id=name,
                nonce=uuid.uuid4().hex,
                created=timestamp(),
                mode=mode,
            )
            write(self.path / "marker.json", self.marker)
            append(
                self.root,
                dict(
                    event="created",
                    mode=mode,
                    path=str(self.path),
                    created=timestamp(),
                    directory_identity=fingerprint(self.path),
                    root_identity=self.root_id,
                    marker_sha256=sha256(encode(self.marker)),
                ),
            )
            self.info = dict(
                format=FORMAT,
                version=VERSION,
                mode=mode,
                created=timestamp(),
                status="planning",
                config=cfg.public(),
                config_path=str(cfg.path),
                selection=[],
                durable_pairs=[],
            )
        self.path_id = fingerprint(self.path)
        self.logfile = os.fdopen(
            os.open(
                self.path / "run.log",
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            ),
            "a",
            encoding="utf-8",
            buffering=1,
        )
        self.save()
        self.checkpoint(independent=True)

    def guard(self):
        require(
            fingerprint(self.root) == self.root_id,
            "Root journal sparita/sostituita: " + str(self.root),
        )
        require(
            fingerprint(self.path) == self.path_id,
            "Run sparita/sostituita: " + str(self.path),
        )
        require(read(self.path / "marker.json") == self.marker, "Marker run modificato")

    def save(self):
        self.guard()
        write(self.path / "run.json", self.info)

    def checkpoint(self, independent=False):
        self.guard()
        require(
            read(self.path / "run.json") == self.info,
            "Stato disco diverso dallo stato in memoria",
        )
        if independent:
            r = subprocess.run(
                [sys.executable, "-m", "immich_still.journal", str(self.path)],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True,
                timeout=30,
            )
            require(
                r.returncode == 0,
                "Rilettura indipendente journal fallita: "
                + r.stderr.decode(errors="replace"),
            )
            p = json.loads(r.stdout)
            require(
                p["directory_identity"] == self.path_id
                and p["root_identity"] == self.root_id
                and p["ok"],
                "Secondo processo non vede lo stesso journal",
            )
        fsync_dir(self.path)
        fsync_dir(self.root)

    def log(self, message):
        self.guard()
        line = (
            "["
            + dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            + "] "
            + S.brief(message)
        )
        self.logfile.write(line + "\n")
        self.logfile.flush()
        os.fsync(self.logfile.fileno())
        print(line, flush=True)

    def save_pair(self, pair):
        self.guard()
        pair["journal_updated"] = timestamp()
        key = str(uuid.UUID(pair["id"]))
        require(key == pair["id"], "ID coppia non canonico")
        write(self.path / (key + ".json"), pair)
        if key not in self.info.setdefault("durable_pairs", []):
            self.info["durable_pairs"].append(key)
        self.save()

    def pairs(self):
        self.guard()
        result = []
        for key in self.info["selection"]:
            require(str(uuid.UUID(key)) == key, "ID selezione non valido")
            path = self.path / (key + ".json")
            if path.exists():
                result.append(read(path))
            else:
                require(
                    key not in self.info.get("durable_pairs", []),
                    "Record coppia scomparso: " + str(path),
                )
        return result

    def close(self):
        try:
            self.guard()
            append(
                self.root,
                dict(
                    event="closed",
                    path=str(self.path),
                    at=timestamp(),
                    status=self.info.get("status"),
                ),
            )
        except Exception as e:
            print(
                S.brief("WARN journal " + str(e)),
                file=sys.stderr,
            )
        finally:
            self.logfile.close()
            self.lock.close()


if __name__ == "__main__":
    try:
        print(json.dumps(proof(Path(sys.argv[1])), indent=2))
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
