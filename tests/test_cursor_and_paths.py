import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from immich_still import cli, journal
from immich_still.api import ApiError
from immich_still.core import Skip, Stop
from immich_still.db import Database
from immich_still.engine import ensure_supported

FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"
THIRD = "33333333-3333-4333-8333-333333333333"
DATE = "2026-10-01T12:00:00+00:00"
LATER = "2026-10-02T12:00:00+00:00"
EARLIER = "2020-01-01T12:00:00+00:00"
SCOPE = {"owner": FIRST, "include_archived": False, "sanity_wall": None}


def snapshot(asset_id=FIRST, capture=DATE, uploaded=DATE):
    return {"asset": {"id": asset_id, "fileCreatedAt": capture, "createdAt": uploaded}}


class PathTests(unittest.TestCase):
    def diagnose(self, argv):
        output = io.StringIO()
        with (
            patch.object(cli, "pin_docker") as docker,
            patch.object(cli, "Api") as api,
            patch.object(cli, "Database") as db,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["--show-paths", *argv]), 0)
        docker.assert_not_called()
        api.assert_not_called()
        db.assert_not_called()
        return json.loads(output.getvalue())

    def test_defaults_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(cli, "PROJECT_ROOT", root),
                patch("pathlib.Path.cwd", return_value=root / "elsewhere"),
            ):
                result = self.diagnose([])
            self.assertEqual(result["config"]["path"], str(root / "conf.json"))
            self.assertEqual(result["state"]["path"], str(root / "state"))
            self.assertFalse((root / "state").exists())
            self.assertIsNone(result["options"]["sanity_wall"])
            self.assertEqual(result["options"]["mode"], "convert")
            self.assertEqual(result["options"]["limit"], 0)

    def test_config_paths_wall_and_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "conf.json"
            config.write_text(
                json.dumps(
                    {
                        "api_key": "NEVER-PRINT",
                        "db_password": "PRIVATE",
                        "native_state_dir": "journal",
                        "sanity_wall": DATE,
                    }
                )
            )
            config.chmod(0o600)
            result = self.diagnose(["--config", str(config)])
            self.assertEqual(result["state"]["path"], str(root / "journal"))
            self.assertEqual(result["state"]["source"], "native_state_dir")
            self.assertEqual(result["options"]["sanity_wall"], DATE)
            self.assertNotIn("NEVER-PRINT", json.dumps(result))
            self.assertNotIn("PRIVATE", json.dumps(result))
            override = self.diagnose(
                [
                    "--config",
                    str(config),
                    "--state-dir",
                    str(root / "override"),
                    "--no-sanity-wall",
                ]
            )
            self.assertEqual(override["state"]["path"], str(root / "override"))
            self.assertIsNone(override["options"]["sanity_wall"])
            resumed = self.diagnose(
                ["--config", str(config), "--resume", str(root / "runs" / "run")]
            )
            self.assertEqual(resumed["state"]["path"], str(root / "runs"))
            self.assertEqual(resumed["options"]["mode"], "resume")

    def test_wall_optional_validated_and_utc_normalized(self):
        args = cli.parse([])
        for data in ({}, {"sanity_wall": None}):
            self.assertIsNone(cli.configured_wall(args, data))
        self.assertEqual(
            cli.configured_wall(args, {"sanity_wall": "2026-10-01T14:00:00+02:00"}),
            DATE,
        )
        for value in ("", "not-a-date", "2026-10-01", True, 123):
            with self.subTest(value=value), self.assertRaises(Stop):
                cli.configured_wall(args, {"sanity_wall": value})


class CursorTests(unittest.TestCase):
    def save(self, root, asset_id=FIRST, capture=DATE, uploaded=DATE, **kwargs):
        return journal.save_cursor(
            root,
            asset_id=asset_id,
            file_created_at=capture,
            created_at=uploaded,
            scope=SCOPE,
            note="test",
            run_path=root / "run",
            **kwargs,
        )

    def test_timeline_and_upload_positions_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boundary = {"id": FIRST, "createdAt": DATE}
            self.save(root, discovery_start=boundary)
            # A late upload has an older capture date: recover it without moving
            # the timeline backwards or hiding the next regular candidate.
            self.save(root, SECOND, EARLIER, LATER, recent=True)
            cursor = journal.load_cursor(root)
            self.assertEqual(cursor["id"], FIRST)
            self.assertEqual(cursor["fileCreatedAt"], DATE)
            self.assertEqual(cursor["discovery"], {"id": SECOND, "createdAt": LATER})
            self.save(root, THIRD, LATER, DATE)
            cursor = journal.load_cursor(root)
            self.assertEqual(cursor["id"], THIRD)
            self.assertEqual(cursor["discovery"]["id"], SECOND)
            previous = (root / "cursor.json").read_bytes()
            self.save(root)
            self.assertEqual((root / "cursor.json").read_bytes(), previous)

    def test_legacy_cursor_is_read_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = {
                "schema": journal.LEGACY_CURSOR_SCHEMA,
                "id": FIRST,
                "fileCreatedAt": DATE,
            }
            journal.write(root / "cursor.json", legacy)
            self.assertEqual(journal.load_cursor(root), legacy)
            self.assertEqual(journal.read(root / "cursor.json"), legacy)
            self.save(root, capture=EARLIER)
            self.assertEqual(journal.load_cursor(root)["schema"], journal.CURSOR_SCHEMA)

    def test_query_starts_with_oldest_timeline_without_wall(self):
        db = Database({}, "test")
        db.query = Mock(return_value=[])
        db.selection(FIRST, 0, False)
        sql = db.query.call_args.args[0]
        self.assertIn('ORDER BY a."fileCreatedAt",a.id', sql)
        self.assertNotIn("timestamptz", sql)
        self.assertNotIn("LIMIT", sql)

    def test_late_upload_query_uses_both_positions_and_optional_wall(self):
        db = Database({}, "test")
        db.query = Mock(return_value=[])
        db.selection(
            FIRST,
            3,
            False,
            before={"id": FIRST, "fileCreatedAt": DATE},
            recent_after={"id": SECOND, "createdAt": LATER},
            sanity_wall=EARLIER,
        )
        sql = db.query.call_args.args[0]
        self.assertIn('(a."fileCreatedAt",a.id) <=', sql)
        self.assertIn('(a."createdAt",a.id) >', sql)
        self.assertIn('ORDER BY a."createdAt",a.id', sql)
        self.assertIn('a."fileCreatedAt" >=', sql)
        self.assertIn("LIMIT 3", sql)


class RecoveryTests(unittest.TestCase):
    def fixture(self):
        cfg = SimpleNamespace(state=Path("/unused"), data={"recovery_attempts": 1})
        db = Mock()
        db.selection.return_value = [FIRST]
        db.ingestion_boundary.return_value = {"id": FIRST, "createdAt": DATE}
        db.asset.return_value = snapshot()
        db.quota.return_value = {}
        pairs = {}
        run = Mock(info={}, path=Path("/unused/run"))
        run.save_pair.side_effect = lambda pair: pairs.__setitem__(pair["id"], pair)
        run.pairs.side_effect = lambda: list(pairs.values())
        engine = Mock(db=db)
        pair = {
            "id": FIRST,
            "phase": "prepared",
            "before": snapshot(),
            "filename": "still.jpg",
            "existing_still_ids": [],
            "conversion": {"original_bytes": 10, "still_bytes": 5},
        }
        engine.prepare.return_value = (pair, b"original", b"still", b"sidecar")
        engine.stage.side_effect = lambda pair, *payload: run.save_pair(pair)
        engine.process.side_effect = lambda pair: pair.update(phase="completed")
        return cfg, db, run, engine, pairs

    def test_io_and_consistency_errors_do_not_skip_or_advance(self):
        for error in (OSError("temporary"), Stop("changed"), ApiError("unreachable")):
            with self.subTest(error=type(error).__name__):
                cfg, db, run, engine, pairs = self.fixture()
                engine.prepare.side_effect = error
                with (
                    patch.object(journal, "save_cursor") as save,
                ):
                    self.assertEqual(
                        cli.process_selection(
                            cli.parse([]),
                            cfg,
                            db,
                            run,
                            engine,
                            FIRST,
                            {"trash_days": 30},
                        ),
                        3,
                    )
                self.assertFalse(pairs)
                save.assert_not_called()

    def test_manual_conversion_and_resume_never_change_shared_cursor(self):
        cfg, db, run, engine, pairs = self.fixture()
        with patch.object(journal, "save_cursor") as save:
            cli.process_selection(
                cli.parse(["--asset-id", FIRST]),
                cfg,
                db,
                run,
                engine,
                FIRST,
                {"trash_days": 30},
            )
            cli.resume_run(cli.parse(["--resume", str(run.path)]), cfg, run, engine)
        save.assert_not_called()
        self.assertEqual(run.info["cursor_mode"], "none")

    def test_failed_preparation_is_retried_on_resume(self):
        cfg, db, run, engine, pairs = self.fixture()
        engine.prepare.side_effect = OSError("temporary")
        with patch.object(journal, "save_cursor") as save:
            self.assertEqual(
                cli.process_selection(
                    cli.parse([]), cfg, db, run, engine, FIRST, {"trash_days": 30}
                ),
                3,
            )
            save.assert_not_called()
            engine.prepare.side_effect = None
            result = cli.resume_run(
                cli.parse(["--resume", str(run.path)]), cfg, run, engine
            )
        self.assertEqual(result, 0)
        self.assertEqual(pairs[FIRST]["phase"], "completed")
        engine.prepare.assert_called_with(FIRST)
        save.assert_called_once()

    def test_resume_limit_counts_unfinished_items(self):
        cfg, db, run, engine, pairs = self.fixture()
        run.info.update(selection=[FIRST, SECOND], cursor_mode="none")
        pairs[FIRST] = {"id": FIRST, "phase": "completed", "before": snapshot()}
        pairs[SECOND] = {"id": SECOND, "phase": "staged", "before": snapshot(SECOND)}
        result = cli.resume_run(
            cli.parse(["--resume", str(run.path), "--limit", "1"]), cfg, run, engine
        )
        self.assertEqual(result, 0)
        engine.process.assert_called_once_with(pairs[SECOND])

    def test_new_scope_or_legacy_cursor_rescans_from_start(self):
        for previous in (
            {
                "schema": journal.LEGACY_CURSOR_SCHEMA,
                "id": FIRST,
                "fileCreatedAt": DATE,
            },
            {
                "schema": journal.CURSOR_SCHEMA,
                "id": FIRST,
                "fileCreatedAt": DATE,
                "scope": {**SCOPE, "sanity_wall": DATE},
                "discovery": None,
            },
        ):
            with self.subTest(previous=previous):
                cfg, db, run, engine, pairs = self.fixture()
                db.selection.return_value = []
                with patch.object(journal, "load_cursor", return_value=previous):
                    self.assertEqual(
                        cli.process_selection(
                            cli.parse([]),
                            cfg,
                            db,
                            run,
                            engine,
                            FIRST,
                            {"trash_days": 30},
                        ),
                        0,
                    )
                self.assertIsNone(db.selection.call_args.kwargs["after"])
                db.ingestion_boundary.assert_called_once_with(FIRST)

    def test_late_candidates_share_total_limit_and_preserve_timeline(self):
        cfg, db, run, engine, pairs = self.fixture()
        previous = {
            "schema": journal.CURSOR_SCHEMA,
            "id": THIRD,
            "fileCreatedAt": LATER,
            "scope": SCOPE,
            "discovery": {"id": THIRD, "createdAt": DATE},
        }
        with (
            patch.object(journal, "load_cursor", return_value=previous),
            patch.object(journal, "save_cursor") as save,
        ):
            cli.process_selection(
                cli.parse(["--limit", "1"]),
                cfg,
                db,
                run,
                engine,
                FIRST,
                {"trash_days": 30},
            )
        db.selection.assert_called_once()
        self.assertEqual(db.selection.call_args.kwargs["before"], previous)
        self.assertTrue(save.call_args.kwargs["recent"])
        self.assertEqual(run.info["late_selection"], [FIRST])

    def test_missing_pair_failure_leaves_run_pending_in_main(self):
        cfg, db, run, engine, pairs = self.fixture()
        cfg.path = Path("/unused/conf.json")
        db.discover.return_value = {"identity": {}}
        api = Mock(mutating=True)
        api.preflight.return_value = {"owner": FIRST, "trash_days": 30}
        engine.prepare.side_effect = OSError("temporary")
        with (
            patch.object(cli, "Config", return_value=cfg),
            patch.object(cli, "pin_docker"),
            patch.object(cli, "validate_mount"),
            patch.object(cli, "Api", return_value=api),
            patch.object(cli, "Database", return_value=db),
            patch.object(journal, "Journal", return_value=run),
            patch.object(cli, "Engine", return_value=engine),
            patch.object(cli.os, "umask"),
            patch.object(cli.os, "geteuid", return_value=1000),
        ):
            self.assertEqual(cli.main([]), 3)
        self.assertEqual(run.info["status"], "pending")
        self.assertFalse(pairs)
        db.close.assert_called_once()
        run.close.assert_called_once()

    def test_offline_is_retryable_not_a_permanent_skip(self):
        source = {
            "asset": {"type": "IMAGE", "isOffline": True},
            "exif": {"fileSizeInByte": 10},
        }
        with self.assertRaises(Stop) as error:
            ensure_supported(source, {})
        self.assertNotIsInstance(error.exception, Skip)


if __name__ == "__main__":
    unittest.main()
