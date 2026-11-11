#!/usr/bin/env python3
"""Native workflow: new verified still -> normal trash of original IMAGE.
Never stop containers, never mutate catalog SQL, never unlink/recreate/purge the motion VIDEO.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import signal
import stat
import sys
from pathlib import Path

from . import core, journal
from .api import Api
from .core import (
    VERSION,
    Config,
    Pending,
    Stop,
    canonical_id,
    checked_run,
    require,
)
from .db import Database
from .engine import Engine
from .workflow import (
    configured_wall,
    process_selection,
    recover_runs,
    resume_run,
    validate_run,
    verify_run,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        allow_abbrev=False,
        epilog="Default: conversione, limite 0; nessun filtro temporale senza sanity_wall nel JSON.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=VERSION + " (native API upload + trash + cursor)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "conf.json",
        help="Configurazione (default: conf.json accanto allo script)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true")
    mode.add_argument(
        "--convert-to-still", "--convert", dest="convert", action="store_true"
    )
    mode.add_argument("--resume", metavar="RUN", type=Path)
    mode.add_argument("--verify", metavar="RUN", type=Path)
    mode.add_argument("--verify-journal", metavar="RUN")
    mode.add_argument("--list-runs", action="store_true")
    mode.add_argument("--check-db", action="store_true")
    parser.add_argument("--asset-id")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Numero massimo di candidati (default: 0, tutti)",
    )
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument(
        "--sanity-wall",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Applica sanity_wall da conf.json se presente; --no-sanity-wall la disabilita",
    )
    parser.add_argument(
        "--non-interactive", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="Stato: override CLI, directory del RUN, native_state_dir nel JSON, oppure state accanto a conf.json",
    )
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="Mostra percorsi e opzioni effettive senza avviare operazioni",
    )
    args = parser.parse_args(argv)
    if not any(
        (
            args.audit,
            args.convert,
            args.resume,
            args.verify,
            args.verify_journal,
            args.list_runs,
            args.check_db,
        )
    ):
        args.convert = True
    if args.limit < 0:
        parser.error("--limit deve essere >= 0")
    if (args.resume or args.verify) and args.asset_id:
        parser.error("Con RUN non si riselezionano asset")
    if args.asset_id:
        try:
            canonical_id(args.asset_id)
        except Stop as e:
            parser.error(str(e))
    return args


def state_override(args):
    run = args.resume or args.verify
    return args.state_dir or (run.expanduser().absolute().parent if run else None)


def show_paths(args):
    config = args.config.expanduser().absolute()
    data = core.configure(config) if config.exists() else {}
    state = core.state_path(config, state_override(args), data)
    source = (
        "--state-dir"
        if args.state_dir
        else "RUN"
        if args.resume or args.verify
        else "native_state_dir"
        if data.get("native_state_dir")
        else "config-directory"
    )

    def describe(path):
        return {
            "path": str(path),
            "resolved": str(path.resolve()),
            "exists": path.exists(),
            "canonical": path == path.resolve(),
        }

    result = {
        "script_directory": str(PROJECT_ROOT),
        "working_directory": str(Path.cwd()),
        "config": describe(config),
        "state": {**describe(state), "source": source},
        "cursor": describe(state / journal.CURSOR_FILE),
        "options": {
            "mode": next(
                name
                for name in (
                    "resume",
                    "verify",
                    "audit",
                    "check_db",
                    "list_runs",
                    "verify_journal",
                    "convert",
                )
                if getattr(args, name)
            ),
            "limit": args.limit,
            "sanity_wall": configured_wall(args, data),
            "include_archived": args.include_archived,
            "asset_id": args.asset_id,
        },
    }
    print(json.dumps(result, indent=2))
    return 0


def pin_docker():
    require(shutil.which("docker"), "Docker CLI non disponibile nel PATH")

    def cmd(*args):
        return checked_run(["docker", *args], timeout=30).stdout.decode().strip()

    if os.environ.get("DOCKER_CONTEXT"):
        context = os.environ["DOCKER_CONTEXT"]
        host = json.loads(cmd("context", "inspect", context))[0]["Endpoints"]["docker"][
            "Host"
        ]
    elif os.environ.get("DOCKER_HOST"):
        host = os.environ["DOCKER_HOST"]
    else:
        host = json.loads(cmd("context", "inspect", cmd("context", "show")))[0][
            "Endpoints"
        ]["docker"]["Host"]
    require(
        host.startswith("unix:///"), "Richiesto Docker locale rootless via socket UNIX"
    )
    socket = Path(host[len("unix://") :])
    require(socket.resolve() == socket, "Socket Docker non canonico")
    st = socket.stat()
    require(
        stat.S_ISSOCK(st.st_mode)
        and st.st_uid == os.geteuid()
        and not st.st_mode & 0o002,
        "Socket Docker non privato/proprio",
    )
    os.environ["DOCKER_HOST"] = host
    for k in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        os.environ.pop(k, None)
    info = json.loads(cmd("info", "--format", "{{json .}}"))
    require(
        "name=rootless" in info.get("SecurityOptions", []), "Daemon Docker non rootless"
    )
    return {
        "endpoint": host,
        "id": info["ID"],
        "security_options": info["SecurityOptions"],
    }


def validate_mount(cfg):
    names = cfg.data["app_containers"]
    require(isinstance(names, list) and names, "app_containers assente")
    result = json.loads(checked_run(["docker", "inspect", *names], timeout=30).stdout)
    for c in result:
        require(
            c.get("State", {}).get("Running"),
            "Immich deve rimanere acceso per questo workflow",
        )
    mounts = [
        m
        for c in result
        for m in c.get("Mounts", [])
        if m["Destination"] == cfg.server_root
    ]
    require(
        mounts and all(Path(m["Source"]).resolve() == cfg.root for m in mounts),
        "Mapping filesystem diverso dal container Immich",
    )
    return [
        {
            "name": c["Name"].lstrip("/"),
            "id": c["Id"],
            "restart_policy": c.get("HostConfig", {}).get("RestartPolicy"),
        }
        for c in result
    ]


def local_mode(args):
    data = core.configure(args.config) if args.config.exists() else {}
    root = core.state_path(args.config, args.state_dir, data)
    if args.list_runs:
        for x in journal.entries(root):
            if x.get("event") == "created":
                print(x["path"])
    else:
        if args.verify_journal == "latest":
            runs = [r for r in journal.entries(root) if r.get("event") == "created"]
            require(runs, "Nessuna run")
            path = Path(runs[-1]["path"])
        else:
            path = Path(args.verify_journal).expanduser().absolute()
        result = journal.proof(path)
        print(json.dumps(result, indent=2))
        journal.write(path / "persistence-recheck.json", result)
    return 0


def main(argv=None):
    args = parse(argv)
    if args.show_paths:
        return show_paths(args)
    os.umask(0o077)
    require(
        os.geteuid() != 0, "Avvia come mike, senza sudo. Non servono privilegi root."
    )
    if args.list_runs or args.verify_journal:
        return local_mode(args)
    old = args.resume or args.verify
    if old:
        old = old.expanduser().absolute()
        journal.proof(old)
    cfg = Config(args.config, state_override(args))
    docker = pin_docker()
    containers = validate_mount(cfg)
    api = Api(cfg, mutating=bool(args.convert or args.resume))
    metadata = api.preflight()
    owner = metadata["owner"]
    db = Database(cfg, "immich_still_native")
    run = None
    try:
        db.acquire()
        identity = db.discover()["identity"]
        if args.check_db:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "database": identity,
                        "auth_source": db.auth_source,
                        "sudo_required": False,
                    },
                    indent=2,
                )
            )
            return 0
        reserved, recovered, pending = set(), 0, 0
        if args.convert:
            journal.recover_cursor(cfg.state)
            reserved, recovered, pending = recover_runs(
                args, cfg, api, db, docker, identity, owner
            )
            if args.limit and recovered >= args.limit:
                return 3 if pending else 0
            args = copy.copy(args)
            if args.limit:
                args.limit -= recovered
        mode = (
            "resume"
            if args.resume
            else "verify"
            if args.verify
            else "convert"
            if args.convert
            else "audit"
        )
        run = journal.Journal(cfg, mode, existing=old)
        if old:
            validate_run(run, docker, identity, owner)
        else:
            run.info.update(
                docker=docker,
                database=identity,
                owner=owner,
                containers=containers,
                server=metadata,
            )
            run.save()
        engine = Engine(cfg, api, db, run, owner)
        if args.verify:
            return verify_run(args, run, engine)
        if args.resume:
            return resume_run(args, cfg, run, engine)
        result = process_selection(
            args, cfg, db, run, engine, owner, metadata, reserved
        )
        return 3 if pending else result
    except (Exception, KeyboardInterrupt) as e:
        if run:
            try:
                if not args.verify:
                    run.info["status"] = (
                        "pending"
                        if api.mutating and bool(run.info.get("selection"))
                        else "aborted"
                    )
                    run.info["error"] = core.redact(str(e))
                    run.save()
                run.log("STOP: " + str(e))
            except Exception:
                pass
        if isinstance(e, KeyboardInterrupt):
            return 130
        if isinstance(e, Pending) or (run and api.mutating):
            return 3
        raise
    finally:
        if run:
            run.close()
        db.close()


def interrupted(sig, frame):
    raise KeyboardInterrupt("Interruzione: recupero automatico al prossimo avvio")


def entrypoint():
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return main()
    except (OSError, RuntimeError, ValueError) as error:
        print(core.brief("ERROR " + str(error)), file=sys.stderr)
        return 1
