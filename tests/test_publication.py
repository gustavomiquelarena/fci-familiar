"""Publication/crypto checks in an isolated checkout, with no Git or HTTP IO."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cryptography.exceptions import InvalidTag

from fci_familiar.config import Config
from fci_familiar import crypto, db, publication


PASSWORDS = {"tommy": "uno", "juli": "dos", "pili": "tres"}


def bundle():
    participants = []
    for index, (pid, name) in enumerate((("tommy", "Tommy"), ("juli", "Juli"), ("pili", "Pili"))):
        report = {"participant": {"id": pid, "name": name}, "as_of": "2026-11-08",
            "balance_usd": str(100 + index), "units": str(100 + index),
            "unit_value": "1", "contributions_usd": str(100 + index),
            "withdrawals_usd": "0", "profit_usd": "0", "history": [],
            "movements": [], "allocation": [],
            "test_marker": "synthetic-private-report-marker-" + pid}
        participants.append({"id": pid, "name": name, "report": report})
    return {"schema_version": 1, "version": "synthetic-close-version", "as_of": "2026-11-08",
        "participants": participants, "issues": [], "pending": [],
        "summary": {"total_usd": "303", "total_units": "303", "unit_value": "1", "as_of": "2026-11-08"}}


class CryptoTests(unittest.TestCase):
    def test_each_report_uses_its_own_password(self):
        participants = bundle()["participants"]
        for index, item in enumerate(participants):
            envelope = crypto.encrypt_report(item["report"], PASSWORDS[item["id"]])
            self.assertEqual(crypto.decrypt_report(envelope, PASSWORDS[item["id"]]), item["report"])
            wrong_pid = participants[(index + 1) % len(participants)]["id"]
            with self.assertRaises(InvalidTag):
                crypto.decrypt_report(envelope, PASSWORDS[wrong_pid])
            encoded = json.dumps(envelope)
            self.assertNotIn(item["report"]["test_marker"], encoded)
            self.assertNotIn('"balance_usd"', encoded)
            self.assertEqual(set(envelope), {"version", "kdf", "iterations", "salt", "iv", "ciphertext"})
            self.assertEqual(len(base64.b64decode(envelope["salt"])), 16)
            self.assertEqual(len(base64.b64decode(envelope["iv"])), 12)

    def test_modified_ciphertext_is_rejected(self):
        envelope = crypto.encrypt_report(bundle()["participants"][0]["report"], "simple")
        data = bytearray(base64.b64decode(envelope["ciphertext"]))
        data[0] ^= 1
        envelope["ciphertext"] = base64.b64encode(data).decode("ascii")
        with self.assertRaises(InvalidTag):
            crypto.decrypt_report(envelope, "simple")

    def test_format_and_empty_password_rejected(self):
        with self.assertRaises(ValueError):
            crypto.encrypt_report({}, "")
        envelope = crypto.encrypt_report({}, "simple")
        envelope["version"] = 2
        with self.assertRaises(ValueError):
            crypto.decrypt_report(envelope, "simple")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.repo = self.directory / "public-checkout"
        self.repo.mkdir()
        (self.repo / "docs").mkdir()
        (self.repo / "README.md").write_text("Synthetic project", encoding="utf-8")
        (self.repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        self.config = Config(data_dir=str(self.directory / "private"),
            repository_url="https://github.com/example/family",
            pages_url="https://example.github.io/family/")
        self.conn = db.connect(self.directory / "private" / "test.sqlite3")
        db.initialize(self.conn)
        publication.initialize_publications(self.conn)
        self.root_patch = patch.object(publication, "REPO_ROOT", self.repo)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.conn.close()
        self.temp.cleanup()

    @staticmethod
    def fake_git(*args, **kwargs):
        stdout = ""
        if args[:2] == ("diff", "--cached"):
            stdout = "docs/data/manifest.json\n"
        elif args[:2] == ("rev-parse", "HEAD"):
            stdout = "a" * 40 + "\n"
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    def publication_state(self):
        return self.conn.execute("SELECT state FROM publications ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    def test_stage_contains_only_three_encrypted_reports_and_manifest(self):
        manifest = publication.stage_reports(bundle(), PASSWORDS)
        self.assertEqual(len(manifest["participants"]), 3)
        self.assertEqual(len(list((self.repo / "docs" / "data").glob("*.enc"))), 3)
        publication.assert_public_tree()
        for item in manifest["participants"]:
            envelope = json.loads((self.repo / "docs" / item["file"]).read_text(encoding="utf-8"))
            payload = crypto.decrypt_report(envelope, PASSWORDS[item["id"]])
            self.assertEqual(payload["participant"]["id"], item["id"])
            for other in manifest["participants"]:
                if item["id"] != other["id"]:
                    with self.assertRaises(InvalidTag):
                        crypto.decrypt_report(envelope, PASSWORDS[other["id"]])

    def test_private_file_rejected_even_inside_allowed_source_directory(self):
        (self.repo / "local").mkdir()
        (self.repo / "local" / "private.sqlite3").write_bytes(b"private")
        with self.assertRaises(ValueError):
            publication.assert_public_tree()

    def test_plaintext_report_and_embedded_secret_rejected(self):
        data = self.repo / "docs" / "data"
        data.mkdir()
        report = data / "tommy-invalid.enc"
        report.write_text(json.dumps(bundle()["participants"][0]["report"]), encoding="utf-8")
        with self.assertRaises(ValueError):
            publication.assert_public_tree()
        report.unlink()
        # Construct the synthetic token at runtime so its source is safe to scan.
        (self.repo / "docs" / "example.js").write_text("".join(["gh", "p", "_", "x" * 30]), encoding="utf-8")
        with self.assertRaises(ValueError):
            publication.assert_public_tree()

    def test_no_pages_url_never_claims_publication(self):
        self.config.pages_url = ""
        with patch.object(publication, "_git", side_effect=self.fake_git), \
             patch.object(publication, "ensure_repository"), \
             patch.object(publication, "verify_publication") as verifier:
            result = publication.publish(self.conn, self.config, PASSWORDS, bundle=bundle())
        self.assertEqual(result["state"], "pushed")
        self.assertEqual(self.publication_state(), "pushed")
        self.assertIsNone(publication.current_payload(self.conn))
        verifier.assert_not_called()

    def test_verify_false_never_claims_publication(self):
        with patch.object(publication, "_git", side_effect=self.fake_git), \
             patch.object(publication, "ensure_repository"), \
             patch.object(publication, "verify_publication") as verifier:
            result = publication.publish(self.conn, self.config, PASSWORDS, bundle=bundle(), verify=False)
        self.assertEqual(result["state"], "pushed")
        self.assertEqual(self.publication_state(), "pushed")
        self.assertIsNone(publication.current_payload(self.conn))
        verifier.assert_not_called()

    def test_failed_git_never_claims_publication(self):
        def failed(*args, **kwargs):
            if args[0] == "push":
                raise RuntimeError("Synthetic Git failure")
            return self.fake_git(*args, **kwargs)
        with patch.object(publication, "_git", side_effect=failed), \
             patch.object(publication, "ensure_repository"), \
             patch.object(publication, "verify_publication") as verifier:
            with self.assertRaises(RuntimeError):
                publication.publish(self.conn, self.config, PASSWORDS, bundle=bundle())
        self.assertEqual(self.publication_state(), "failed")
        self.assertIsNone(publication.current_payload(self.conn))
        verifier.assert_not_called()

    def test_only_verified_version_becomes_published(self):
        with patch.object(publication, "_git", side_effect=self.fake_git), \
             patch.object(publication, "ensure_repository"), \
             patch.object(publication, "verify_publication") as verifier:
            result = publication.publish(self.conn, self.config, PASSWORDS, bundle=bundle())
        self.assertEqual(result["state"], "published")
        self.assertEqual(self.publication_state(), "published")
        self.assertEqual(publication.current_payload(self.conn)["version"], result["id"])
        verifier.assert_called_once()

    def test_unchanged_prepare_is_idempotent(self):
        first = publication.publish(self.conn, self.config, PASSWORDS, bundle=bundle(), push=False)
        before = {file.name: file.read_bytes() for file in (self.repo / "docs" / "data").iterdir()}
        second = publication.publish(self.conn, self.config, PASSWORDS, bundle=bundle(), push=False)
        after = {file.name: file.read_bytes() for file in (self.repo / "docs" / "data").iterdir()}
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(before, after)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 1)

    def test_blocking_financial_issue_stops_before_files_are_staged(self):
        value = bundle()
        value["issues"] = [{"severity": "error", "blocking": True, "message": "Synthetic reconciliation"}]
        with self.assertRaises(ValueError):
            publication.publish(self.conn, self.config, PASSWORDS, bundle=value, push=False)
        self.assertFalse((self.repo / "docs" / "data").exists())


class VerificationTests(unittest.TestCase):
    def expected_and_envelope(self):
        envelope = crypto.encrypt_report({"synthetic": "only"}, "simple")
        digest = hashlib.sha256(json.dumps(envelope, sort_keys=True).encode()).hexdigest()[:16]
        expected = {"schema_version": 1, "version": "expected-version", "as_of": "2026-11-08",
                    "participants": [{"id": "tommy", "name": "Tommy", "file": f"data/tommy-{digest}.enc"}]}
        return expected, envelope

    def verify_responses(self, expected, responses):
        streams = [io.BytesIO(json.dumps(value).encode()) for value in responses]
        with patch.object(publication.urllib.request, "urlopen", side_effect=streams), \
             patch.object(publication.time, "monotonic", side_effect=[0, 1]), \
             patch.object(publication.time, "sleep"):
            publication.verify_publication("https://example.github.io/family", expected, timeout_seconds=0)

    def test_old_manifest_version_rejected(self):
        expected, envelope = self.expected_and_envelope()
        wrong = copy.deepcopy(expected)
        wrong["version"] = "old-version"
        with self.assertRaises(RuntimeError):
            self.verify_responses(expected, [wrong])

    def test_wrong_report_ciphertext_rejected(self):
        expected, envelope = self.expected_and_envelope()
        wrong = crypto.encrypt_report({"synthetic": "different report"}, "simple")
        with self.assertRaises(RuntimeError):
            self.verify_responses(expected, [expected, wrong])

    def test_valid_manifest_and_exact_report_accepted(self):
        expected, envelope = self.expected_and_envelope()
        self.verify_responses(expected, [expected, envelope])


if __name__ == "__main__":
    unittest.main()
