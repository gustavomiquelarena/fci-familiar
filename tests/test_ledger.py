"""Financial invariants and the read-only migration of the supplied workbook."""
from __future__ import annotations

import hashlib
import json
import unittest
from decimal import Decimal
from pathlib import Path

from fci_familiar import db, finance, importer


def seeded_connection():
    conn = db.connect(":memory:")
    db.initialize(conn)
    for participant in ("tommy", "juli", "pili"):
        conn.execute("""INSERT INTO movements(id,effective_at,type,participant_id,amount_usd,
            units,unit_value,fx,note,status,source_key,created_at,original_amount)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (f"initial:{participant}", "2025-12-01", "initial", participant,
                      "100", "100", "1", "1000", "", "posted", f"initial:{participant}",
                      "2025-12-01T00:00:00Z", "100"))
    insert_close(conn, "2026-01-01", "300", "1")
    conn.commit()
    return conn


def insert_close(conn, as_of, units, unit_value, metadata=None):
    total = Decimal(units) * Decimal(unit_value)
    conn.execute("""INSERT INTO account_snapshots(id,as_of,cash_ars,cash_usd,securities_ars,
        mep,total_usd,units,unit_value,source,raw_hash,metadata_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (as_of, as_of, "0", str(total), "0", "1000", str(total), units, unit_value,
                  "test", f"hash:{as_of}", json.dumps(metadata or {})))


def own(bundle, participant="tommy"):
    return next(item["report"] for item in bundle["participants"] if item["id"] == participant)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = seeded_connection()
        self.addCleanup(self.conn.close)

    def test_ars_uses_saved_mep_and_fractional_units_without_publishing_ahead(self):
        created = finance.add_movement(self.conn, {"participant_id": "tommy", "type": "subscription",
            "amount": "1500", "currency": "ARS", "date": "2026-01-02", "request_id": "a"})
        self.assertEqual(Decimal(created["movements"][0]["units"]), Decimal("1.5"))
        before = finance.build_reports(self.conn)
        self.assertEqual(Decimal(own(before)["balance_usd"]), Decimal("100"))
        self.assertEqual(len(before["pending"]), 1)
        insert_close(self.conn, "2026-01-03", "301.5", "1")
        after = finance.build_reports(self.conn)
        self.assertEqual(Decimal(own(after)["balance_usd"]), Decimal("101.5"))
        self.assertEqual(Decimal(own(after)["profit_usd"]), Decimal("0"))
        self.assertEqual(after["issues"], [])

    def test_split_is_atomic_and_repeated_request_is_idempotent(self):
        data = {"type": "subscription", "amount": "30", "currency": "USD", "date": "2026-01-02",
                "allocations": {"tommy": "10", "juli": "20"}, "request_id": "request_%_1"}
        first, second = finance.add_movement(self.conn, data), finance.add_movement(self.conn, data)
        self.assertEqual(len(first["movements"]), 2)
        self.assertTrue(second["duplicate"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM movements").fetchone()[0], 5)
        with self.assertRaises(ValueError):
            finance.add_movement(self.conn, {**data, "amount": "31", "allocations": {"tommy": "11", "juli": "20"}})

    def test_excess_redemption_rolls_back_allocation(self):
        with self.assertRaises(ValueError):
            finance.add_movement(self.conn, {"participant_id": "tommy", "type": "redemption",
                "amount": "101", "currency": "USD", "date": "2026-01-02"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM movements").fetchone()[0], 3)

    def test_pending_redemption_reserves_units(self):
        finance.add_movement(self.conn, {"participant_id": "tommy", "type": "redemption",
            "amount": "80", "date": "2026-01-02", "status": "pending"})
        with self.assertRaises(ValueError):
            finance.add_movement(self.conn, {"participant_id": "tommy", "type": "redemption",
                "amount": "30", "date": "2026-01-03"})
        self.assertEqual(Decimal(own(finance.build_reports(self.conn))["units"]), Decimal("100"))

    def test_reverse_posted_uses_counterentry_not_double_subtraction(self):
        movement = finance.add_movement(self.conn, {"participant_id": "tommy", "type": "subscription",
            "amount": "50", "date": "2026-01-02"})["movements"][0]
        reversal = finance.reverse_movement(self.conn, movement["id"], "Corrección")
        self.assertEqual(reversal["movement"]["status"], "posted")
        self.assertEqual(reversal["reversal"]["status"], "posted")
        self.assertEqual(sum((Decimal(r[0]) for r in self.conn.execute("SELECT units FROM movements WHERE status='posted'")), Decimal(0)), Decimal("300"))
        self.assertTrue(finance.reverse_movement(self.conn, movement["id"], "Reintento")["duplicate"])
        insert_close(self.conn, "2026-01-03", "300", "1")
        report = own(finance.build_reports(self.conn))
        self.assertEqual(Decimal(report["contributions_usd"]), Decimal("100"))
        self.assertEqual(Decimal(report["profit_usd"]), Decimal("0"))

    def test_reverse_pending_cancels_without_posting_negative_entry(self):
        movement = finance.add_movement(self.conn, {"participant_id": "tommy", "type": "subscription",
            "amount": "50", "date": "2026-01-02", "status": "pending"})["movements"][0]
        result = finance.reverse_movement(self.conn, movement["id"], "No se acreditó")
        self.assertEqual(result["movement"]["status"], "reversed")
        self.assertEqual(result["reversal"]["status"], "reversed")
        insert_close(self.conn, "2026-01-03", "300", "1")
        self.assertEqual(finance.build_reports(self.conn)["pending"], [])

    def test_market_gain_is_separate_from_cash_flows(self):
        insert_close(self.conn, "2026-01-03", "300", "1.1")
        report = own(finance.build_reports(self.conn))
        self.assertEqual(Decimal(report["profit_usd"]), Decimal("10"))
        self.assertEqual(Decimal(report["unit_return_pct"]), Decimal("10"))

    def test_recorded_close_does_not_include_a_later_same_day_entry(self):
        frozen_ids = [row[0] for row in self.conn.execute("SELECT id FROM movements")]
        self.conn.execute("UPDATE account_snapshots SET metadata_json=?",
                          (json.dumps({"included_movement_ids": frozen_ids}),))
        self.conn.commit()
        finance.add_movement(self.conn, {"participant_id": "tommy", "type": "subscription",
            "amount": "25", "date": "2026-01-01"})
        report = finance.build_reports(self.conn)
        self.assertEqual(Decimal(own(report)["units"]), Decimal("100"))
        self.assertEqual(len(report["pending"]), 1)
        self.assertEqual(report["issues"], [])

    def test_date_only_selects_same_day_saved_close_and_never_future_close(self):
        insert_close(self.conn, "2026-01-02T12:00:00", "300", "1.2")
        insert_close(self.conn, "2026-01-04", "300", "1.9")
        movement = finance.add_movement(self.conn, {"participant_id": "tommy", "type": "subscription",
            "amount": "12", "date": "2026-01-02"})["movements"][0]
        self.assertEqual(Decimal(movement["units"]), Decimal("10"))
        self.assertEqual(movement["close_id"], "2026-01-02T12:00:00")

    def test_bad_numeric_values_and_distribution_are_rejected(self):
        for amount in ("NaN", "Infinity", "-1", "0", "texto"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                finance.add_movement(self.conn, {"participant_id": "tommy", "amount": amount, "date": "2026-01-02"})
        with self.assertRaises(ValueError):
            finance.add_movement(self.conn, {"amount": "20", "allocations": {"tommy": "10"}, "date": "2026-01-02"})

    def test_quote_and_operation_corrections_are_audited_without_duplicates(self):
        self.assertEqual(db.upsert_operation(self.conn, "iol", "1", {"amount": "10"}), "inserted")
        self.assertEqual(db.upsert_operation(self.conn, "iol", "1", {"amount": "10"}), "unchanged")
        self.assertEqual(db.upsert_operation(self.conn, "iol", "1", {"amount": "11"}), "updated")
        self.assertEqual(db.upsert_quote(self.conn, "MEP", "2026-01-01", "1000"), "inserted")
        self.assertEqual(db.upsert_quote(self.conn, "MEP", "2026-01-01", "1001"), "updated")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM operations").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM quotes").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM audit_log WHERE action='updated'").fetchone()[0], 2)

    def test_mismatched_close_blocks_publication(self):
        insert_close(self.conn, "2026-01-03", "400", "1")
        self.assertTrue(any(issue["blocking"] for issue in finance.build_reports(self.conn)["issues"]))


class SuppliedWorkbookMigrationTests(unittest.TestCase):
    workbook = Path(__file__).resolve().parents[2] / "fci y mep.xlsx"

    @unittest.skipUnless(workbook.exists(), "El histórico real se verifica sólo en el equipo del administrador.")
    def test_real_workbook_is_preserved_and_import_is_repeatable(self):
        original_hash = hashlib.sha256(self.workbook.read_bytes()).hexdigest()
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.initialize(conn)
        first = importer.import_workbook(conn, self.workbook)
        second = importer.import_workbook(conn, self.workbook)
        self.assertEqual([first[k] for k in ("operations", "snapshots", "quotes", "movement_rows", "allocations")],
                         [227, 64, 950, 36, 49])
        self.assertEqual(first["differences"], [])
        self.assertTrue(second["already_imported"])
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM movements").fetchone()[0], 49)
        bundle = finance.build_reports(conn)
        saved_close = conn.execute("SELECT * FROM account_snapshots ORDER BY as_of DESC LIMIT 1").fetchone()
        self.assertEqual(bundle["as_of"], saved_close["as_of"])
        self.assertEqual(len(bundle["pending"]), 1)
        self.assertGreater(bundle["pending"][0]["date"], saved_close["as_of"])
        self.assertEqual(bundle["issues"], [])
        self.assertLess(abs(sum((Decimal(item["report"]["balance_usd"]) for item in bundle["participants"]), Decimal(0)) - Decimal(saved_close["total_usd"])), Decimal("0.02"))
        for snapshot in conn.execute("SELECT * FROM account_snapshots"):
            self.assertLess(abs(sum((Decimal(row["units"]) for row in finance.eligible_movements(conn, snapshot)), Decimal(0)) - Decimal(snapshot["units"])), Decimal("0.00000001"))
        self.assertIn("Historic_MEP", conn.execute("SELECT query_code FROM migration_log").fetchone()[0])
        self.assertEqual(hashlib.sha256(self.workbook.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
