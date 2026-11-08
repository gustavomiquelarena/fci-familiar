"""Integration checks without a Windows vault, IOL or a GitHub connection."""
from __future__ import annotations

from contextlib import closing
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from fci_familiar import cli, db, finance, publication
from fci_familiar.config import Config
from fci_familiar.crypto import decrypt_report
from fci_familiar.service import Service


class MemorySecretStore:
    def __init__(self):
        self.values = {}

    def get(self, target):
        if target not in self.values:
            raise KeyError(target)
        return self.values[target]

    def set(self, target, value):
        self.values[target] = value

    def delete(self, target):
        self.values.pop(target, None)


class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        default_root = Path(__file__).resolve().parents[2] / ".fci-test-tmp"
        self.temp_root = Path(os.environ.get("FCI_TEST_TMP", default_root)).resolve()
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix="service-", dir=self.temp_root)).resolve()
        self.public_root = self.base / "public"
        (self.public_root / "docs").mkdir(parents=True)
        self.secrets = MemorySecretStore()
        self.patches = [patch("fci_familiar.service.REPO_ROOT", self.public_root),
                        patch("fci_familiar.publication.REPO_ROOT", self.public_root),
                        patch("fci_familiar.service.SecretStore", return_value=self.secrets)]
        for replacement in self.patches:
            replacement.start()
        self.config = Config(data_dir=str(self.base / "private"),
                             repository_url="https://github.com/example/fondo-familiar",
                             pages_url="https://example.github.io/fondo-familiar")
        self.config.save()
        self.service = Service(self.config)
        self.seed()
        for pid in ("tommy", "juli", "pili"):
            self.secrets.set(self.service.secret_target(pid), "anterior-" + pid)

    def tearDown(self):
        for replacement in reversed(self.patches):
            replacement.stop()
        # The exact recursive target is verified inside the dedicated test root.
        resolved = self.base.resolve()
        self.assertNotEqual(resolved, self.temp_root)
        self.assertTrue(resolved.is_relative_to(self.temp_root))
        for attempt in range(10):
            try:
                shutil.rmtree(resolved)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                # Windows indexers may briefly retain a directory after a close.
                time.sleep(0.1)

    def seed(self):
        with self.service.connection() as conn:
            ids = []
            for pid in ("tommy", "juli", "pili"):
                key = "initial:" + pid
                ids.append(key)
                conn.execute("""INSERT INTO movements(id,effective_at,type,participant_id,amount_usd,
                    units,unit_value,fx,note,status,source_key,created_at,original_amount)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (key, "2025-12-01", "initial", pid, "100", "100", "1", "1000",
                              "PRIVATE_FINANCIAL_SENTINEL", "posted", key,
                              "2025-12-01T00:00:00Z", "100"))
            self.insert_close(conn, "2026-01-01", "300", "300", ids)
            conn.commit()

    @staticmethod
    def insert_close(conn, as_of, total, units, member_ids):
        conn.execute("""INSERT INTO account_snapshots(id,as_of,cash_ars,cash_usd,securities_ars,
            mep,total_usd,units,unit_value,source,raw_hash,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (as_of, as_of, "0", total, "0", "1000", total, units, "1", "test",
                      "hash:" + as_of, json.dumps({"included_movement_ids": member_ids})))

    def record_published(self):
        with self.service.connection() as conn:
            bundle = finance.build_reports(conn)
            bundle["version"] = "original-published"
            conn.execute("""INSERT INTO publications(id,version,as_of,created_at,state,git_sha,error,payload_json)
                VALUES(?,?,?,?,?,?,?,?)""", ("original-published", "original-published", bundle["as_of"],
                "2026-01-01T18:00:00Z", "published", "a" * 40, None, json.dumps(bundle)))
            conn.commit()
        return bundle

    def fake_git(self, *args, **kwargs):
        stdout = ""
        if args[:3] == ("remote", "get-url", "origin"):
            stdout = self.config.repository_url + "\n"
        elif args[:2] == ("rev-parse", "HEAD"):
            stdout = "b" * 40 + "\n"
        elif args[:3] == ("diff", "--cached", "--name-only"):
            stdout = "docs/data/manifest.json\n"
        return subprocess.CompletedProcess(["git", *args], 0, stdout, "")

    def current_manifest(self):
        return json.loads((self.public_root / "docs/data/manifest.json").read_text(encoding="utf-8"))

    def report_for(self, participant, password):
        manifest = self.current_manifest()
        entry = next(item for item in manifest["participants"] if item["id"] == participant)
        envelope = json.loads((self.public_root / "docs" / entry["file"]).read_text(encoding="utf-8"))
        return decrypt_report(envelope, password)

    def test_prepared_report_does_not_promote_pending_password(self):
        self.service.set_password("tommy", "facil", "facil", publish=False)
        result = self.service.publish(push=False)
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(self.secrets.get(self.service.secret_target("tommy")), "anterior-tommy")
        self.assertEqual(self.secrets.get(self.service.secret_target("tommy", True)), "facil")
        self.assertEqual(self.report_for("tommy", "facil")["participant"]["id"], "tommy")
        with self.service.connection() as conn:
            self.assertIsNone(publication.current_payload(conn))

    def test_push_without_live_verification_does_not_promote_password(self):
        self.service.set_password("tommy", "facil", "facil", publish=False)
        with patch("fci_familiar.publication._git", side_effect=self.fake_git):
            result = self.service.publish(verify=False)
        self.assertEqual(result["state"], "pushed")
        self.assertEqual(self.secrets.get(self.service.secret_target("tommy")), "anterior-tommy")
        self.assertEqual(self.secrets.get(self.service.secret_target("tommy", True)), "facil")

    def test_reset_republishes_last_published_close_not_new_local_close(self):
        published = self.record_published()
        self.service.add_movement({"participant_id": "tommy", "amount": "99", "date": "2026-01-02",
                                   "type": "subscription", "request_id": "new-local-flow"})
        with self.service.connection() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM movements WHERE status='posted'")]
            self.insert_close(conn, "2026-01-03", "399", "399", ids)
            conn.commit()
            self.assertEqual(finance.build_reports(conn)["as_of"], "2026-01-03")
        with patch("fci_familiar.publication._git", side_effect=self.fake_git), \
             patch("fci_familiar.publication.verify_publication", return_value=None):
            result = self.service.set_password("tommy", "facil", "facil")
        self.assertEqual(result["state"], "published")
        report = self.report_for("tommy", "facil")
        self.assertEqual(report["as_of"], "2026-01-01")
        self.assertEqual(report["balance_usd"], "100")
        self.assertNotIn("new-local-flow", json.dumps(report))
        self.assertEqual(self.secrets.get(self.service.secret_target("tommy")), "facil")
        self.assertNotIn(self.service.secret_target("tommy", True), self.secrets.values)
        with self.service.connection() as conn:
            self.assertEqual(publication.current_payload(conn)["as_of"], published["as_of"])

    def test_failed_push_keeps_old_password_and_pending_new_password(self):
        self.record_published()
        def failing_git(*args, **kwargs):
            if args and args[0] == "push":
                raise RuntimeError("Push de prueba rechazado")
            return self.fake_git(*args, **kwargs)
        with patch("fci_familiar.publication._git", side_effect=failing_git):
            result = self.service.set_password("tommy", "facil", "facil")
        self.assertEqual(result["state"], "pending")
        self.assertEqual(self.secrets.get(self.service.secret_target("tommy")), "anterior-tommy")
        self.assertEqual(self.secrets.get(self.service.secret_target("tommy", True)), "facil")
        with self.service.connection() as conn:
            self.assertEqual(conn.execute("SELECT state FROM publications ORDER BY rowid DESC LIMIT 1").fetchone()[0], "failed")
            self.assertEqual(publication.current_payload(conn)["version"], "original-published")

    def test_generated_public_files_contain_only_individual_encrypted_reports(self):
        self.service.publish(push=False)
        manifest = self.current_manifest()
        self.assertEqual(len(manifest["participants"]), 3)
        for file in (self.public_root / "docs/data").iterdir():
            raw = file.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE_FINANCIAL_SENTINEL", raw)
            self.assertNotIn("anterior-", raw)
            self.assertNotIn('"balance_usd"', raw)
        for pid in ("tommy", "juli", "pili"):
            decrypted = self.report_for(pid, "anterior-" + pid)
            self.assertEqual(decrypted["participant"]["id"], pid)
            self.assertNotIn("participants", decrypted)
        leak = self.public_root / "docs/data/leak.json"
        leak.write_text('{"balance_usd":"PRIVATE_FINANCIAL_SENTINEL"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            publication.assert_public_tree()

    def test_restore_publication_uses_saved_close_without_changing_ledger(self):
        published = self.record_published()
        self.service.add_movement({"participant_id": "juli", "amount": "10", "date": "2026-01-02",
                                   "type": "subscription", "status": "posted", "request_id": "later"})
        with self.service.connection() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM movements WHERE status='posted'")]
            self.insert_close(conn, "2026-01-03", "310", "310", ids)
            conn.commit()
        with patch("fci_familiar.publication._git", side_effect=self.fake_git), patch("fci_familiar.publication.verify_publication"):
            result = self.service.restore_publication("original-published")
        self.assertEqual(result["state"], "published")
        self.assertEqual(self.report_for("juli", "anterior-juli")["as_of"], published["as_of"])
        with self.service.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM movements").fetchone()[0], 4)
            self.assertEqual(finance.latest_close(conn)["as_of"], "2026-01-03")

    def test_settings_validation_is_atomic(self):
        original_username = self.config.iol_username
        with self.assertRaises(ValueError):
            self.service.update_settings({"iol_username": "changed", "repository_url": "https://invalid.example/repo"})
        self.assertEqual(self.config.iol_username, original_username)
        with self.assertRaises(ValueError):
            self.service.restore_publication("does-not-exist")

    def test_backup_and_restore_retain_financial_state_and_integrity(self):
        self.record_published()
        self.service.add_movement({"participant_id": "juli", "amount": "12.5", "date": "2026-01-02",
                                   "type": "subscription", "status": "pending", "request_id": "pending"})
        result = self.service.backup()
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(len(result["files"]), 2)
        backup = Path(result["files"][0])
        with closing(sqlite3.connect(backup)) as check:
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(check.execute("SELECT count(*) FROM movements").fetchone()[0], 4)
            self.assertEqual(check.execute("SELECT count(*) FROM publications WHERE state='published'").fetchone()[0], 1)
        self.assertEqual(list(backup.parent.glob("*.tmp")), [])
        restored_root = self.base / "restored"
        with patch("sys.stdout", new=io.StringIO()) as output:
            cli.main(["--data-dir", str(restored_root), "restore", str(backup)])
        self.assertEqual(json.loads(output.getvalue())["integrity"], "ok")
        with closing(db.connect(restored_root / "fondo.sqlite3")) as restored:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT count(*) FROM movements").fetchone()[0], 4)
            self.assertEqual(len(finance.build_reports(restored)["pending"]), 1)
        with self.assertRaises(ValueError):
            cli.main(["--data-dir", str(restored_root), "restore", str(backup)])


if __name__ == "__main__":
    unittest.main()
