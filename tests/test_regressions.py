import contextlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from immich_still import cli, journal
from immich_still.api import Api
from immich_still.core import FORMAT, Stop
from immich_still.db import normalize_password, pgpass_record
from immich_still.gcam import CAM, XMP, Unsupported, convert, parse_jpeg

ASSET_ID = "11111111-1111-4111-8111-111111111111"
DATE = "2026-10-01T12:00:00+00:00"


def microvideo():
    def segment(marker, payload):
        return bytes([255, marker]) + struct.pack(">H", len(payload) + 2) + payload

    video = b"".join(
        struct.pack(">I4s", 8, name) for name in (b"ftyp", b"moov", b"mdat")
    )
    packet = (
        f'<x xmlns:GCamera="{CAM}" GCamera:MicroVideo="1" '
        f'GCamera:MicroVideoVersion="1" GCamera:MicroVideoOffset="{len(video)}" '
        'GCamera:MicroVideoPresentationTimestampUs="0" keep="yes"/>'
    ).encode()
    jpeg = (
        b"\xff\xd8"
        + segment(0xE1, b"Exif\0\0test")
        + segment(0xE1, XMP + packet)
        + segment(0xC0, b"\x08\x00\x02\x00\x03\x01")
        + segment(0xDA, b"\x01")
        + b"pixels\xff\x00more\xff\xd9"
    )
    return jpeg + video


class CodecTests(unittest.TestCase):
    def test_conversion_preserves_scans_and_exif(self):
        original = microvideo()
        still, proof = convert(original)
        self.assertEqual(proof["video_bytes_removed"], 24)
        self.assertEqual(proof["primary_dimensions"], [3, 2])
        self.assertTrue(proof["exif_bit_identical"])
        self.assertNotIn(b"MicroVideo", still)
        self.assertIn(b'keep="yes"', still)
        self.assertEqual(parse_jpeg(still), parse_jpeg(original))
        for start, end in parse_jpeg(original)["scans"]:
            self.assertEqual(still[start:end], original[start:end])

    def test_invalid_input_rejected(self):
        for data in (
            b"",
            microvideo()[:-1],
            microvideo().replace(b'MicroVideo="1"', b'MicroVideo="0"'),
        ):
            with self.subTest(data=data[:20]), self.assertRaises(Unsupported):
                convert(data)


class JournalTests(unittest.TestCase):
    def test_reopen_and_missing_pair_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = SimpleNamespace(
                state=Path(directory) / "state",
                path=Path(directory) / "conf.json",
                public=lambda: {},
                data={},
            )
            with contextlib.redirect_stdout(io.StringIO()):
                j = journal.Journal(cfg, "convert")
                j.info["selection"] = [ASSET_ID]
                j.save_pair({"id": ASSET_ID, "format": FORMAT, "phase": "staged"})
                path = j.path
                j.checkpoint(independent=True)
                j.close()
                j = journal.Journal(cfg, "resume", existing=path)
                try:
                    self.assertEqual(j.pairs()[0]["phase"], "staged")
                    (path / (ASSET_ID + ".json")).unlink()
                    with self.assertRaises((RuntimeError, OSError)):
                        journal.proof(path)
                    with self.assertRaises(RuntimeError):
                        j.pairs()
                finally:
                    j.close()

    def test_cursor_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(journal.load_cursor(root))
            journal.save_cursor(
                root,
                asset_id=ASSET_ID,
                file_created_at=DATE,
                note="completed",
                run_path=root / "run",
            )
            self.assertEqual(journal.load_cursor(root)["id"], ASSET_ID)
            data = json.loads((root / "cursor.json").read_text())
            data["schema"] = "unknown"
            (root / "cursor.json").write_text(json.dumps(data))
            with self.assertRaises(Stop):
                journal.load_cursor(root)


class SafetyTests(unittest.TestCase):
    def test_audit_and_delete_guards(self):
        api = Api(
            {
                "immich_url": "http://localhost:2283",
                "api_key": "test",
                "http_timeout": 1,
            }
        )
        api.opener = Mock()
        with self.assertRaises(Stop):
            api.request("POST", "/assets", {})
        api.mutating = True
        for payload in (
            {"ids": [ASSET_ID], "force": False},
            {"ids": [ASSET_ID], "force": True},
        ):
            with self.assertRaises(Stop):
                api.request("DELETE", "/assets", payload)
        api.allowed_trash_id = ASSET_ID
        with self.assertRaises(Stop):
            api.request("DELETE", "/assets", {"ids": [ASSET_ID], "force": True})
        api.opener.open.assert_not_called()

    def test_password_bytes_are_preserved(self):
        self.assertEqual(
            normalize_password(b" secret \r\n", from_file=True), b" secret "
        )
        self.assertEqual(normalize_password(b" secret ", from_file=False), b" secret ")
        with self.assertRaises(Stop):
            normalize_password(b"secret\n", from_file=False)
        self.assertEqual(
            pgpass_record("/socket", 5432, "db", "user", b"a:b\\c"),
            b"*:5432:db:user:a\\:b\\\\c\n",
        )

    def test_cli_defaults_and_alias(self):
        self.assertTrue(cli.parse([]).convert)
        self.assertTrue(cli.parse(["--convert"]).convert)
        self.assertFalse(cli.parse(["--audit"]).convert)
        self.assertTrue(cli.parse([]).sanity_wall)
        self.assertFalse(cli.parse(["--no-sanity-wall"]).sanity_wall)


class WorkflowTests(unittest.TestCase):
    def selection(self, args):
        cfg = SimpleNamespace(state=Path("/unused"))
        db = Mock()
        db.selection.return_value = [ASSET_ID]
        db.asset.return_value = {"asset": {"id": ASSET_ID, "fileCreatedAt": DATE}}
        db.quota.return_value = {}
        j = Mock(info={}, path=Path("/unused/run"))
        engine = Mock()
        pair = {
            "id": ASSET_ID,
            "phase": "prepared",
            "conversion": {"original_bytes": 100, "still_bytes": 70},
            "filename": "photo.STILL.jpg",
            "existing_still_ids": [],
        }
        engine.prepare.return_value = (pair, b"original", b"still", b"sidecar")
        return cfg, db, j, engine, pair

    def test_audit_does_not_stage_or_advance_cursor(self):
        args = cli.parse(["--audit"])
        cfg, db, j, engine, pair = self.selection(args)
        with patch.object(journal, "save_cursor") as save:
            result = cli.process_selection(
                args, cfg, db, j, engine, ASSET_ID, {"trash_days": 30}
            )
        self.assertEqual(result, 0)
        self.assertEqual(pair["phase"], "audit")
        engine.stage.assert_not_called()
        engine.process.assert_not_called()
        save.assert_not_called()

    def test_success_advances_cursor_after_processing(self):
        args = cli.parse(["--asset-id", ASSET_ID])
        cfg, db, j, engine, pair = self.selection(args)
        events = []
        engine.process.side_effect = lambda pair: events.append("processed")
        with patch.object(
            journal, "save_cursor", side_effect=lambda *a, **k: events.append("cursor")
        ):
            result = cli.process_selection(
                args, cfg, db, j, engine, ASSET_ID, {"trash_days": 30}
            )
        self.assertEqual(result, 0)
        self.assertEqual(events, ["processed", "cursor"])
        self.assertEqual(j.info["status"], "completed")
        self.assertEqual(j.info["skipped"], 0)

    def test_existing_still_is_skipped_without_upload(self):
        args = cli.parse(["--asset-id", ASSET_ID])
        cfg, db, j, engine, pair = self.selection(args)
        pair["existing_still_ids"] = ["existing"]
        with patch.object(journal, "save_cursor") as save:
            result = cli.process_selection(
                args, cfg, db, j, engine, ASSET_ID, {"trash_days": 30}
            )
        self.assertEqual(result, 2)
        self.assertEqual(pair["phase"], "skipped")
        self.assertEqual(save.call_args.kwargs["note"], "skipped-existing-still")
        engine.stage.assert_not_called()
        engine.process.assert_not_called()

    def test_pending_conversion_keeps_cursor(self):
        from immich_still.core import Pending

        args = cli.parse(["--asset-id", ASSET_ID])
        cfg, db, j, engine, pair = self.selection(args)
        engine.process.side_effect = Pending("metadata not ready")
        with patch.object(journal, "save_cursor") as save:
            with self.assertRaises(Pending):
                cli.process_selection(
                    args, cfg, db, j, engine, ASSET_ID, {"trash_days": 30}
                )
        save.assert_not_called()
        engine.stage.assert_called_once()

    def test_skipped_candidate_advances_cursor(self):
        args = cli.parse(["--asset-id", ASSET_ID])
        cfg, db, j, engine, pair = self.selection(args)
        engine.prepare.side_effect = Unsupported("unsupported profile")
        with patch.object(journal, "save_cursor") as save:
            result = cli.process_selection(
                args, cfg, db, j, engine, ASSET_ID, {"trash_days": 30}
            )
        self.assertEqual(result, 2)
        self.assertEqual(j.save_pair.call_args.args[0]["phase"], "skipped")
        self.assertEqual(save.call_args.kwargs["asset_id"], ASSET_ID)
        engine.process.assert_not_called()

    def test_api_failure_is_not_treated_as_skip(self):
        from immich_still.api import ApiError

        args = cli.parse(["--asset-id", ASSET_ID])
        cfg, db, j, engine, pair = self.selection(args)
        engine.prepare.side_effect = ApiError("unavailable")
        with patch.object(journal, "save_cursor") as save, self.assertRaises(ApiError):
            cli.process_selection(
                args, cfg, db, j, engine, ASSET_ID, {"trash_days": 30}
            )
        save.assert_not_called()
        j.save_pair.assert_not_called()

    def test_resume_marks_unstarted_and_respects_limit(self):
        args = cli.parse(["--resume", "/unused/run", "--limit", "1"])
        second = "22222222-2222-4222-8222-222222222222"
        third = "33333333-3333-4333-8333-333333333333"
        pairs = [
            {
                "id": key,
                "phase": "staged",
                "before": {"asset": {"id": key, "fileCreatedAt": DATE}},
            }
            for key in (ASSET_ID, second)
        ]
        j = Mock(
            info={"selection": [ASSET_ID, second, third]}, path=Path("/unused/run")
        )
        j.pairs.side_effect = lambda: list(pairs)
        j.save_pair.side_effect = pairs.append
        engine = Mock()
        engine.process.side_effect = lambda pair: pair.update(phase="completed")
        with patch.object(journal, "save_cursor") as save:
            result = cli.resume_run(
                args, SimpleNamespace(state=Path("/unused")), j, engine
            )
        self.assertEqual(result, 3)
        self.assertEqual(j.info["status"], "pending")
        self.assertEqual(pairs[-1]["id"], third)
        self.assertEqual(pairs[-1]["phase"], "skipped")
        engine.process.assert_called_once_with(pairs[0])
        save.assert_called_once()

    def test_target_verification_failure_never_trashes_source(self):
        from immich_still.engine import Engine

        api, db, j = Mock(), Mock(), Mock()
        db.asset.return_value = {"asset": {"status": "active"}}
        engine = Engine({}, api, db, j, ASSET_ID)
        pair = {"format": FORMAT, "owner": ASSET_ID, "id": ASSET_ID, "phase": "staged"}
        engine.payloads = Mock(return_value=(b"original", b"still", b"sidecar"))
        engine.assert_source = Mock()
        engine.adopt_or_upload = Mock(
            side_effect=lambda *args: pair.update(target_id="target")
        )
        engine.wait_metadata = Mock()
        engine.copy_associations = Mock()
        engine.refresh_thumbnails = Mock()
        engine.verify_target = Mock(side_effect=Stop("target differs"))
        with self.assertRaises(Stop):
            engine.process(pair)
        api.trash_source.assert_not_called()
        api.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
