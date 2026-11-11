"""Selection and automatic recovery of durable conversion jobs."""

from __future__ import annotations

import copy
import time

from . import core, journal
from .api import ApiError
from .core import FORMAT, Pending, Skip, Stop, canonical_id, instant, require
from .engine import Engine
from .gcam import Unsupported


def configured_wall(args, data):
    value = data.get("sanity_wall")
    if not args.sanity_wall or value is None:
        return None
    require(
        isinstance(value, str),
        "sanity_wall deve essere una data ISO con fuso oppure null",
    )
    return instant(value).isoformat()


def candidate_marker(snapshot):
    asset = (snapshot or {}).get("asset") or {}
    require(
        asset.get("id") and asset.get("fileCreatedAt") and asset.get("createdAt"),
        "Snapshot asset incompleto per il cursor",
    )
    return {
        "id": canonical_id(asset["id"]),
        "fileCreatedAt": instant(asset["fileCreatedAt"]).isoformat(),
        "createdAt": instant(asset["createdAt"]).isoformat(),
    }


def advance_cursor(args, cfg, run, marker, note):
    if run.info.get("cursor_blocked"):
        return
    if run.info.get("cursor_mode") == journal.CURSOR_SCHEMA and (
        args.convert or args.resume
    ):
        if args.resume:
            previous = journal.load_cursor(cfg.state)
            if previous and previous.get("scope") != run.info["selection_scope"]:
                return
            if not previous and marker["id"] in run.info.get("late_selection", []):
                return
        journal.save_cursor(
            cfg.state,
            asset_id=marker["id"],
            file_created_at=marker["fileCreatedAt"],
            created_at=marker["createdAt"],
            scope=run.info["selection_scope"],
            discovery_start=run.info.get("discovery_start"),
            recent=marker["id"] in run.info.get("late_selection", []),
            note=note,
            run_path=run.path,
        )


def verify_run(args, run, engine):
    pairs = run.pairs()
    pairs = pairs[: args.limit] if args.limit else pairs
    for pair in pairs:
        if pair.get("phase") == "skipped":
            continue
        engine.verify(pair)
        run.log("VERIFY " + pair["id"] + " ok")
    return 0


def retryable(error):
    if isinstance(error, ApiError):
        return error.status is None or error.status in (408, 429) or error.status >= 500
    return isinstance(error, (OSError, Pending))


def process_item(args, cfg, run, engine, asset_id, pair=None):
    """Retry from durable state; never repeat an uncertain upload blindly."""
    attempts = cfg.data.get("recovery_attempts", 3)
    delay = cfg.data.get("recovery_delay", 5)
    for attempt in range(1, attempts + 1):
        try:
            engine.guard()
            if pair is None:
                skipped = process_candidate(args, cfg, engine.db, run, engine, asset_id)
            elif pair.get("phase") in ("completed", "skipped"):
                marker = (
                    candidate_marker(pair["before"])
                    if pair["phase"] == "completed"
                    else pair.get("cursor_marker")
                )
                if marker:
                    advance_cursor(args, cfg, run, marker, "recovered")
                skipped = pair["phase"] == "skipped"
            else:
                engine.process(pair)
                advance_cursor(
                    args, cfg, run, candidate_marker(pair["before"]), "recovered"
                )
                skipped = False
            run.info.setdefault("retry_errors", {}).pop(asset_id, None)
            run.save()
            if not skipped:
                run.log("OK " + asset_id)
            return skipped
        except (Stop, OSError, Unsupported) as error:
            # Persistence and lock failures are global: continuing could lose
            # the only evidence that authorizes adoption of an uploaded asset.
            engine.guard()
            run.info.setdefault("retry_errors", {})[asset_id] = {
                "error": core.redact(str(error)),
                "at": core.timestamp(),
                "attempt": attempt,
            }
            run.info["status"] = "pending"
            run.save()
            if attempt == attempts or not retryable(error):
                # Retain the selection in this journal. Other assets may run,
                # but the shared cursor cannot jump over this unfinished item.
                run.info["cursor_blocked"] = True
                run.save()
                run.log("PENDING " + asset_id + " " + str(error))
                return None
            time.sleep(min(delay * attempt, 60))
            pair = next((p for p in run.pairs() if p["id"] == asset_id), None)
    raise AssertionError("Numero tentativi non valido")


def resume_run(args, cfg, run, engine):
    pairs = {pair["id"]: pair for pair in run.pairs()}
    processed = 0
    run.info["cursor_blocked"] = False
    for asset_id in run.info.get("selection", []):
        pair = pairs.get(asset_id)
        phase = (pair or {}).get("phase")
        if phase in ("completed", "skipped", "audit"):
            # A completed pair already passed verification before being saved.
            # User edits after completion must not block unrelated recovery.
            marker = (
                candidate_marker(pair["before"])
                if phase == "completed"
                else pair.get("cursor_marker")
            )
            if marker:
                advance_cursor(args, cfg, run, marker, "recovered")
            run.info.setdefault("retry_errors", {}).pop(asset_id, None)
            continue
        if args.limit and processed >= args.limit:
            break
        process_item(args, cfg, run, engine, asset_id, pair)
        processed += 1
    pairs = run.pairs()
    complete = (
        len(pairs) == len(run.info["selection"])
        and all(pair.get("phase") in ("completed", "skipped") for pair in pairs)
        and not run.info.get("retry_errors")
    )
    run.info["status"] = "completed" if complete else "pending"
    run.info["attempted"] = processed
    if complete:
        run.info.pop("error", None)
    run.save()
    pending = sum(pair.get("phase") not in ("completed", "skipped") for pair in pairs)
    pending += len(run.info["selection"]) - len(pairs)
    run.log(f"RECOVER {run.path.name} tried={processed} pending={pending}")
    return 0 if complete else 3


def validate_run(run, docker, identity, owner):
    require(
        run.info.get("docker") == docker
        and run.info.get("database") == identity
        and run.info.get("owner") == owner,
        "Journal riferito a altro Docker/database/utente",
    )
    require(
        run.info.get("mode") == "convert", "Questo RUN non è una conversione nativa"
    )


def recover_runs(args, cfg, api, db, docker, identity, owner):
    reserved = set()
    attempted = 0
    pending = 0
    for path, info in journal.pending_runs(cfg.state):
        # Never select the same source in a second journal while the first
        # may own an uploaded target, even when the per-invocation limit is used.
        if args.limit and attempted >= args.limit:
            reserved.update(info.get("selection", []))
            pending += 1
            continue
        run = journal.Journal(cfg, "resume", existing=path)
        try:
            validate_run(run, docker, identity, owner)
            recovery_args = copy.copy(args)
            recovery_args.resume = path
            recovery_args.limit = args.limit - attempted if args.limit else 0
            engine = Engine(cfg, api, db, run, owner)
            result = resume_run(recovery_args, cfg, run, engine)
            attempted += run.info["attempted"]
            if result:
                pending += 1
                reserved.update(run.info.get("selection", []))
        finally:
            run.close()
    return reserved, attempted, pending


def process_candidate(args, cfg, db, run, engine, asset_id):
    """Process one candidate; return whether it was deliberately skipped."""
    source_snapshot = db.asset(asset_id)
    marker = candidate_marker(source_snapshot)
    try:
        pair, original, still, sidecar = engine.prepare(asset_id)
    except (Unsupported, Skip) as e:
        run.save_pair(
            {
                "id": asset_id,
                "format": FORMAT,
                "phase": "skipped",
                "cursor_marker": marker,
                "reason": core.redact(str(e)),
            }
        )
        run.log("SKIP " + asset_id + ": " + str(e))
        advance_cursor(args, cfg, run, marker, "skipped")
        return True
    marker = candidate_marker(pair["before"])
    if pair["existing_still_ids"]:
        pair["phase"] = "skipped"
        pair["cursor_marker"] = marker
        pair["reason"] = (
            "Still già presente con checksum identico: nessun riuso automatico in questa release"
        )
        run.save_pair(pair)
        run.log("SKIP " + asset_id + " duplicate")
        advance_cursor(args, cfg, run, marker, "skipped-existing-still")
        return True
    if not (args.convert or args.resume):
        pair["phase"] = "audit"
        run.save_pair(pair)
        return False
    engine.stage(pair, original, still, sidecar)
    engine.process(pair)
    advance_cursor(args, cfg, run, marker, "completed")
    return False


def process_selection(args, cfg, db, run, engine, owner, metadata, reserved=()):
    sanity_wall = configured_wall(args, cfg.data)
    scope = {
        "owner": owner,
        "include_archived": args.include_archived,
        "sanity_wall": sanity_wall,
    }
    run.info["cursor_mode"] = (
        journal.CURSOR_SCHEMA if args.convert and not args.asset_id else "none"
    )
    run.info["selection_scope"] = scope
    after = None
    late_ids = []
    if run.info["cursor_mode"] == journal.CURSOR_SCHEMA:
        previous = journal.load_cursor(cfg.state)
        if (
            previous
            and previous["schema"] == journal.CURSOR_SCHEMA
            and previous.get("scope") == scope
        ):
            after = previous
            # Recover newer uploads that fall behind the timeline cursor.
            late_ids = db.selection(
                owner,
                args.limit,
                args.include_archived,
                before=after,
                recent_after=after.get("discovery"),
                sanity_wall=sanity_wall,
                excluded=reserved,
            )
        # Capture the boundary BEFORE selecting; uploads during the scan must
        # remain discoverable, even when their capture date is older.
        run.info["discovery_start"] = (
            after.get("discovery") if after else db.ingestion_boundary(owner)
        )
    remaining = args.limit - len(late_ids) if args.limit else 0
    ids = (
        []
        if args.limit and remaining <= 0
        else db.selection(
            owner,
            remaining,
            args.include_archived,
            args.asset_id,
            after=after,
            sanity_wall=sanity_wall,
            excluded=reserved,
        )
    )
    run.info["late_selection"] = late_ids
    ids = late_ids + ids
    run.info["selection"] = ids
    run.info["selection_cursor_after"] = after
    run.info["sanity_wall"] = sanity_wall
    run.save()
    if not ids:
        run.info["status"] = "completed" if args.convert else "audit"
        run.save()
        run.log(f"END {run.path.name} n=0")
        return 2 if args.asset_id else 0
    skipped = 0
    deferred = 0
    for asset_id in ids:
        if args.convert:
            result = process_item(args, cfg, run, engine, asset_id)
            deferred += result is None
            skipped += result is True
        else:
            skipped += process_candidate(args, cfg, db, run, engine, asset_id)
    run.info["status"] = (
        "pending" if deferred else "completed" if args.convert else "audit"
    )
    run.info["skipped"] = skipped
    run.info["deferred"] = deferred
    run.save()
    run.checkpoint(independent=True)
    label = "END" if args.convert else "AUDIT"
    run.log(f"{label} {run.path.name} n={len(ids)} skip={skipped} pending={deferred}")
    return 3 if deferred else 2 if skipped else 0
