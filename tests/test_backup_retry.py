"""Transient Windows locks and the weekly cycle's completion boundary."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import io
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

from fci_familiar import cli, config


class ReplaceFileRetryTests(unittest.TestCase):
    def test_two_transient_permission_errors_then_success(self):
        source, destination = Path("synthetic.tmp"), Path("synthetic.sqlite3")
        with patch("fci_familiar.config.os.replace", side_effect=[PermissionError("busy"), PermissionError("busy"), None]) as replace, \
             patch("fci_familiar.config.time.sleep") as sleep:
            config.replace_file(source, destination, attempts=3, delay=0)
        self.assertEqual(replace.call_count, 3)
        self.assertTrue(all(call.args == (source, destination) for call in replace.call_args_list))
        self.assertEqual(sleep.call_count, 2)

    def test_persistent_permission_error_is_bounded(self):
        with patch("fci_familiar.config.os.replace", side_effect=PermissionError("still busy")) as replace, \
             patch("fci_familiar.config.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                config.replace_file(Path("synthetic.tmp"), Path("synthetic.sqlite3"), attempts=3, delay=0)
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_missing_source_is_reported_without_permission_retries(self):
        with patch("fci_familiar.config.os.replace", side_effect=FileNotFoundError("missing source")) as replace, \
             patch("fci_familiar.config.time.sleep") as sleep:
            with self.assertRaises(FileNotFoundError):
                config.replace_file(Path("synthetic.tmp"), Path("synthetic.sqlite3"), attempts=3, delay=0)
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()


class WeeklyCompletionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE app_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.addCleanup(self.conn.close)
        self.service = Mock(spec=["sync", "publish", "backup", "connection"])
        self.service.sync.return_value = {"status": "completed"}
        self.service.publish.return_value = {"state": "published", "version": "synthetic"}
        self.service.backup.side_effect = AssertionError("No debe realizar un tercer respaldo redundante")

        @contextmanager
        def connection():
            yield self.conn
        self.service.connection = connection
        self.private = config.Config(data_dir=str(Path.cwd() / "synthetic-private-unused"))

    def marker(self):
        row = self.conn.execute("SELECT value FROM app_meta WHERE key='last_weekly_published'").fetchone()
        return row[0] if row else None

    def invoke_weekly(self):
        with patch("fci_familiar.cli.Config.load", return_value=self.private), \
             patch("fci_familiar.service.Service", return_value=self.service), \
             patch("fci_familiar.cli._print") as output, \
             patch("sys.stdout", new=io.StringIO()):
            cli.main(["--data-dir", self.private.data_dir, "weekly", "--if-due"])
        return output

    def test_unmarked_weekly_cycle_publishes_sets_marker_without_third_backup(self):
        self.assertIsNone(self.marker())
        output = self.invoke_weekly()
        self.service.sync.assert_called_once_with()
        self.service.publish.assert_called_once_with()
        self.service.backup.assert_not_called()
        datetime.fromisoformat(self.marker())
        self.assertEqual(output.call_args.args[0]["publication"]["state"], "published")
        # A successful whole cycle, not merely a successful download, is skipped.
        again = self.invoke_weekly()
        self.service.sync.assert_called_once_with()
        self.service.publish.assert_called_once_with()
        self.assertTrue(again.call_args.args[0]["skipped"])

    def test_failed_publication_does_not_create_completion_marker(self):
        self.service.publish.side_effect = RuntimeError("Publicación de prueba fallida")
        with self.assertRaisesRegex(RuntimeError, "Publicación de prueba fallida"):
            self.invoke_weekly()
        self.assertIsNone(self.marker())
        self.service.backup.assert_not_called()
        self.service.sync.assert_called_once_with()

    def test_unverified_push_does_not_advance_previous_week_marker(self):
        previous = "2000-01-01T00:00:00+00:00"
        self.conn.execute("INSERT INTO app_meta(key,value) VALUES('last_weekly_published',?)", (previous,))
        self.conn.commit()
        self.service.publish.return_value = {"state": "pushed", "version": "synthetic"}
        with self.assertRaisesRegex(RuntimeError, "pendiente"):
            self.invoke_weekly()
        self.assertEqual(self.marker(), previous)
        self.service.backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
