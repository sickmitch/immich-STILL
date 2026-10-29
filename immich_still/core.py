"""Configurazione, protezione delle credenziali e primitive condivise."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path

SECRETS = []


def configure(path):
    p = Path(path).expanduser().absolute()
    if p.resolve(strict=True) != p:
        raise RuntimeError("Configurazione symlink/non canonica")
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        s = os.fstat(f.fileno())
        if (
            not stat.S_ISREG(s.st_mode)
            or s.st_nlink != 1
            or s.st_uid != os.geteuid()
            or stat.S_IMODE(s.st_mode) != 0o600
            or s.st_size > 1048576
        ):
            raise RuntimeError(
                "Config: richiesto file proprio, regolare, 0600, non hardlink"
            )
        value = json.load(f)
    if not isinstance(value, dict):
        raise RuntimeError("Configurazione non oggetto JSON")
    SECRETS[:] = [
        v
        for k, v in value.items()
        if ("password" in k or k == "api_key") and isinstance(v, str) and v
    ]
    return value


def redact(text):
    for secret in sorted(SECRETS, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


VERSION = "1.1.0"
FORMAT = "immich-still-native-v1.1"
MAX_BYTES = 256 * 1024 * 1024


class Stop(RuntimeError):
    pass


class Pending(Stop):
    pass


class Skip(Stop):
    pass


def require(ok, message):
    if not ok:
        raise Stop(message)


def canonical_id(value):
    try:
        v = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as e:
        raise Stop("UUID non valido") from e
    require(v == value, "UUID non canonico")
    return v


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def compact(v):
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha1(v):
    return hashlib.sha1(v).hexdigest()


def sha256(v):
    return hashlib.sha256(v).hexdigest()


def sql_json(v):
    return (
        "convert_from(decode('" + compact(v).encode().hex() + "','hex'),'UTF8')::jsonb"
    )


def ident(v):
    return '"' + v.replace('"', '""') + '"'


def db_sha(row):
    v = row.get("checksum", "")
    v = v[2:] if v.startswith("\\x") else v
    require(
        re.fullmatch("[a-f0-9]{40}", v) is not None
        and row.get("checksumAlgorithm") == "sha1",
        "Checksum non SHA-1 completo",
    )
    return v


def checked_run(cmd, *, input_data=None, timeout=120):
    try:
        r = subprocess.run(
            cmd,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise Stop("Timeout comando: " + str(cmd[0])) from e
    require(
        r.returncode == 0,
        "Comando fallito: "
        + str(cmd[0])
        + ": "
        + redact(r.stderr.decode(errors="replace")[-1800:]),
    )
    return r


def instant(value):
    require(isinstance(value, str), "Data assente/non valida")
    try:
        v = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise Stop("Data ISO non valida") from e
    require(v.tzinfo is not None, "Data senza offset")
    return v.astimezone(dt.timezone.utc)


def equal_date(a, b):
    return a is b if a is None or b is None else instant(a) == instant(b)


def file_read(p: Path, cap=MAX_BYTES):
    require(p.resolve(strict=True) == p, "Symlink nel percorso file: " + str(p))
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(p, flags | getattr(os, "O_NOATIME", 0))
    except PermissionError:
        fd = os.open(p, flags)
    with os.fdopen(fd, "rb") as f:
        a = os.fstat(f.fileno())
        require(
            stat.S_ISREG(a.st_mode) and 0 < a.st_size <= cap,
            "File non regolare/vuoto/troppo grande",
        )
        data = f.read(cap + 1)
        b = os.fstat(f.fileno())
    require(
        (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns)
        == (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns, b.st_ctime_ns)
        and len(data) == a.st_size,
        "File cambiato durante la lettura",
    )
    return data, dict(
        device=a.st_dev,
        inode=a.st_ino,
        bytes=a.st_size,
        uid=a.st_uid,
        gid=a.st_gid,
        mode=stat.S_IMODE(a.st_mode),
        mtime_ns=a.st_mtime_ns,
    )


def write_payload(p: Path, data: bytes):
    if p.exists():
        previous, _ = file_read(p)
        require(previous == data, "Payload del journal differente: " + str(p))
        return
    tmp = p.with_name("." + p.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # No overwrite of an unknown payload. Both names are on the local journal FS.
        os.link(tmp, p, follow_symlinks=False)
    finally:
        tmp.unlink(missing_ok=True)
    fd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def state_path(config_path, override=None, data=None):
    config_path = Path(config_path).expanduser().absolute()
    state = Path(
        override or (data or {}).get("native_state_dir") or config_path.parent / "state"
    ).expanduser()
    if not state.is_absolute():
        state = (Path.cwd() if override else config_path.parent) / state
    return state.absolute()


class Config:
    def __init__(self, path, state_override=None):
        self.path = Path(path).expanduser().absolute()
        self.data = configure(self.path)
        d = self.data
        for k in (
            "immich_url",
            "api_key",
            "host_storage_path",
            "container_storage_path",
            "db_container",
            "db_user",
            "db_name",
        ):
            require(isinstance(d.get(k), str) and d[k], "Config: manca " + k)
        for k in ("db_container", "db_user", "db_name"):
            require(
                re.fullmatch("[A-Za-z0-9_.-]+", d[k]) and not d[k].startswith("-"),
                "Nome non valido " + k,
            )
        self.root = Path(d["host_storage_path"]).resolve(strict=True)
        require(self.root.is_dir(), "Root media mancante")
        self.server_root = d["container_storage_path"].rstrip("/")
        require(
            self.server_root.startswith("/")
            and self.server_root != ""
            and ".." not in Path(self.server_root).parts,
            "Root container non valida",
        )
        self.state = state_path(self.path, state_override, d)
        require(
            self.state.resolve() == self.state
            and self.state != self.root
            and self.root not in self.state.parents,
            "Stato deve essere locale/canonico e fuori dai media",
        )
        d.setdefault("app_containers", ["immich"])
        d.setdefault("db_host", "127.0.0.1")
        d.setdefault("db_port", 5432)
        d.setdefault("db_password_file", "")
        require(
            d["db_host"] in ("127.0.0.1", "localhost", "::1")
            or str(d["db_host"]).startswith("/"),
            "DB deve essere locale al container",
        )
        require(
            type(d["db_port"]) is int and 1 <= d["db_port"] <= 65535,
            "db_port non valida",
        )
        for k, default in [
            ("http_timeout", 60),
            ("db_timeout", 120),
            ("thumbnail_timeout", 300),
            ("thumbnail_poll_interval", 2),
            ("metadata_timeout", 300),
        ]:
            d.setdefault(k, default)
            require(type(d[k]) is int and d[k] > 0, "Valore positivo necessario: " + k)
        self.public_keys = (
            "immich_url",
            "host_storage_path",
            "container_storage_path",
            "db_container",
            "db_user",
            "db_name",
            "db_host",
            "db_port",
        )

    def __getitem__(self, k):
        return self.data[k]

    def public(self):
        return {k: self.data[k] for k in self.public_keys}

    def host_path(self, server):
        require(
            isinstance(server, str) and server.startswith(self.server_root + "/"),
            "Percorso fuori root: " + str(server),
        )
        rel = server[len(self.server_root) + 1 :]
        require(
            ".." not in Path(rel).parts and not Path(rel).is_absolute(),
            "Traversal media",
        )
        p = self.root / rel
        require(p.resolve() == p, "Symlink media: " + str(p))
        return p
