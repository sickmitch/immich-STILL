"""PostgreSQL READ ONLY observations via existing Docker authentication. No catalog writes."""

from __future__ import annotations

import json
import os
import queue
import re
import stat
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .core import (
    Config,
    Stop,
    canonical_id,
    checked_run,
    ident,
    instant,
    require,
    sql_json,
)

MAX_PASSWORD_BYTES = 8192
CONTAINER_PASSWORD_READER = r"""
if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then
    test -f "$POSTGRES_PASSWORD_FILE" && test -r "$POSTGRES_PASSWORD_FILE" || exit 42
    printf 'file\n'
    head -c 8195 "$POSTGRES_PASSWORD_FILE"
elif [ -n "${POSTGRES_PASSWORD:-}" ]; then
    printf 'env\n%s' "$POSTGRES_PASSWORD"
else
    printf 'native\n'
fi
"""
CREATE_PASSFILE = r"""
umask 077
mkdir -- "$1"
trap 'rm -f -- "$1/pgpass"; rmdir -- "$1"' EXIT HUP INT TERM
cat > "$1/pgpass"
chmod 600 "$1/pgpass"
trap - EXIT HUP INT TERM
"""
REMOVE_PASSFILE = 'if [ -d "$1" ]; then rm -f -- "$1/pgpass"; rmdir -- "$1"; fi'


def normalize_password(raw: bytes, *, from_file: bool) -> bytes:
    # A text editor may add ONE final LF/CRLF. Spaces and all other bytes are
    # significant. An environment password is not a text file: never trim it.
    if from_file:
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
    require(
        0 < len(raw) <= MAX_PASSWORD_BYTES,
        "Password DB vuota o troppo lunga (massimo 8192 byte)",
    )
    require(
        not any(c in raw for c in (b"\n", b"\r", b"\x00")),
        "Password DB multilinea o con NUL non supportata; non verrà modificata",
    )
    return raw


def read_password_file(path: str) -> bytes:
    p = Path(path)
    require(
        p.is_absolute() and p.resolve() == p,
        "Symlink/percorso non canonico nel file password DB",
    )
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        require(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
            "File password DB: richiesto file normale, non hardlink/FIFO",
        )
        require(
            before.st_mode & 0o077 == 0,
            "File password DB troppo accessibile: impostare chmod 600",
        )
        trusted = {0, os.geteuid()}
        require(
            before.st_uid in trusted,
            "Proprietario del file password DB non autorizzato",
        )
        raw = f.read(MAX_PASSWORD_BYTES + 3)
        after = os.fstat(f.fileno())
        require(
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "Il file password DB è cambiato durante la lettura",
        )
    return normalize_password(raw, from_file=True)


def pgpass_record(
    host: str, port: int, database: str, user: str, password: bytes
) -> bytes:
    def escape(value: bytes) -> bytes:
        return value.replace(b"\\", b"\\\\").replace(b":", b"\\:")

    # libpq may match either the socket-directory host or "localhost". This
    # private, per-process passfile only has the configured DB/user/port entry.
    match_host = "*" if host.startswith("/") else host
    fields = [
        match_host.encode(),
        str(port).encode(),
        database.encode(),
        user.encode(),
        password,
    ]
    return b":".join(escape(f) for f in fields) + b"\n"


class Database:
    def __init__(self, cfg: Config, application: str):
        self.cfg, self.application = cfg, application
        self.fks = []
        self.advisory = None
        self._auth_ready = False
        self._auth_dir = None
        self.auth_source = "non inizializzata"

    def prepare_auth(self):
        if self._auth_ready:
            return
        password_path = self.cfg["db_password_file"]
        if password_path:
            password = read_password_file(password_path)
            source = "db_password_file (file host protetto)"
        else:
            # This stdout is captured privately, NEVER displayed or journaled.
            # Do not use docker inspect: it would read every container secret.
            result = checked_run(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.cfg["db_container"],
                    "sh",
                    "-eu",
                    "-c",
                    CONTAINER_PASSWORD_READER,
                ],
                timeout=30,
            )
            kind, separator, raw = result.stdout.partition(b"\n")
            require(
                separator and kind in (b"env", b"file", b"native"),
                "Risposta inattesa nella lettura credenziali del container DB",
            )
            if kind == b"native":
                require(not raw, "Risposta credenziali libpq inattesa")
                self.auth_source = "libpq già configurata (nessuna password trovata nelle variabili POSTGRES)"
                self._auth_ready = True
                return
            password = normalize_password(raw, from_file=(kind == b"file"))
            source = (
                "POSTGRES_PASSWORD_FILE nel container"
                if kind == b"file"
                else "POSTGRES_PASSWORD nel container"
            )
        record = pgpass_record(
            self.cfg["db_host"],
            self.cfg["db_port"],
            self.cfg["db_name"],
            self.cfg["db_user"],
            password,
        )
        # Set before issuing the write so close() also cleans up a failed/uncertain
        # Docker reply. Never include the record itself in a subprocess argument.
        self._auth_dir = "/tmp/immich-still-auth-" + uuid.uuid4().hex
        checked_run(
            [
                "docker",
                "exec",
                "-i",
                self.cfg["db_container"],
                "sh",
                "-eu",
                "-c",
                CREATE_PASSFILE,
                "immich-still-auth",
                self._auth_dir,
            ],
            input_data=record,
            timeout=30,
        )
        self.auth_source = source
        self._auth_ready = True

    def cmd(self, tool, *args):
        self.prepare_auth()
        command = [
            "docker",
            "exec",
            "-i",
            "-e",
            "PGCONNECT_TIMEOUT=10",
            self.cfg["db_container"],
        ]
        # PGHOSTADDR/service must not silently override the configured local DB.
        unset = "unset PGHOSTADDR PGSERVICE PGSERVICEFILE; "
        if self._auth_dir:
            # Replace inherited PGPASSWORD; libpq otherwise prefers it to pgpass.
            unset += 'unset PGPASSWORD; export PGPASSFILE="$1"; shift; '
            command += [
                "sh",
                "-eu",
                "-c",
                unset + 'exec "$@"',
                "immich-still-db",
                self._auth_dir + "/pgpass",
            ]
        else:
            command += ["sh", "-eu", "-c", unset + 'exec "$@"', "immich-still-db"]
        require(tool == "psql", "Questo backend consente soltanto psql in lettura")
        return command + [
            tool,
            "-h",
            self.cfg["db_host"],
            "-p",
            str(self.cfg["db_port"]),
            *args,
        ]

    def psql(self):
        return self.cmd(
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-w",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            self.cfg["db_user"],
            "-d",
            self.cfg["db_name"],
        )

    def connection_check(self):
        try:
            identity = self.query(
                "SELECT jsonb_build_object('database',current_database(),"
                "'user',current_user,'server_address',inet_server_addr(),"
                "'server_port',inet_server_port());"
            )
        except Stop as e:
            raise Stop(
                "Connessione PostgreSQL fallita nel controllo preliminare. "
                "Controlla db_user/db_name/db_host/db_port e la password: "
                "POSTGRES_PASSWORD[_FILE] nel container o db_password_file sullo host. "
                "Nessuna password verrà richiesta interattivamente. Dettaglio: "
                + str(e)
            ) from e
        require(
            isinstance(identity, dict)
            and identity.get("database") == self.cfg["db_name"]
            and identity.get("user") == self.cfg["db_user"],
            "Identità DB diversa dalla configurazione",
        )
        return identity

    def acquire(self):
        self.connection_check()  # Show auth failure, not a misleading advisory-lock error.
        # This session survives between the short psql calls and serializes all runs,
        # including runs using another state directory. EOF releases the lock on crash.
        proc = subprocess.Popen(
            self.psql(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        q = queue.Queue()

        def reader():
            for line in proc.stdout:
                q.put(line.strip())
            q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        proc.stdin.write(
            "SET application_name='" + self.application + "';\n"
            "SELECT 'LOCK:' || pg_try_advisory_lock(492116733)::text;\n"
        )
        proc.stdin.flush()
        try:
            line = q.get(timeout=30)
            require(
                line == "LOCK:true",
                "Un altro migratore detiene il lock PostgreSQL, o lock non acquisibile",
            )
        except BaseException:
            proc.stdin.close()
            proc.wait(timeout=10)
            raise
        self.advisory = proc

    def assert_lock(self):
        require(
            self.advisory is not None and self.advisory.poll() is None,
            "Lock PostgreSQL perso: nessuna nuova scrittura API autorizzata",
        )

    def close(self):
        if self.advisory:
            try:
                self.advisory.stdin.close()
                self.advisory.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.advisory.terminate()
            self.advisory = None
        if self._auth_dir:
            path = self._auth_dir
            try:
                checked_run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        self.cfg["db_container"],
                        "sh",
                        "-eu",
                        "-c",
                        REMOVE_PASSFILE,
                        "immich-still-auth",
                        path,
                    ],
                    timeout=15,
                )
                self._auth_dir = None
            except Exception:
                # This path is not a password. Never mask the original migration error.
                print(
                    "ATTENZIONE: file credenziali temporaneo non eliminato nel container "
                    + self.cfg["db_container"]
                    + ": "
                    + path
                    + "/pgpass. "
                    "Rimuovere quella directory al termine del processo.",
                    file=sys.stderr,
                )
        self._auth_ready = False

    def selection(
        self,
        owner: str,
        limit: int,
        archived: bool,
        asset_id=None,
        *,
        after=None,
        sanity_wall=None,
    ):
        specific = (
            "" if asset_id is None else " AND a.id='" + canonical_id(asset_id) + "'"
        )
        vis = "('timeline','archive')" if archived else "('timeline')"
        cursor = ""
        if after:
            require(isinstance(after, dict), "Cursor di selezione non valido")
            after_id = canonical_id(after["id"])
            after_ts = instant(after["fileCreatedAt"]).isoformat()
            cursor = (
                ' AND (a."fileCreatedAt",a.id) > (\''
                + after_ts
                + "'::timestamptz,'"
                + after_id
                + "')"
            )
        wall = ""
        if sanity_wall:
            wall_ts = instant(str(sanity_wall)).isoformat()
            wall = ' AND a."fileCreatedAt" >= \'' + wall_ts + "'::timestamptz"
        query = f"""
    SELECT coalesce(jsonb_agg(x.id ORDER BY x."fileCreatedAt",x.id), '[]'::jsonb) FROM (
     SELECT a.id,a."fileCreatedAt" FROM public.asset a
     WHERE a."ownerId"='{canonical_id(owner)}' AND a.type='IMAGE' AND a."deletedAt" IS NULL
      AND a."livePhotoVideoId" IS NOT NULL AND a.visibility IN {vis} {specific}{cursor}{wall}
     ORDER BY a."fileCreatedAt",a.id {("LIMIT " + str(limit)) if limit else ""}
    ) x;
    """
        return self.query(query)

    def foreign_refs(self, asset_id: str) -> dict:
        expressions = []
        for f in self.fks:
            if f["schema"] == "public" and f["table"] in (
                "asset",
                "asset_exif",
                "asset_file",
            ):
                continue
            table = ident(f["schema"]) + "." + ident(f["table"])
            key = f["schema"] + "." + f["table"] + "." + f["column"]
            expressions.append(
                f"SELECT {sql_json(key)} AS k, count(*) AS n FROM {table} WHERE {ident(f['column'])}='{canonical_id(asset_id)}'"
            )
        if not expressions:
            return {}
        return self.query(
            "SELECT coalesce(jsonb_object_agg(k #>> '{}',n),'{}'::jsonb) FROM ("
            + " UNION ALL ".join(expressions)
            + ") z;"
        )

    def query(self, sql):
        if self.advisory is not None:
            self.assert_lock()
        require(
            sql.lstrip().upper().startswith("SELECT"),
            "Ammesse solo osservazioni SELECT",
        )
        app = re.sub("[^a-zA-Z0-9_-]", "_", self.application)
        pre = (
            "SET application_name='" + app + "'; SET default_transaction_read_only=on; "
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL TIME ZONE 'UTC'; "
            "SET LOCAL bytea_output='hex'; SET LOCAL statement_timeout='60s'; SET LOCAL lock_timeout='5s';\n"
        )
        r = checked_run(
            self.psql(),
            input_data=(pre + sql.rstrip(";\n ") + ";\nCOMMIT;\n").encode(),
            timeout=self.cfg["db_timeout"],
        )
        text = r.stdout.decode().strip()
        return json.loads(text) if text else None

    def discover(self):
        schema = self.query("""SELECT jsonb_build_object('identity',jsonb_build_object('database',current_database(),
          'system',(SELECT system_identifier::text FROM pg_control_system())),
          'fks',(SELECT coalesce(jsonb_agg(jsonb_build_object('schema',n.nspname,'table',c.relname,
            'column',a.attname,'target',b.attname,'delete',f.confdeltype,'valid',f.convalidated)),'[]'::jsonb)
          FROM pg_constraint f JOIN pg_class c ON c.oid=f.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
          JOIN pg_attribute a ON a.attrelid=f.conrelid AND a.attnum=f.conkey[1]
          JOIN pg_attribute b ON b.attrelid=f.confrelid AND b.attnum=f.confkey[1]
          WHERE f.contype='f' AND f.confrelid='public.asset'::regclass AND cardinality(f.conkey)=1));""")
        self.fks = schema["fks"]
        return schema

    def asset(self, asset_id):
        i = canonical_id(asset_id)
        return self.query(f"""SELECT jsonb_build_object(
          'asset',(SELECT to_jsonb(a) FROM public.asset a WHERE a.id='{i}'),
          'exif',(SELECT to_jsonb(e) FROM public.asset_exif e WHERE e."assetId"='{i}'),
          'files',(SELECT coalesce(jsonb_agg(to_jsonb(f) ORDER BY f.id),'[]'::jsonb) FROM public.asset_file f WHERE f."assetId"='{i}'),
          'job',(SELECT to_jsonb(j) FROM public.asset_job_status j WHERE j."assetId"='{i}'),
          'albums',(SELECT coalesce(jsonb_agg("albumId" ORDER BY "albumId"),'[]'::jsonb) FROM public.album_asset WHERE "assetId"='{i}'),
          'sharedLinks',(SELECT coalesce(jsonb_agg("sharedLinkId" ORDER BY "sharedLinkId"),'[]'::jsonb) FROM public.shared_link_asset WHERE "assetId"='{i}'),
          'tags',(SELECT coalesce(jsonb_agg("tagId" ORDER BY "tagId"),'[]'::jsonb) FROM public.tag_asset WHERE "assetId"='{i}'),
          'metadata',(SELECT coalesce(jsonb_agg(jsonb_build_object('key',key,'value',value) ORDER BY key),'[]'::jsonb) FROM public.asset_metadata WHERE "assetId"='{i}'),
          'faces',(SELECT coalesce(jsonb_agg(to_jsonb(f) ORDER BY f.id),'[]'::jsonb) FROM public.asset_face f WHERE f."assetId"='{i}'),
          'covers',(SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM public.album WHERE "albumThumbnailAssetId"='{i}'),
          'stack',(SELECT to_jsonb(s) FROM public.stack s WHERE s.id=(SELECT "stackId" FROM public.asset WHERE id='{i}'))
        );""")

    def by_checksum(self, owner, checksum):
        require(re.fullmatch("[a-f0-9]{40}", checksum), "SHA1 invalido")
        return self.query(f"""SELECT coalesce(jsonb_agg(id ORDER BY id),'[]'::jsonb) FROM public.asset
          WHERE "ownerId"='{canonical_id(owner)}' AND "libraryId" IS NULL AND checksum=decode('{checksum}','hex');""")

    def video(self, i):
        return self.query(
            f"SELECT to_jsonb(a) FROM public.asset a WHERE id='{canonical_id(i)}';"
        )

    def quota(self, owner):
        return self.query(f"""SELECT jsonb_build_object('stored',u."quotaUsageInBytes",'calculated',
        (SELECT coalesce(sum(e."fileSizeInByte"),0) FROM public.asset a LEFT JOIN public.asset_exif e ON e."assetId"=a.id WHERE a."ownerId"=u.id AND a."libraryId" IS NULL),
        'missingSizes',(SELECT count(*) FROM public.asset a LEFT JOIN public.asset_exif e ON e."assetId"=a.id WHERE a."ownerId"=u.id AND a."libraryId" IS NULL AND e."fileSizeInByte" IS NULL))
        FROM public."user" u WHERE u.id='{canonical_id(owner)}';""")
