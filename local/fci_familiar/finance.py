"""Fund units and personal statements, calculated locally with Decimal."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from . import db
from .portfolio import classified_positions

ZERO = Decimal(0)
UNIT_TOLERANCE = Decimal("0.00000001")
MONEY_TOLERANCE = Decimal("0.02")


def dec(value, default="0") -> Decimal:
    try:
        number = Decimal(str(value if value is not None and value != "" else default))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Ingrese un importe numérico válido.") from exc
    if not number.is_finite():
        raise ValueError("Los importes deben ser números finitos.")
    return number


def decimal_text(value) -> str:
    number = dec(value)
    return format(number, "f")


def _metadata(row) -> dict:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return {}


def latest_close(conn, effective_at=None):
    if effective_at:
        expression = "as_of" if "T" in str(effective_at) else "substr(as_of,1,10)"
        row = conn.execute(f"SELECT * FROM account_snapshots WHERE {expression}<=? ORDER BY as_of DESC,id DESC LIMIT 1",
                           (str(effective_at),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM account_snapshots ORDER BY as_of DESC,id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


get_last_close = latest_close


def eligible_movements(conn, snapshot) -> list[dict]:
    """Use saved membership for historical closes; actual cutoff for new closes."""
    rows = conn.execute("""SELECT * FROM movements WHERE status='posted'
        AND substr(effective_at,1,10)<=? ORDER BY effective_at,source_key,id""",
                        (snapshot["as_of"][:10],)).fetchall()
    metadata = _metadata(snapshot)
    if "included_movement_ids" in metadata:
        allowed = set(metadata["included_movement_ids"])
        return [dict(row) for row in rows if row["id"] in allowed]
    cutoff = snapshot["as_of"]
    if "T" in cutoff:
        rows = [row for row in rows if row["effective_at"] <= cutoff]
    return [dict(row) for row in rows]


def _validate_nonnegative(conn):
    balances = {}
    for row in conn.execute("""SELECT * FROM movements WHERE status IN ('posted','pending')
        ORDER BY effective_at,created_at,source_key,id"""):
        key = row["participant_id"]
        balances[key] = balances.get(key, ZERO) + dec(row["units"])
        if balances[key] < -UNIT_TOLERANCE:
            raise ValueError("El rescate supera las cuotapartes disponibles del beneficiario en esa fecha.")


def _effective_at(value) -> str:
    text = str(value or date.today().isoformat()).strip()
    try:
        if "T" in text:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("La fecha debe tener formato AAAA-MM-DD.") from exc
    return text


def add_movement(conn, data: dict) -> dict:
    """Add one or more allocations atomically at the latest recorded unit value.

    ``amount`` and allocation amounts are positive in the original ``currency``.
    ``type`` supplies the economic direction; a request_id makes retries safe.
    """
    effective_at = _effective_at(data.get("effective_at") or data.get("date"))
    kind = str(data.get("type", "subscription")).casefold()
    kind = {"aporte": "subscription", "suscripción": "subscription", "suscripcion": "subscription",
            "retiro": "redemption", "rescate": "redemption"}.get(kind, kind)
    if kind not in ("subscription", "redemption"):
        raise ValueError("Seleccione suscripción o rescate.")
    currency = str(data.get("currency", "USD")).upper()
    if currency not in ("ARS", "USD"):
        raise ValueError("La moneda debe ser ARS o USD.")
    status = str(data.get("status", "posted"))
    if status not in ("posted", "pending"):
        raise ValueError("El estado debe ser acreditado o pendiente.")
    close = latest_close(conn, effective_at)
    if not close:
        raise ValueError("No hay un cierre guardado correspondiente a la fecha del movimiento.")
    unit_value, fx = dec(close["unit_value"]), dec(close["mep"])
    if unit_value <= 0 or fx <= 0:
        raise ValueError("El cierre seleccionado no tiene una valuación válida.")
    allocations = data.get("allocations")
    if allocations is None:
        allocations = {str(data.get("participant_id", "")): data.get("amount", data.get("amount_usd"))}
    elif isinstance(allocations, list):
        if len({item["participant_id"] for item in allocations}) != len(allocations):
            raise ValueError("Un beneficiario no puede repetirse dentro de una asignación.")
        allocations = {item["participant_id"]: item["amount"] for item in allocations}
    if not isinstance(allocations, dict) or not allocations:
        raise ValueError("Seleccione al menos un beneficiario.")
    valid_participants = {row["id"] for row in conn.execute("SELECT id FROM participants")}
    normalized = {}
    for participant_id, value in allocations.items():
        if participant_id not in valid_participants:
            raise ValueError("El beneficiario no existe.")
        amount = dec(value)
        if amount <= 0:
            raise ValueError("Cada importe debe ser mayor que cero.")
        normalized[participant_id] = amount
    total = sum(normalized.values(), ZERO)
    if data.get("amount") is not None and abs(total - dec(data["amount"])) > Decimal("0.00000001"):
        raise ValueError("La distribución no coincide con el importe total.")
    request_id = str(data.get("request_id") or data.get("idempotency_key") or uuid.uuid4())
    source_prefix = f"manual:{request_id}:"
    old = conn.execute("SELECT * FROM movements WHERE substr(source_key,1,?)=? ORDER BY source_key",
                       (len(source_prefix), source_prefix)).fetchall()
    if old:
        expected = set(normalized)
        if {row["participant_id"] for row in old} != expected:
            raise ValueError("La referencia del movimiento ya fue utilizada con otra distribución.")
        for row in old:
            if (row["type"] != kind or row["currency"] != currency or
                    dec(row["original_amount"]) != normalized[row["participant_id"]] or
                    row["effective_at"] != effective_at):
                raise ValueError("La referencia del movimiento ya fue utilizada con otros datos.")
        return {"movements": [dict(row) for row in old], "duplicate": True}
    inserted = []
    with conn:
        for participant_id, amount in normalized.items():
            signed = amount if kind == "subscription" else -amount
            usd = signed / fx if currency == "ARS" else signed
            units = usd / unit_value
            movement_id = uuid.uuid4().hex
            metadata = {"request_id": request_id, "close_as_of": close["as_of"]}
            record = {"id": movement_id, "effective_at": effective_at, "type": kind,
                      "participant_id": participant_id, "amount_usd": decimal_text(usd),
                      "units": decimal_text(units), "unit_value": decimal_text(unit_value),
                      "fx": decimal_text(fx), "note": str(data.get("note") or "").strip(),
                      "status": status, "source_key": source_prefix + participant_id,
                      "reverses_id": None, "created_at": db.utcnow(), "currency": currency,
                      "original_amount": decimal_text(amount), "close_id": close["id"],
                      "metadata_json": db.json_text(metadata)}
            columns = ",".join(record)
            conn.execute(f"INSERT INTO movements({columns}) VALUES({','.join('?' for _ in record)})",
                         tuple(record.values()))
            db.audit(conn, "created", "movement", movement_id, after=record)
            inserted.append(record)
        _validate_nonnegative(conn)
    return {"movements": inserted, "duplicate": False}


def reverse_movement(conn, movement_id, reason) -> dict:
    if not str(reason or "").strip():
        raise ValueError("Indique el motivo de la reversión.")
    old = conn.execute("SELECT * FROM movements WHERE id=?", (movement_id,)).fetchone()
    if not old:
        raise ValueError("No se encontró el movimiento.")
    old = dict(old)
    existing = conn.execute("SELECT * FROM movements WHERE reverses_id=?", (movement_id,)).fetchone()
    if existing:
        return {"movement": old, "reversal": dict(existing), "duplicate": True}
    if old["type"] == "reversal" or old["status"] == "reversed":
        raise ValueError("Ese registro ya es una reversión.")
    reversal = dict(old)
    reversal.update(id=uuid.uuid4().hex, type="reversal", amount_usd=decimal_text(-dec(old["amount_usd"])),
                    units=decimal_text(-dec(old["units"])), note=str(reason).strip(),
                    source_key=f"reversal:{movement_id}", reverses_id=movement_id,
                    created_at=db.utcnow(), original_amount=decimal_text(-dec(old["original_amount"])),
                    metadata_json=db.json_text({"original_type": old["type"],
                                               "reason": str(reason).strip()}))
    with conn:
        if old["status"] == "pending":
            reversal["status"] = "reversed"
            conn.execute("UPDATE movements SET status='reversed' WHERE id=?", (movement_id,))
        metadata = _metadata(old)
        metadata["reversed_by"] = reversal["id"]
        conn.execute("UPDATE movements SET metadata_json=? WHERE id=?",
                     (db.json_text(metadata), movement_id))
        conn.execute(f"INSERT INTO movements({','.join(reversal)}) VALUES({','.join('?' for _ in reversal)})",
                     tuple(reversal.values()))
        _validate_nonnegative(conn)
        db.audit(conn, "reversed", "movement", movement_id, before=old, after=reversal)
    current = conn.execute("SELECT * FROM movements WHERE id=?", (movement_id,)).fetchone()
    return {"movement": dict(current), "reversal": reversal, "duplicate": False}


def set_movement_status(conn, movement_id, status):
    if status not in ("posted", "pending"):
        raise ValueError("Estado inválido.")
    old = conn.execute("SELECT * FROM movements WHERE id=?", (movement_id,)).fetchone()
    if not old or old["status"] == "reversed" or old["type"] == "reversal":
        raise ValueError("El movimiento no puede cambiar de estado.")
    if _metadata(old).get("reversed_by"):
        raise ValueError("Un movimiento revertido no puede cambiar de estado.")
    with conn:
        conn.execute("UPDATE movements SET status=? WHERE id=?", (status, movement_id))
        _validate_nonnegative(conn)
        db.audit(conn, "status_changed", "movement", movement_id, before=dict(old), after={"status": status})
    return dict(conn.execute("SELECT * FROM movements WHERE id=?", (movement_id,)).fetchone())


def _cash_totals(rows):
    contributions, withdrawals = ZERO, ZERO
    for row in rows:
        kind = row["type"]
        if kind == "reversal":
            kind = _metadata(row).get("original_type")
        if kind in ("initial", "subscription"):
            contributions += dec(row["amount_usd"])
        elif kind == "redemption":
            withdrawals -= dec(row["amount_usd"])
    return contributions, withdrawals


def _fund_allocation(snapshot, positions):
    mep = dec(snapshot["mep"])
    if mep <= 0:
        return []
    items = [{"symbol": row["symbol"], "name": row["name"] or row["symbol"],
              "category": row["category"], "value_usd": dec(row["value_usd"])} for row in positions]
    for currency, value in (("ARS", dec(snapshot["cash_ars"]) / mep), ("USD", dec(snapshot["cash_usd"]))):
        if value:
            items.append({"symbol": currency, "name": f"Efectivo en {currency}",
                          "category": f"Cash {currency}", "value_usd": value})
    if not positions and dec(snapshot["securities_ars"]):
        items.append({"symbol": "PORTFOLIO", "name": "Títulos · detalle histórico no disponible",
                      "category": "Títulos", "value_usd": dec(snapshot["securities_ars"]) / mep})
    return items


def _report_movement(row):
    movement = {"id": row["id"], "date": row["effective_at"], "type": row["type"],
                "amount_usd": row["amount_usd"], "units": row["units"], "note": row["note"]}
    if row["currency"] == "ARS":
        movement.update(currency="ARS", original_amount=row["original_amount"], fx=row["fx"])
    return movement


def build_reports(conn, *, raw_root=None) -> dict:
    snapshots = [dict(row) for row in conn.execute("SELECT * FROM account_snapshots ORDER BY as_of,id")]
    if not snapshots:
        return {"schema_version": 1, "version": "empty", "as_of": None, "unit_value": "0",
                "participants": [], "summary": {}, "pending": [],
                "issues": [{"code": "missing_close", "message": "Todavía no hay cierres importados o descargados.",
                            "severity": "error", "blocking": True}]}
    latest = snapshots[-1]
    latest_metadata = _metadata(latest)
    latest_rows = eligible_movements(conn, latest)
    total_units = sum((dec(row["units"]) for row in latest_rows), ZERO)
    recorded_units, unit_value = dec(latest["units"]), dec(latest["unit_value"])
    issues = []
    if abs(total_units - recorded_units) > UNIT_TOLERANCE:
        issues.append({"code": "units_reconciliation", "severity": "error", "blocking": True,
                       "message": f"Las cuotapartes de los beneficiarios ({total_units}) no concilian con el cierre ({recorded_units})."})
    calculated_total = total_units * unit_value
    if abs(calculated_total - dec(latest["total_usd"])) > MONEY_TOLERANCE:
        issues.append({"code": "valuation_reconciliation", "severity": "error", "blocking": True,
                       "message": "La suma de posiciones individuales no concilia con la valuación del cierre."})
    if dec(latest["mep"]) <= 0 or unit_value <= 0 or recorded_units <= 0:
        issues.append({"code": "invalid_close", "severity": "error", "blocking": True,
                       "message": "El cierre no tiene tipo de cambio, valor unitario o cuotapartes válidos."})
    migration_rows = conn.execute("SELECT counts_json FROM migration_log").fetchall()
    for row in migration_rows:
        issues.extend(json.loads(row["counts_json"]).get("differences", []))
    all_movements = [dict(row) for row in conn.execute("SELECT * FROM movements ORDER BY effective_at,source_key,id")]
    allowed_ids = {row["id"] for row in latest_rows}
    pending = [{"id": row["id"], "participant_id": row["participant_id"], "date": row["effective_at"],
                "type": row["type"], "amount_usd": row["amount_usd"], "units": row["units"],
                "status": row["status"], "note": row["note"],
                "reason": "Pendiente de acreditación" if row["status"] == "pending" else "Pendiente del próximo cierre"}
               for row in all_movements
               if row["status"] == "pending" or (row["status"] == "posted" and row["id"] not in allowed_ids)]
    membership = [(snapshot, eligible_movements(conn, snapshot)) for snapshot in snapshots]
    positions = classified_positions(conn, latest["id"], latest_metadata, raw_root)
    fund_allocation = _fund_allocation(latest, positions)
    fund_total = dec(latest["total_usd"])
    participants = []
    for participant in conn.execute("SELECT * FROM participants ORDER BY rowid"):
        participant_id = participant["id"]
        movements = [row for row in latest_rows if row["participant_id"] == participant_id]
        units = sum((dec(row["units"]) for row in movements), ZERO)
        contributions, withdrawals = _cash_totals(movements)
        balance = units * unit_value
        history = []
        initial = [row for row in all_movements if row["participant_id"] == participant_id
                   and row["type"] == "initial" and row["status"] == "posted"]
        if initial and initial[0]["effective_at"] < snapshots[0]["as_of"]:
            initial_units = sum((dec(row["units"]) for row in initial), ZERO)
            initial_amount = sum((dec(row["amount_usd"]) for row in initial), ZERO)
            history.append({"date": initial[0]["effective_at"], "balance_usd": decimal_text(initial_amount),
                            "unit_value": initial[0]["unit_value"], "units": decimal_text(initial_units),
                            "contributions_usd": decimal_text(initial_amount), "withdrawals_usd": "0"})
        for snapshot, snapshot_rows in membership:
            own = [row for row in snapshot_rows if row["participant_id"] == participant_id]
            historical_units = sum((dec(row["units"]) for row in own), ZERO)
            historical_contributions, historical_withdrawals = _cash_totals(own)
            history.append({"date": snapshot["as_of"],
                            "balance_usd": decimal_text(historical_units * dec(snapshot["unit_value"])),
                            "unit_value": snapshot["unit_value"], "units": decimal_text(historical_units),
                            "contributions_usd": decimal_text(historical_contributions),
                            "withdrawals_usd": decimal_text(historical_withdrawals)})
        share = units / recorded_units if recorded_units else ZERO
        allocation = [{**item, "value_usd": decimal_text(item["value_usd"] * share),
                       "weight": decimal_text(item["value_usd"] / fund_total if fund_total else ZERO)}
                      for item in fund_allocation]
        report = {"participant": dict(participant), "as_of": latest["as_of"],
                  "quote_as_of": latest_metadata.get("quote_as_of"),
                  "unit_value": decimal_text(unit_value), "balance_usd": decimal_text(balance),
                  "contributions_usd": decimal_text(contributions), "withdrawals_usd": decimal_text(withdrawals),
                  "profit_usd": decimal_text(balance + withdrawals - contributions),
                  "units": decimal_text(units), "unit_return_pct": decimal_text((unit_value - 1) * 100),
                  "history": history,
                  "movements": [_report_movement(row) for row in movements], "allocation": allocation,
                  "allocation_status": "detailed" if positions else "historical_summary"}
        participants.append({"id": participant_id, "name": participant["name"], "report": report})
    version_input = {"as_of": latest["as_of"], "participants": participants,
                     "raw_hash": latest["raw_hash"]}
    version = hashlib.sha256(db.json_text(version_input).encode("utf-8")).hexdigest()[:20]
    return {"schema_version": 1, "version": version, "as_of": latest["as_of"],
            "unit_value": decimal_text(unit_value), "participants": participants,
            "summary": {"total_usd": latest["total_usd"], "total_units": decimal_text(total_units),
                        "unit_value": decimal_text(unit_value), "as_of": latest["as_of"],
                        "cash_ars": latest["cash_ars"], "cash_usd": latest["cash_usd"],
                        "securities_ars": latest["securities_ars"], "mep": latest["mep"],
                        "quote_as_of": latest_metadata.get("quote_as_of"),
                        "pending_count": len(pending)}, "issues": issues, "pending": pending}
