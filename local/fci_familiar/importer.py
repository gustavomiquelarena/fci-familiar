"""Read-only migration of Excel's saved values, including Power Query sources.

Excel is never recalculated or rewritten. Cached historical unit prices are the
recorded source of truth; external workbook formulas are retained as provenance.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import struct
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from . import db

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _decimal(value, default="0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("El histórico contiene un número no finito.")
    return result


def _excel_date(value, with_time=False) -> str:
    days = _decimal(value)
    if with_time:
        milliseconds = int((days * 86400000).quantize(Decimal("1"), ROUND_HALF_UP))
        dt = datetime(1899, 12, 30) + timedelta(milliseconds=milliseconds)
        return dt.isoformat(timespec="milliseconds").removesuffix(".000")
    return (datetime(1899, 12, 30) + timedelta(days=int(days))).date().isoformat()


def _row_number(cell):
    return int(re.search(r"\d+$", cell).group())


class SavedWorkbook:
    """Small OOXML reader that deliberately exposes cached formula values."""

    def __init__(self, path):
        self.path = Path(path)
        self.content = self.path.read_bytes()
        self.sha256 = hashlib.sha256(self.content).hexdigest()
        self.sheets = {}
        self.formulas = {}
        self.tables = {}
        self.query_code = ""
        self.errors = []
        with ZipFile(io.BytesIO(self.content)) as archive:
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = ["".join(node.itertext()) for node in
                          ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", NS)]
            rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            rels = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rel_root}
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            for node in workbook.findall("s:sheets/s:sheet", NS):
                target = rels[node.attrib[f"{{{NS['r']}}}id"]]
                part = target.lstrip("/") if target.startswith("/") else "xl/" + target
                cells, formulas = {}, {}
                for cell in ET.fromstring(archive.read(part)).findall(".//s:sheetData/s:row/s:c", NS):
                    ref = cell.attrib["r"]
                    value_node = cell.find("s:v", NS)
                    value = value_node.text if value_node is not None else None
                    kind = cell.attrib.get("t")
                    if kind == "s" and value is not None:
                        value = shared[int(value)]
                    elif kind == "inlineStr":
                        inline = cell.find("s:is", NS)
                        value = "".join(inline.itertext()) if inline is not None else None
                    elif kind == "e":
                        self.errors.append({"sheet": node.attrib["name"], "cell": ref, "error": value})
                    formula = cell.find("s:f", NS)
                    if formula is not None:
                        formulas[ref] = formula.text
                    cells[ref] = value
                self.sheets[node.attrib["name"]] = cells
                self.formulas[node.attrib["name"]] = formulas
            for part in archive.namelist():
                if part.startswith("xl/tables/") and part.endswith(".xml") and "/_rels/" not in part:
                    table = ET.fromstring(archive.read(part))
                    self.tables[table.attrib["name"]] = dict(table.attrib)
            if "customXml/item1.xml" in archive.namelist():
                root = ET.fromstring(archive.read("customXml/item1.xml"))
                if root.text:
                    try:
                        blob = base64.b64decode(root.text)
                        _, length = struct.unpack("<II", blob[:8])
                        with ZipFile(io.BytesIO(blob[8:8 + length])) as package:
                            self.query_code = "\n\n".join(
                                package.read(name).decode("utf-8-sig") for name in package.namelist()
                                if name.endswith(".m"))
                    except (ValueError, KeyError, struct.error):
                        self.query_code = ""

    def table_rows(self, name):
        if name not in self.tables:
            raise ValueError(f"Falta la tabla histórica {name}.")
        ref = self.tables[name]["ref"]
        first, last = ref.split(":")
        col0 = re.sub(r"\d", "", first)
        # Table locations are matched by the header in the saved sheet, not by order.
        header_row = _row_number(first)
        for sheet_name, cells in self.sheets.items():
            if cells.get(f"{col0}{header_row}") in ("numero", "Fecha", "fecha"):
                if name == "MEP" and cells.get(f"{col0}{header_row}") != "fecha":
                    continue
                if name == "Operaciones_IOL" and cells.get(f"{col0}{header_row}") != "numero":
                    continue
                if name == "EstadoCuenta_IOL" and cells.get(f"{col0}{header_row}") != "Fecha":
                    continue
                return sheet_name, cells, range(header_row + 1, _row_number(last) + 1)
        raise ValueError(f"No se localizaron valores guardados para {name}.")


def _snapshot_members(conn, as_of, target_units):
    rows = conn.execute("""SELECT * FROM movements WHERE status='posted'
        AND substr(effective_at,1,10)<=? ORDER BY effective_at,source_key,id""", (as_of,)).fetchall()
    before = [row for row in rows if row["effective_at"][:10] < as_of]
    same = [row for row in rows if row["effective_at"][:10] == as_of]
    included = list(before)
    total = sum((_decimal(row["units"]) for row in included), Decimal(0))
    if abs(total - target_units) <= Decimal("0.00000001"):
        return [row["id"] for row in included], None
    # Same-day order is the explicit source row/column ordering, never a guessed time.
    for row in same:
        included.append(row)
        total += _decimal(row["units"])
        if abs(total - target_units) <= Decimal("0.00000001"):
            return [item["id"] for item in included], None
    return [row["id"] for row in rows], {
        "code": "historical_units_difference", "severity": "warning", "blocking": False,
        "message": f"El cierre {as_of} registra {target_units} cuotapartes; el libro de movimientos suma {total}.",
        "as_of": as_of, "recorded_units": str(target_units), "ledger_units": str(total)}


def import_workbook(conn, workbook_path) -> dict:
    source = SavedWorkbook(workbook_path)
    existing = conn.execute("SELECT counts_json FROM migration_log WHERE source_hash=?",
                            (source.sha256,)).fetchone()
    if existing:
        result = json.loads(existing["counts_json"])
        return {**result, "already_imported": True, "inserted": 0}
    if conn.execute("SELECT 1 FROM migration_log LIMIT 1").fetchone():
        raise ValueError("La base ya contiene una migración de otro archivo. Revise el histórico antes de reemplazarla.")
    if source.errors:
        raise ValueError("El archivo contiene errores de Excel guardados; se requiere revisión antes de migrar.")
    sheet_name = next((name for name in source.sheets if name.casefold() == "fci chicos"), None)
    if sheet_name is None:
        raise ValueError("No existe la hoja FCI Chicos.")
    cells = source.sheets[sheet_name]
    counts = {"operations": 0, "snapshots": 0, "quotes": 0,
              "movement_rows": 0, "allocations": 0}
    differences = []
    with conn:
        _, mep_cells, mep_rows = source.table_rows("MEP")
        for row in mep_rows:
            if mep_cells.get(f"A{row}") is None:
                continue
            day, price = _excel_date(mep_cells[f"A{row}"]), _decimal(mep_cells.get(f"B{row}"))
            if price <= 0:
                raise ValueError(f"Cotización MEP inválida en fila {row}.")
            db.upsert_quote(conn, "MEP", day, str(price), "Excel histórico")
            counts["quotes"] += 1
        for col, participant_id in [("C", "tommy"), ("D", "juli"), ("E", "pili")]:
            conn.execute("UPDATE participants SET name=? WHERE id=?",
                         (cells.get(f"{col}6") or participant_id.capitalize(), participant_id))
        event_rows = sorted({_row_number(key) for key, value in cells.items()
                             if key.startswith("A") and re.match(r"^A\d+$", key)
                             and value in ("Inicio", "Suscripción", "Rescate")})
        for row in event_rows:
            day = _excel_date(cells[f"B{row}"])
            price = _decimal(cells.get(f"H{row}"))
            if price <= 0:
                raise ValueError(f"Valor de cuotaparte inválido en movimiento {row}.")
            counts["movement_rows"] += 1
            total_units = Decimal(0)
            quoted = conn.execute("SELECT price FROM quotes WHERE symbol='MEP' AND date<=? ORDER BY date DESC LIMIT 1", (day,)).fetchone()
            fx = _decimal(quoted["price"]) if quoted else Decimal(1)
            note = cells.get(f"I{row}") or ""
            try:
                if note:
                    fx, note = _decimal(note), ""
            except Exception:
                pass
            event_kind = {"Inicio": "initial", "Suscripción": "subscription", "Rescate": "redemption"}[cells[f"A{row}"]]
            for column_index, (col, participant_id) in enumerate([("C", "tommy"), ("D", "juli"), ("E", "pili")]):
                units = _decimal(cells.get(f"{col}{row}"))
                if units == 0:
                    continue
                if (event_kind == "redemption") != (units < 0):
                    raise ValueError(f"Signo inconsistente en movimiento {row}.")
                amount = units * price
                total_units += units
                source_key = f"excel:{row:06d}:{column_index}:{participant_id}"
                movement_id = hashlib.sha256((source.sha256 + ":" + source_key).encode()).hexdigest()[:32]
                metadata = {"sheet": sheet_name, "row": row, "column": col,
                            "source_hash": source.sha256,
                            "declared_row_amount_usd": cells.get(f"G{row}"),
                            "recorded_units": str(units), "recorded_unit_value": str(price)}
                conn.execute("""INSERT INTO movements(id,effective_at,type,participant_id,amount_usd,
                    units,unit_value,fx,note,status,source_key,reverses_id,created_at,currency,
                    original_amount,close_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (movement_id, day, event_kind, participant_id, str(amount), str(units),
                              str(price), str(fx), str(note), "posted", source_key, None,
                              db.utcnow(), "USD", str(amount), None, db.json_text(metadata)))
                counts["allocations"] += 1
            declared_amount = _decimal(cells.get(f"G{row}"))
            if abs(total_units * price - declared_amount) > Decimal("0.00000001"):
                differences.append({"code": "historical_flow_difference", "severity": "warning",
                                    "blocking": False, "row": row,
                                    "message": f"Diferencia entre aporte y cuotapartes en fila {row}."})
        _, op_cells, op_rows = source.table_rows("Operaciones_IOL")
        for row in op_rows:
            if op_cells.get(f"M{row}") is None:
                continue
            number = str(int(_decimal(op_cells[f"M{row}"])))
            payload = {"numero": number, "tipo": op_cells.get(f"N{row}"),
                       "simbolo": op_cells.get(f"O{row}"),
                       "fechaOperada": _excel_date(op_cells[f"P{row}"], True),
                       "cantidad": str(_decimal(op_cells.get(f"Q{row}"))),
                       "cantidadOperada": str(_decimal(op_cells.get(f"R{row}"))),
                       "precioOperado": str(_decimal(op_cells.get(f"S{row}"))),
                       "USD": str(_decimal(op_cells.get(f"T{row}"))),
                       "ARS": str(_decimal(op_cells.get(f"U{row}"))), "source": "Excel histórico"}
            db.upsert_operation(conn, "iol", number, payload)
            counts["operations"] += 1
        _, account_cells, account_rows = source.table_rows("EstadoCuenta_IOL")
        for row in account_rows:
            if account_cells.get(f"W{row}") is None:
                continue
            day = _excel_date(account_cells[f"W{row}"])
            amounts = {name: _decimal(account_cells.get(f"{col}{row}")) for name, col in
                       [("cash_ars", "X"), ("cash_usd", "Y"), ("securities_ars", "Z"),
                        ("mep", "AA"), ("total_usd", "AB"), ("units", "AC"), ("unit_value", "AD")]}
            if min(amounts["mep"], amounts["units"], amounts["unit_value"]) <= 0:
                raise ValueError(f"El cierre histórico {day} tiene MEP, unidades o valor unitario inválido.")
            members, issue = _snapshot_members(conn, day, amounts["units"])
            if issue:
                differences.append(issue)
            metadata = {"historical": True, "source_sheet": sheet_name, "source_row": row,
                        "included_movement_ids": members, "issues": [issue] if issue else [],
                        "recorded_units": str(amounts["units"]),
                        "recorded_unit_value": str(amounts["unit_value"])}
            snapshot_id = f"excel:{day}"
            conn.execute("""INSERT INTO account_snapshots(id,as_of,cash_ars,cash_usd,
                 securities_ars,mep,total_usd,units,unit_value,source,raw_hash,metadata_json)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (snapshot_id, day, str(amounts["cash_ars"]), str(amounts["cash_usd"]),
                          str(amounts["securities_ars"]), str(amounts["mep"]),
                          str(amounts["total_usd"]), str(amounts["units"]), str(amounts["unit_value"]),
                          "Excel histórico", source.sha256, db.json_text(metadata)))
            counts["snapshots"] += 1
        summary = {**counts, "source_hash": source.sha256, "differences": differences,
                   "already_imported": False, "inserted": sum(counts[k] for k in
                    ("operations", "snapshots", "quotes", "allocations"))}
        conn.execute("""INSERT INTO migration_log(source_hash,source_path,imported_at,counts_json,query_code)
                        VALUES(?,?,?,?,?)""",
                     (source.sha256, str(source.path.resolve()), db.utcnow(),
                      db.json_text(summary), source.query_code))
        db.audit(conn, "imported", "workbook", source.sha256, after=summary)
    return summary
