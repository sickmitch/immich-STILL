import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_cursor_and_paths as fixtures

from immich_still import cli, journal, workflow
from immich_still.api import ApiError
from immich_still.core import FORMAT, Stop
from immich_still.engine import Engine

FIRST, SECOND = fixtures.FIRST, fixtures.SECOND


class AutoRecoveryTests(unittest.TestCase):
    def test_transient_error_heals_in_same_invocation(self):
        cfg, db, run, engine, pairs = fixtures.RecoveryTests().fixture()
        cfg.data["recovery_attempts"] = 3
        prepared = engine.prepare.return_value
        engine.prepare.side_effect = [OSError("temporary"), prepared]
        with (
            patch.object(workflow.time, "sleep") as sleep,
            patch.object(journal, "save_cursor"),
        ):
            result = cli.process_selection(
                cli.parse([]), cfg, db, run, engine, FIRST, {"trash_days": 30}
            )
        self.assertEqual(result, 0)
        self.assertEqual(engine.prepare.call_count, 2)
        self.assertEqual(pairs[FIRST]["phase"], "completed")
        self.assertEqual(run.info["retry_errors"], {})
        sleep.assert_called_once()

    def test_error_on_one_asset_does_not_block_the_next(self):
        cfg, db, run, engine, pairs = fixtures.RecoveryTests().fixture()
        db.selection.return_value = [FIRST, SECOND]
        db.asset.side_effect = lambda i: fixtures.snapshot(i)
        second_pair = {
            **engine.prepare.return_value[0],
            "id": SECOND,
            "before": fixtures.snapshot(SECOND),
        }
        engine.prepare.side_effect = [
            Stop("metadata differs"),
            (second_pair, b"original", b"still", b"sidecar"),
        ]
        with patch.object(journal, "save_cursor") as save:
            result = cli.process_selection(
                cli.parse([]), cfg, db, run, engine, FIRST, {"trash_days": 30}
            )
        self.assertEqual(result, 3)
        self.assertEqual(pairs[SECOND]["phase"], "completed")
        self.assertIn(FIRST, run.info["retry_errors"])
        self.assertNotIn(FIRST, pairs)
        save.assert_not_called()
        # A later normal recovery resolves the earlier hole and then the cursor
        # catches up through the already completed second item.
        first_pair = {
            **second_pair,
            "id": FIRST,
            "before": fixtures.snapshot(FIRST),
            "phase": "prepared",
        }
        engine.prepare.side_effect = None
        engine.prepare.return_value = (first_pair, b"original", b"still", b"sidecar")
        with patch.object(journal, "save_cursor") as save:
            result = cli.resume_run(
                cli.parse(["--resume", str(run.path)]), cfg, run, engine
            )
        self.assertEqual(result, 0)
        self.assertEqual(
            [call.kwargs["asset_id"] for call in save.call_args_list], [FIRST, SECOND]
        )
        self.assertFalse(run.info["retry_errors"])

    def test_retry_reloads_staged_pair_instead_of_preparing_again(self):
        cfg, db, run, engine, pairs = fixtures.RecoveryTests().fixture()
        cfg.data["recovery_attempts"] = 2

        def uncertain_upload(pair):
            if "target_id" not in pair:
                pair["target_id"] = SECOND
                pair["phase"] = "uploaded"
                run.save_pair(pair)
                raise ApiError("reply lost")
            pair["phase"] = "completed"

        engine.process.side_effect = uncertain_upload
        with patch.object(workflow.time, "sleep"), patch.object(journal, "save_cursor"):
            result = cli.process_selection(
                cli.parse([]), cfg, db, run, engine, FIRST, {"trash_days": 30}
            )
        self.assertEqual(result, 0)
        engine.prepare.assert_called_once()
        engine.stage.assert_called_once()
        self.assertEqual(engine.process.call_count, 2)
        self.assertEqual(pairs[FIRST]["target_id"], SECOND)

    def test_engine_adopts_own_upload_after_lost_reply(self):
        api, db, run = Mock(), Mock(), Mock()
        engine = Engine({}, api, db, run, FIRST)
        engine.guard = Mock()
        pair = {
            "id": FIRST,
            "target_id": None,
            "conversion": {"original_sha1": "a" * 40, "still_sha1": "b" * 40},
            "upload_token": "token",
            "run_id": "run",
            "filename": "still.jpg",
            "before": {
                "asset": {
                    "fileCreatedAt": fixtures.DATE,
                    "fileModifiedAt": fixtures.DATE,
                    "isFavorite": False,
                    "visibility": "timeline",
                },
                "metadata": [],
            },
        }
        db.by_checksum.side_effect = [[], [SECOND]]
        api.upload.side_effect = ApiError("reply lost")
        with self.assertRaises(ApiError):
            engine.adopt_or_upload(pair, b"still", b"sidecar")
        db.asset.return_value = {
            "metadata": [
                {"key": "immich-still-native", "value": engine.provenance(pair)}
            ]
        }
        engine.adopt_or_upload(pair, b"still", b"sidecar")
        self.assertEqual(pair["target_id"], SECOND)
        api.upload.assert_called_once()

    def test_unsafe_asset_is_deferred_without_retry_or_trash(self):
        cfg, db, run, engine, pairs = fixtures.RecoveryTests().fixture()
        cfg.data["recovery_attempts"] = 3
        engine.prepare.side_effect = Stop("source changed")
        with patch.object(workflow.time, "sleep") as sleep:
            result = cli.process_selection(
                cli.parse([]), cfg, db, run, engine, FIRST, {"trash_days": 30}
            )
        self.assertEqual(result, 3)
        engine.prepare.assert_called_once()
        engine.process.assert_not_called()
        sleep.assert_not_called()

    def test_lock_loss_aborts_global_work(self):
        cfg, db, run, engine, pairs = fixtures.RecoveryTests().fixture()
        engine.guard.side_effect = Stop("lock lost")
        with self.assertRaises(Stop):
            cli.process_selection(
                cli.parse([]), cfg, db, run, engine, FIRST, {"trash_days": 30}
            )
        engine.prepare.assert_not_called()

    def test_real_journal_is_reopened_automatically(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            cfg = SimpleNamespace(
                state=Path(directory) / "state",
                path=Path(directory) / "conf.json",
                data={"recovery_attempts": 1},
                public=lambda: {},
            )
            run = journal.Journal(cfg, "convert")
            run.info.update(
                selection=[FIRST], docker={}, database={}, owner=FIRST, status="pending"
            )
            run.save_pair(
                {
                    "id": FIRST,
                    "format": FORMAT,
                    "phase": "staged",
                    "before": fixtures.snapshot(),
                }
            )
            original_path = run.path
            run.close()

            def make_engine(cfg, api, db, run, owner):
                engine = Mock(db=db)

                def complete(pair):
                    pair["phase"] = "completed"
                    run.save_pair(pair)

                engine.process.side_effect = complete
                return engine

            with patch.object(workflow, "Engine", side_effect=make_engine):
                reserved, count, pending = cli.recover_runs(
                    cli.parse([]), cfg, Mock(), Mock(), {}, {}, FIRST
                )
            self.assertEqual((reserved, count, pending), (set(), 1, 0))
            self.assertEqual(
                journal.read(original_path / "run.json")["status"], "completed"
            )
            self.assertEqual(journal.pending_runs(cfg.state), [])

    def test_pending_ids_are_reserved_when_recovery_fails(self):
        cfg, db, run, engine, pairs = fixtures.RecoveryTests().fixture()
        run.info.update(
            selection=[FIRST],
            docker={},
            database={},
            owner=FIRST,
            mode="convert",
            cursor_mode="none",
        )
        engine.prepare.side_effect = Stop("not safe yet")
        with (
            patch.object(journal, "pending_runs", return_value=[(run.path, run.info)]),
            patch.object(journal, "Journal", return_value=run),
            patch.object(workflow, "Engine", return_value=engine),
        ):
            reserved, count, pending = cli.recover_runs(
                cli.parse([]), cfg, Mock(), db, {}, {}, FIRST
            )
        self.assertEqual((reserved, count, pending), ({FIRST}, 1, 1))
        db.selection.return_value = []
        with patch.object(journal, "load_cursor", return_value=None):
            cli.process_selection(
                cli.parse([]),
                cfg,
                db,
                Mock(info={}),
                engine,
                FIRST,
                {"trash_days": 30},
                reserved,
            )
        self.assertEqual(db.selection.call_args.kwargs["excluded"], {FIRST})

    def test_audit_never_recovers_pending_runs(self):
        cfg, db, run, engine, pairs = fixtures.RecoveryTests().fixture()
        cfg.path = Path("/unused/conf.json")
        db.discover.return_value = {"identity": {}}
        api = Mock(mutating=False)
        api.preflight.return_value = {"owner": FIRST, "trash_days": 30}
        with (
            patch.object(cli, "Config", return_value=cfg),
            patch.object(cli, "pin_docker"),
            patch.object(cli, "validate_mount"),
            patch.object(cli, "Api", return_value=api),
            patch.object(cli, "Database", return_value=db),
            patch.object(journal, "Journal", return_value=run),
            patch.object(cli, "Engine", return_value=engine),
            patch.object(cli, "recover_runs") as recover,
            patch.object(cli.os, "umask"),
            patch.object(cli.os, "geteuid", return_value=1000),
        ):
            self.assertEqual(cli.main(["--audit"]), 0)
        recover.assert_not_called()
        engine.process.assert_not_called()

    def test_missing_payloads_are_rebuilt_before_upload(self):
        api, db, run = Mock(), Mock(), Mock()
        engine = Engine({}, api, db, run, FIRST)
        pair = {"format": FORMAT, "owner": FIRST, "id": FIRST, "phase": "staged"}
        db.asset.return_value = {"asset": {"status": "active"}}
        engine.assert_source = Mock()
        engine.payloads = Mock(
            side_effect=[
                FileNotFoundError("still"),
                (b"original", b"still", b"sidecar"),
            ]
        )
        engine.restore_payloads = Mock()
        engine.adopt_or_upload = Mock(side_effect=Stop("stop before network"))
        with self.assertRaises(Stop):
            engine.process(pair)
        engine.restore_payloads.assert_called_once_with(pair)
        engine.adopt_or_upload.assert_called_once_with(pair, b"still", b"sidecar")
        api.trash_source.assert_not_called()

    def test_recorded_trash_is_reconciled_without_repeating_delete(self):
        api, db, run = Mock(), Mock(), Mock()
        engine = Engine({}, api, db, run, FIRST)
        pair = {
            "format": FORMAT,
            "owner": FIRST,
            "id": FIRST,
            "phase": "trash_requested",
            "target_id": SECOND,
            "trash_requested_at": fixtures.DATE,
        }
        db.asset.return_value = {"asset": {"status": "trashed"}}
        engine.assert_source = Mock()
        engine.verify_target = Mock()
        engine.payloads = Mock(side_effect=FileNotFoundError("backup missing"))
        engine.process(pair)
        self.assertEqual(pair["phase"], "completed")
        self.assertIn("trash_verified_at", pair)
        engine.payloads.assert_not_called()
        engine.verify_target.assert_called_once_with(pair)
        api.trash_source.assert_not_called()

    def test_corrupt_cursor_is_archived_and_scan_can_restart(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            root = Path(directory)
            original = b"{incomplete"
            (root / "cursor.json").write_bytes(original)
            self.assertIsNone(journal.recover_cursor(root))
            self.assertFalse((root / "cursor.json").exists())
            backups = list(root.glob("cursor-invalid-*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_cursor_failure_after_skip_does_not_process_incomplete_pair(self):
        cfg, db, run, engine, pairs = fixtures.RecoveryTests().fixture()
        cfg.data["recovery_attempts"] = 2
        from immich_still.gcam import Unsupported

        engine.prepare.side_effect = Unsupported("unsupported profile")
        with (
            patch.object(
                journal,
                "save_cursor",
                side_effect=[OSError("temporary cursor write"), None],
            ),
            patch.object(workflow.time, "sleep"),
        ):
            result = cli.process_selection(
                cli.parse([]), cfg, db, run, engine, FIRST, {"trash_days": 30}
            )
        self.assertEqual(result, 2)
        self.assertEqual(pairs[FIRST]["phase"], "skipped")
        engine.process.assert_not_called()

    def test_payload_rebuild_rejects_changed_source(self):
        engine = Engine({}, Mock(), Mock(), Mock(), FIRST)
        engine.assert_source = Mock(side_effect=Stop("original changed"))
        engine.stage = Mock()
        with self.assertRaises(Stop):
            engine.restore_payloads({"id": FIRST})
        engine.stage.assert_not_called()

    def test_recovery_rejects_another_database_before_processing(self):
        cfg, db, run, engine, pairs = fixtures.RecoveryTests().fixture()
        run.info.update(
            selection=[FIRST],
            docker={},
            database={"system": "other"},
            owner=FIRST,
            mode="convert",
        )
        with (
            patch.object(journal, "pending_runs", return_value=[(run.path, run.info)]),
            patch.object(journal, "Journal", return_value=run),
            patch.object(workflow, "Engine") as constructor,
            self.assertRaises(Stop),
        ):
            cli.recover_runs(cli.parse([]), cfg, Mock(), db, {}, {}, FIRST)
        constructor.assert_not_called()
        run.close.assert_called_once()
