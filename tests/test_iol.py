"""Synthetic integration checks; never reads a real vault or contacts IOL."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from fci_familiar import db
from fci_familiar.credentials import CredentialError, SecretStore, load_credentials
from fci_familiar.iol import IOLClient, IOLDataError, IOLHTTPError
from fci_familiar.sync import (historical_closes, normalize_account,
                               normalize_portfolio, sync_account)


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self):
        self.closed = True


class Session:
    def __init__(self, posts=None, gets=None):
        self.posts = list(posts or [])
        self.gets = list(gets or [])
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.posts.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        result = self.gets.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "encrypted_config.ini"
        self.key = Fernet.generate_key()

    def tearDown(self):
        self.directory.cleanup()

    def encrypted(self, data):
        self.path.write_bytes(Fernet(self.key).encrypt(json.dumps(data).encode()))

    def test_existing_format(self):
        self.encrypted({"IOL": "synthetic-password"})
        with patch.object(SecretStore, "get_bytes", return_value=self.key) as vault:
            self.assertEqual(load_credentials("IOL", self.path), "synthetic-password")
            vault.assert_called_once_with("secret_key")

    def test_utf16_master_key_compatibility(self):
        self.encrypted({"IOL": "synthetic"})
        with patch.object(SecretStore, "get_bytes", return_value=self.key.decode().encode("utf-16-le")):
            self.assertEqual(load_credentials("IOL", self.path), "synthetic")

    def test_wrong_key_and_altered_file(self):
        self.encrypted({"IOL": "synthetic"})
        with patch.object(SecretStore, "get_bytes", return_value=Fernet.generate_key()):
            with self.assertRaises(CredentialError):
                load_credentials("IOL", self.path)
        self.path.write_bytes(b"altered")
        with patch.object(SecretStore, "get_bytes", return_value=self.key):
            with self.assertRaises(CredentialError):
                load_credentials("IOL", self.path)

    def test_missing_value_non_object_and_relative_path(self):
        for payload in ({}, {"IOL": None}, ["not an object"]):
            self.encrypted(payload)
            with patch.object(SecretStore, "get_bytes", return_value=self.key):
                with self.assertRaises(CredentialError):
                    load_credentials("IOL", self.path)
        with self.assertRaises(CredentialError):
            load_credentials("IOL", "relative.ini")

    def test_no_secrets_in_error(self):
        self.path.write_bytes(b"private-synthetic-content")
        with patch.object(SecretStore, "get_bytes", return_value=self.key):
            with self.assertRaises(CredentialError) as caught:
                load_credentials("IOL", self.path)
        self.assertNotIn("private-synthetic", str(caught.exception))


class ClientTests(unittest.TestCase):
    def client(self, session, clock=None):
        return IOLClient("/unused", "synthetic-user", session=session,
            secret_loader=lambda *args: "synthetic-password", clock=clock,
            sleep=lambda seconds: None)

    def test_auth_cached_and_refresh_in_memory(self):
        moment = [0]
        session = Session(posts=[Response({"access_token": "a", "refresh_token": "r", "expires_in": 900}),
                                 Response({"access_token": "b", "refresh_token": "s", "expires_in": 900})])
        client = self.client(session, clock=lambda: moment[0])
        self.assertEqual(client.authenticate(), "a")
        self.assertEqual(client.authenticate(), "a")
        self.assertEqual(len(session.calls), 1)
        moment[0] = 901
        self.assertEqual(client.authenticate(), "b")
        self.assertEqual(session.calls[1][2]["data"]["grant_type"], "refresh_token")
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        self.assertTrue(session.calls[0][2]["verify"])
        client.close()
        self.assertIsNone(client._access_token)
        self.assertIsNone(client._refresh_token)
        self.assertTrue(session.closed)

    def test_auth_failure_is_not_retried(self):
        session = Session(posts=[Response({"error": "private-synthetic"}, status=400)])
        with self.assertRaises(IOLHTTPError) as caught:
            self.client(session).authenticate()
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn("private-synthetic", str(caught.exception))

    def test_safe_invalid_json(self):
        session = Session(posts=[Response(ValueError("private synthetic body"))])
        with self.assertRaises(IOLDataError) as caught:
            self.client(session).authenticate()
        self.assertNotIn("private synthetic", str(caught.exception))

    def test_get_bounded_retry(self):
        session = Session(posts=[Response({"access_token": "a"})], gets=[
            Response({}, status=429), Response({}, status=503), Response({"cuentas": []})])
        self.assertEqual(self.client(session).get_account(), {"cuentas": []})
        self.assertEqual(sum(call[0] == "GET" for call in session.calls), 3)

    def test_unsupported_date_filter_falls_back_without_losing_history(self):
        session = Session(posts=[Response({"access_token": "a"})], gets=[
            Response({}, status=422), Response([{"numero": 1}])])
        client = self.client(session)
        self.assertEqual(client.get_operations(date(2026, 1, 1), date(2026, 1, 31)), [{"numero": 1}])
        self.assertFalse(client.date_filter_supported)
        self.assertNotIn("filtro.fechaDesde", session.calls[-1][2]["params"])

    def test_incomplete_collection_is_not_silently_accepted(self):
        session = Session(posts=[Response({"access_token": "a"})], gets=[
            Response({"items": [{"numero": 1}], "total": 50})])
        with self.assertRaises(IOLDataError):
            self.client(session).get_operations("2026-01-01", "2026-01-31")


class ClosingTests(unittest.TestCase):
    def test_open_day_excluded_and_friday_kept_on_sunday(self):
        rows = [{"fechaHora": "2026-11-06T17:00:00", "ultimoPrecio": "81250"},
                {"fechaHora": "2026-11-08T12:00:00", "ultimoPrecio": "99999"}]
        self.assertEqual(historical_closes(rows, date(2026, 11, 8)),
                         {"2026-11-06": Decimal("81250")})

    def test_latest_time_and_conflicting_duplicate(self):
        rows = [{"fechaHora": "2026-11-06T11:00:00", "ultimoPrecio": 100},
                {"fechaHora": "2026-11-06T17:00:00", "ultimoPrecio": 101}]
        self.assertEqual(historical_closes(rows, date(2026, 11, 7))["2026-11-06"], 101)
        rows.append({"fechaHora": "2026-11-06T17:00:00", "ultimoPrecio": 102})
        with self.assertRaises(IOLDataError):
            historical_closes(rows, date(2026, 11, 7))

    def test_zero_prices_and_nonfinite_data_rejected(self):
        for price in (0, "NaN", "Infinity"):
            with self.assertRaises(IOLDataError):
                historical_closes([{"fechaHora": "2026-11-06", "ultimoPrecio": price}], date(2026, 11, 7))

    def test_account_order_does_not_matter(self):
        payload = {"cuentas": [
            {"tipo": "inversion_Argentina_Dolares", "saldo": "20", "titulosValorizados": 0},
            {"tipo": "inversion_Argentina_Pesos", "total": "200000", "titulosValorizados": "150000"}]}
        self.assertEqual(normalize_account(payload), {"cash_ars": Decimal("50000"),
            "cash_usd": Decimal("20"), "securities_ars": Decimal("150000")})

    def test_explicit_currency_and_common_ars_valuation_reconciliation(self):
        portfolio = {"activos": [{"titulo": {"simbolo": "EXAMPLED", "moneda": "dolar_Estadounidense", "tipo": "CEDEAR"},
                                   "cantidad": "1.25", "valorizado": "100"}]}
        rows, basis = normalize_portfolio(portfolio, Decimal("150000"), Decimal("1500"))
        self.assertEqual(rows[0]["value_usd"], Decimal("100"))
        self.assertEqual(basis, "declared_currency")
        portfolio["activos"][0]["valorizado"] = "150000"
        rows, basis = normalize_portfolio(portfolio, Decimal("150000"), Decimal("1500"))
        self.assertEqual(rows[0]["value_usd"], Decimal("100"))
        self.assertEqual(basis, "account_reconciled_ARS")

    def test_unreconciled_or_ccl_portfolio_rejected(self):
        portfolio = {"activos": [{"titulo": {"simbolo": "EXAMPLEC", "moneda": "USD_CCL"},
                                   "cantidad": 1, "valorizado": "100"}]}
        with self.assertRaises(IOLDataError):
            normalize_portfolio(portfolio, Decimal("150000"), Decimal("1500"))


class FakeIOL:
    date_filter_supported = False

    def get_operations(self, start, end):
        return [{"numero": 2, "tipo": "Pago de Dividendos", "simbolo": "EXAMPLE US$", "montoOperado": "1.25"}]

    def get_account(self):
        return {"cuentas": [
            {"tipo": "inversion_Argentina_Dolares", "saldo": "20", "titulosValorizados": 0},
            {"tipo": "inversion_Argentina_Pesos", "total": "165000", "titulosValorizados": "150000"}]}

    def get_portfolio(self):
        return {"activos": [{"titulo": {"simbolo": "EXAMPLE", "moneda": "ARS", "tipo": "CEDEAR"},
                             "cantidad": "1.25", "valorizado": "150000"}]}

    def get_historical(self, symbol, start, end):
        price = {"AL30": "75000", "AL30D": "50", "GD30": "78000", "GD30D": "52"}[symbol]
        return [{"fechaHora": "2026-11-06T17:00:00", "ultimoPrecio": price},
                {"fechaHora": "2026-11-08T12:00:00", "ultimoPrecio": "999"}]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.directory.name) / "test.sqlite3")
        db.initialize(self.conn)
        self.conn.execute("""INSERT INTO movements(id,effective_at,type,participant_id,
            amount_usd,units,unit_value,fx,note,status,source_key,created_at)
            VALUES('m1','2026-10-01','subscription','tommy','100','100','1','1500','','posted','synthetic:m1','2026-10-01')""")
        self.conn.execute("""INSERT INTO movements(id,effective_at,type,participant_id,
            amount_usd,units,unit_value,fx,note,status,source_key,created_at)
            VALUES('pending','2026-11-01','subscription','juli','50','50','1','1500','','pending','synthetic:p','2026-11-01')""")
        db.upsert_operation(self.conn, "iol", "1", {"numero": 1, "old": True})
        db.upsert_quote(self.conn, "MEP", "2023-01-02", "331.68", "Excel")
        self.conn.commit()
        self.when = datetime(2026, 11, 8, 18, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def test_twice_is_incremental_and_preserves_older_rows(self):
        first = sync_account(self.conn, FakeIOL(), Path(self.directory.name) / "raw", self.when)
        second = sync_account(self.conn, FakeIOL(), Path(self.directory.name) / "raw", self.when)
        self.assertEqual(first["operations"]["inserted"], 1)
        self.assertEqual(second["operations"]["inserted"], 0)
        self.assertEqual(second["operations"]["unchanged"], 1)
        self.assertEqual(second["snapshot_status"], "unchanged")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM quotes WHERE date='2026-11-08'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT price FROM quotes WHERE date='2023-01-02'").fetchone()[0], "331.68")
        self.assertEqual(first["units"], "100")
        self.assertEqual(first["quote_as_of"], "2026-11-06")
        self.assertEqual(Decimal(first["total_usd"]), Decimal("130"))
        self.assertEqual(first["portfolio_count"], 1)

    def test_failed_reconciliation_does_not_partially_merge(self):
        client = FakeIOL()
        client.get_portfolio = lambda: {"activos": []}
        with self.assertRaises(IOLDataError):
            sync_account(self.conn, client, Path(self.directory.name) / "raw", self.when)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM runs").fetchone()[0], "failed")
        self.assertTrue(list((Path(self.directory.name) / "raw").glob("*/account.json")))

    def test_changed_allocation_revises_close_even_with_zero_net_units(self):
        first = sync_account(self.conn, FakeIOL(), Path(self.directory.name) / "raw", self.when)
        for identifier, participant, units in (("transfer-out", "tommy", "-10"),
                                               ("transfer-in", "juli", "10")):
            self.conn.execute("""INSERT INTO movements(id,effective_at,type,participant_id,
                amount_usd,units,unit_value,fx,note,status,source_key,created_at)
                VALUES(?,'2026-11-08','subscription',?,? ,?,'1','1500','','posted',?,'2026-11-08')""",
                (identifier, participant, units, units, "synthetic:" + identifier))
        self.conn.commit()
        second = sync_account(self.conn, FakeIOL(), Path(self.directory.name) / "raw", self.when)
        self.assertEqual(first["units"], second["units"])
        self.assertEqual(second["snapshot_status"], "updated")
        metadata = json.loads(self.conn.execute("SELECT metadata_json FROM account_snapshots").fetchone()[0])
        self.assertIn("transfer-in", metadata["included_movement_ids"])

    def test_mep_requires_matching_dates(self):
        client = FakeIOL()
        original = client.get_historical
        client.get_historical = lambda symbol, start, end: ([
            {"fechaHora": "2026-11-05T17:00:00", "ultimoPrecio": "50"}
        ] if symbol == "AL30D" else original(symbol, start, end))
        with self.assertRaises(IOLDataError):
            sync_account(self.conn, client, Path(self.directory.name) / "raw", self.when)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
