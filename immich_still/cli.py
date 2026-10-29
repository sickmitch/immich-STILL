#!/usr/bin/env python3
"""Native workflow: new verified still -> normal trash of original IMAGE.
Never stop containers, never mutate catalog SQL, never unlink/recreate/purge the motion VIDEO.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import sys
from pathlib import Path

from . import core, journal
from .api import Api, ApiError
from .core import (
    FORMAT,
    VERSION,
    Config,
    Pending,
    Stop,
    canonical_id,
    checked_run,
    compact,
    instant,
    require,
)
from .db import Database
from .engine import Engine
from .gcam import Unsupported

SANITY_WALL_TS = "2026-09-23T00:00:00+00:00"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
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
        help="Limita la selezione a fileCreatedAt >= 2026-09-23T00:00:00Z (attivo di default)",
    )
    parser.add_argument(
        "--non-interactive", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--state-dir", type=Path)
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


def candidate_marker(snapshot):
    asset = (snapshot or {}).get("asset") or {}
    require(
        asset.get("id") and asset.get("fileCreatedAt"),
        "Snapshot asset incompleto per il cursor",
    )
    return {
        "id": canonical_id(asset["id"]),
        "fileCreatedAt": instant(asset["fileCreatedAt"]).isoformat(),
    }


def advance_cursor(args, cfg, run, marker, note):
    if args.convert or args.resume:
        journal.save_cursor(
            cfg.state,
            asset_id=marker["id"],
            file_created_at=marker["fileCreatedAt"],
            note=note,
            run_path=run.path,
        )


def verify_run(args, run, engine):
    pairs = run.pairs()
    pairs = pairs[: args.limit] if args.limit else pairs
    for pair in pairs:
        if pair.get("phase") == "skipped":
            continue
        run.log("VERIFICA: " + compact(engine.verify(pair)))
    return 0


def resume_run(args, cfg, run, engine):
    durable = {pair["id"] for pair in run.pairs()}
    for source_id in run.info.get("selection", []):
        if source_id not in durable:
            run.save_pair(
                {
                    "id": source_id,
                    "format": FORMAT,
                    "phase": "skipped",
                    "reason": "Non iniziato prima dell’interruzione; originale non toccato",
                }
            )
    pairs = run.pairs()
    pairs = pairs[: args.limit] if args.limit else pairs
    for pair in pairs:
        if pair.get("phase") in ("skipped", "audit"):
            continue
        engine.process(pair)
        marker = candidate_marker(pair["before"])
        advance_cursor(args, cfg, run, marker, "resume-completed")
    pairs = run.pairs()
    complete = len(pairs) == len(run.info["selection"]) and all(
        pair.get("phase") in ("completed", "skipped") for pair in pairs
    )
    run.info["status"] = "completed" if complete else "pending"
    run.save()
    return 0 if run.info["status"] == "completed" else 3


def process_candidate(args, cfg, db, run, engine, asset_id):
    """Process one candidate; return whether it was deliberately skipped."""
    source_snapshot = db.asset(asset_id)
    marker = candidate_marker(source_snapshot)
    try:
        pair, original, still, sidecar = engine.prepare(asset_id)
    except ApiError:
        raise
    except (Unsupported, Stop, OSError) as e:
        run.save_pair(
            {
                "id": asset_id,
                "format": FORMAT,
                "phase": "skipped",
                "reason": core.redact(str(e)),
            }
        )
        run.log("SKIP " + asset_id + ": " + str(e))
        advance_cursor(args, cfg, run, marker, "skipped")
        return True
    run.log(
        "CANDIDATO "
        + asset_id
        + ": "
        + str(pair["conversion"]["original_bytes"])
        + " -> "
        + str(pair["conversion"]["still_bytes"])
        + " byte; nuova still "
        + pair["filename"]
    )
    if pair["existing_still_ids"]:
        pair["phase"] = "skipped"
        pair["reason"] = (
            "Still già presente con checksum identico: nessun riuso automatico in questa release"
        )
        run.save_pair(pair)
        run.log(
            "SKIP "
            + asset_id
            + ": still già presente "
            + compact(pair["existing_still_ids"])
        )
        advance_cursor(args, cfg, run, marker, "skipped-existing-still")
        return True
    if not args.convert:
        pair["phase"] = "audit"
        run.save_pair(pair)
        return False
    engine.stage(pair, original, still, sidecar)
    engine.process(pair)
    advance_cursor(args, cfg, run, marker, "completed")
    return False


def process_selection(args, cfg, db, run, engine, owner, metadata):
    if args.convert:
        run.unresolved_other_runs()
    after = None
    if args.convert and not args.asset_id:
        after = journal.load_cursor(cfg.state)
        if after:
            run.log(
                "Cursor: riparto dopo " + after["id"] + " @ " + after["fileCreatedAt"]
            )
    sanity_wall = SANITY_WALL_TS if args.sanity_wall else None
    if sanity_wall:
        run.log("Sanity wall attiva: asset con fileCreatedAt >= " + SANITY_WALL_TS)
    ids = db.selection(
        owner,
        args.limit,
        args.include_archived,
        args.asset_id,
        after=after,
        sanity_wall=sanity_wall,
    )
    run.info["selection"] = ids
    run.info["selection_cursor_after"] = after
    run.info["sanity_wall"] = sanity_wall
    run.save()
    run.log("Quota (sola osservazione): " + compact(db.quota(owner)))
    run.log(
        "Nessun downtime / SQL di modifica / sudo. Cestino nativo: "
        + str(metadata["trash_days"])
        + " giorni."
    )
    if not ids:
        run.info["status"] = "completed" if args.convert else "audit"
        run.save()
        return 2 if args.asset_id else 0
    skipped = 0
    for asset_id in ids:
        skipped += process_candidate(args, cfg, db, run, engine, asset_id)
    run.info["status"] = "completed" if args.convert else "audit"
    run.info["skipped"] = skipped
    run.save()
    run.checkpoint(independent=True)
    return 2 if skipped else 0


def main(argv=None):
    args = parse(argv)
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
    cfg = Config(args.config, args.state_dir or (old.parent if old else None))
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
            require(
                run.info.get("docker") == docker
                and run.info.get("database") == identity
                and run.info.get("owner") == owner,
                "Journal riferito a altro Docker/database/utente",
            )
            require(
                run.info.get("mode") == "convert",
                "Questo RUN non è una conversione nativa",
            )
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
        return process_selection(args, cfg, db, run, engine, owner, metadata)
    except (Exception, KeyboardInterrupt) as e:
        if run:
            try:
                if not args.verify:
                    run.info["status"] = (
                        "pending"
                        if api.mutating
                        and any(pair.get("phase") != "skipped" for pair in run.pairs())
                        else "aborted"
                    )
                    run.info["error"] = core.redact(str(e))
                    run.save()
                run.log("STOP: " + str(e))
                run.log("Immich non è stato fermato; nessun DELETE viene forzato.")
                if api.mutating:
                    import shlex

                    command = [
                        str(PROJECT_ROOT / "immich-still.sh"),
                        "--config",
                        str(cfg.path),
                        "--resume",
                        str(run.path),
                        "--limit",
                        "0",
                        "--non-interactive",
                    ]
                    run.log("RIPRESA: " + shlex.join(command))
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
    raise KeyboardInterrupt("Interruzione: riprendere il journal")


def entrypoint():
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return main()
    except (OSError, RuntimeError, ValueError) as error:
        print("ERRORE: " + core.redact(str(error)), file=sys.stderr)
        return 1
