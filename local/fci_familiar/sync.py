"""Private, auditable ingestion of IOL observations and weekly fund closes."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from uuid import uuid4

from . import db
from .iol import IOLDataError, IOLError

try:
    from zoneinfo import ZoneInfo
    ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")
except (ImportError, Exception):
    # Windows installations without tzdata still use the agreed Argentina clock.
    ARGENTINA = timezone(timedelta(hours=-3))


def number(value, label="importe") -> Decimal:
    if isinstance(value, bool) or value is None:
        raise IOLDataError(f"IOL no informó un {label} válido")
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise IOLDataError(f"IOL no informó un {label} válido") from None
    if not result.is_finite():
        raise IOLDataError(f"IOL no informó un {label} finito")
    return result


def _normalized(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in value if not unicodedata.combining(c)).lower())


def _when(value=None):
    if value is None:
        return datetime.now(ARGENTINA)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, 18, tzinfo=ARGENTINA)
    if not isinstance(value, datetime):
        raise ValueError("now debe ser una fecha u hora")
    return value.replace(tzinfo=ARGENTINA) if value.tzinfo is None else value.astimezone(ARGENTINA)


def historical_closes(records, today):
    """Exclude the entire current day; choose the latest timestamp per prior day.

    Historical API dates denote the exchange business date. They are not shifted
    to a different date merely because a provider serializes midnight with Z.
    """
    selected = {}
    for record in records:
        if not isinstance(record, dict):
            raise IOLDataError("La serie histórica contiene una fila inválida")
        timestamp = record.get("fechaHora", record.get("fecha"))
        try:
            business_date = date.fromisoformat(str(timestamp)[:10])
        except (TypeError, ValueError):
            raise IOLDataError("La serie histórica contiene una fecha inválida") from None
        if business_date >= today:
            continue
        price = number(record.get("ultimoPrecio", record.get("cierre")), "precio de cierre")
        if price <= 0:
            raise IOLDataError("La serie histórica contiene un cierre no positivo")
        date_key, stamp_key = business_date.isoformat(), str(timestamp)
        old = selected.get(date_key)
        if old is None or stamp_key > old[0]:
            selected[date_key] = (stamp_key, price)
        elif stamp_key == old[0] and price != old[1]:
            raise IOLDataError("IOL informó dos cierres incompatibles para una misma fecha")
    return {key: value[1] for key, value in selected.items()}


def normalize_account(payload):
    """Identify investment account rows by type, never by array position.

    Retains the accounting definition used by the imported workbook: ARS cash
    is total minus marked securities; USD cash is saldo minus securities. A
    portfolio reconciliation independently checks that securities are counted
    once. Available cash and unsettled cash are not silently interchanged.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("cuentas"), list):
        raise IOLDataError("El estado de cuenta no contiene cuentas válidas")
    ars, usd = [], []
    for account in payload["cuentas"]:
        if not isinstance(account, dict):
            raise IOLDataError("El estado de cuenta contiene una cuenta inválida")
        kind = _normalized(account.get("tipo"))
        if kind == "inversionargentinapesos":
            ars.append(account)
        elif kind == "inversionargentinadolares":
            usd.append(account)
        elif "inversion" in kind and ("ccl" in kind or "exterior" in kind):
            values = [number(account.get(key, 0)) for key in ("total", "saldo", "titulosValorizados")]
            if any(value != 0 for value in values):
                raise IOLDataError("Hay saldo exterior/CCL que requiere identificar su valuación antes del cierre")
    if len(ars) != 1 or len(usd) != 1:
        raise IOLDataError("No se identificaron de forma única las cuentas de inversión ARS y USD")
    ars, usd = ars[0], usd[0]
    securities = number(ars.get("titulosValorizados"), "total de títulos en ARS")
    cash_ars = number(ars.get("total"), "total de cuenta ARS") - securities
    cash_usd = number(usd.get("saldo"), "saldo USD") - number(
        usd.get("titulosValorizados"), "total de títulos en USD")
    if securities < 0:
        raise IOLDataError("La valuación de títulos ARS es negativa")
    return {"cash_ars": cash_ars, "cash_usd": cash_usd, "securities_ars": securities}


def _currency(value):
    value = _normalized(value)
    if value in ("ars", "peso", "pesos", "pesoargentino", "pesosargentinos", "0"):
        return "ARS"
    if value in ("usd", "dolar", "dolares", "dolarestaounidense", "dolarestadounidense",
                  "dolaresestadounidenses", "dolarbillete", "dolarmep", "1"):
        return "USD"
    if any(s in value for s in ("ccl", "exterior", "cable")):
        return "USD_CCL"
    return None


def _category(value):
    value = _normalized(value)
    if "cedear" in value:
        return "CEDEAR"
    if "obligacion" in value or value == "on":
        return "Obligaciones negociables"
    if any(s in value for s in ("bono", "titulopublic", "titulospublic", "letra")):
        return "Bonos y letras"
    if "accion" in value:
        return "Acciones"
    if "fondo" in value or value == "fci":
        return "Fondos comunes"
    return "Otros"


def normalize_portfolio(payload, securities_ars, mep):
    """Reconcile declared currency or a broker's common ARS marked-value basis.

    The ARS-basis alternative is accepted only when its total independently
    matches account securities. Unknown USD/CCL amounts never become USD by
    stripping a ticker suffix. No market values are fabricated from trade cost.
    """
    assets = payload.get("activos") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        raise IOLDataError("La cartera no contiene una lista de activos válida")
    rows, explicit = [], []
    explicit_complete = True
    for asset in assets:
        if not isinstance(asset, dict):
            raise IOLDataError("La cartera contiene un activo inválido")
        title = asset.get("titulo") or {}
        if not isinstance(title, dict):
            raise IOLDataError("La cartera contiene un título inválido")
        symbol = title.get("simbolo", asset.get("simbolo"))
        if not isinstance(symbol, str) or not symbol.strip():
            raise IOLDataError("Un activo de cartera no tiene símbolo")
        quantity = number(asset.get("cantidad"), "cantidad de títulos")
        value = number(asset.get("valorizado", asset.get("valorizacion")), "valuación del activo")
        if quantity < 0 or value < 0:
            raise IOLDataError("La cartera contiene una posición negativa no contemplada por este fondo")
        if quantity == 0 and value == 0:
            continue
        declared = _currency(asset.get("moneda", title.get("moneda", payload.get("moneda"))))
        if declared == "ARS":
            explicit.append(value / mep)
        elif declared == "USD":
            explicit.append(value)
        else:
            explicit_complete = False
            explicit.append(None)
        rows.append({"symbol": symbol.strip(),
                     "name": str(title.get("descripcion", asset.get("descripcion", symbol))),
                     "category": _category(title.get("tipo", asset.get("tipo"))),
                     "quantity": quantity, "raw_value": value, "declared_currency": declared})
    expected = securities_ars / mep
    tolerance = Decimal("0.02")
    explicit_total = sum((value for value in explicit if value is not None), Decimal(0))
    ars_total = sum((row["raw_value"] for row in rows), Decimal(0)) / mep
    if explicit_complete and abs(explicit_total - expected) <= tolerance:
        basis = "declared_currency"
        for row, value in zip(rows, explicit):
            row.update(currency=row["declared_currency"], value_usd=value)
    elif abs(ars_total - expected) <= tolerance:
        basis = "account_reconciled_ARS"
        for row in rows:
            row.update(currency="ARS", value_usd=row["raw_value"] / mep)
    else:
        raise IOLDataError("La cartera por instrumento no concilia con los títulos del estado de cuenta; revisar monedas y valuaciones")
    # Multiple entries of an identical instrument/currency represent one holding.
    grouped = {}
    for row in rows:
        key = (row["symbol"], row["currency"])
        if key in grouped:
            grouped[key]["quantity"] += row["quantity"]
            grouped[key]["value_usd"] += row["value_usd"]
        else:
            grouped[key] = row.copy()
    return list(grouped.values()), basis


def _monthly_windows(start, end):
    cursor = start
    while cursor <= end:
        following = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        stop = min(end, following - timedelta(days=1))
        yield cursor, stop
        cursor = following


def _private_raw(directory, name, payload):
    serialized = db.json_text(payload).encode("utf-8")
    destination = directory / (name + ".json")
    with destination.open("xb") as stream:
        stream.write(serialized)
    return hashlib.sha256(serialized).hexdigest()


def sync_account(conn, client, raw_dir, now=None):
    """Fetch first, validate, then atomically merge; failed runs keep raw evidence.

    This routine updates the local approved-close candidate, not GitHub. The
    publisher performs financial validation before exposing any new report.
    """
    observed = _when(now)
    today = observed.date()
    quote_end = today - timedelta(days=1)
    inception_row = conn.execute("SELECT MIN(effective_at) AS first FROM movements").fetchone()
    if not inception_row or not inception_row["first"]:
        raise IOLDataError("Importar el histórico del fondo antes de actualizar IOL")
    inception = date.fromisoformat(inception_row["first"][:10])
    last_monthly = conn.execute("""SELECT started_at FROM runs WHERE status='success'
        AND json_extract(summary_json,'$.monthly_reconciliation')=1
        ORDER BY started_at DESC LIMIT 1""").fetchone()
    monthly = not last_monthly or last_monthly["started_at"][:7] != observed.isoformat()[:7]
    op_start = inception if monthly else max(inception, today - timedelta(days=30))
    quote_start = max(date(2024, 1, 1), today - timedelta(days=30))
    run_id = uuid4().hex
    archive = Path(raw_dir) / f"{observed.strftime('%Y%m%dT%H%M%S')}_{run_id}"
    archive.mkdir(parents=True, exist_ok=False)
    conn.execute("INSERT INTO runs(id,started_at,status) VALUES(?,?,?)",
                 (run_id, observed.isoformat(timespec="seconds"), "running"))
    conn.commit()
    try:
        operations, quote_sets, warnings = [], {}, []
        windows = _monthly_windows(op_start, today) if monthly else [(op_start, today)]
        for index, (start, end) in enumerate(windows):
            result = client.get_operations(start, end)
            _private_raw(archive, f"operations_{index:03d}", result)
            operations.extend(result)
            if not getattr(client, "date_filter_supported", True):
                warnings.append("IOL no admite el filtro de fechas; la incorporación local sigue siendo incremental")
                break
        account_payload = client.get_account()
        _private_raw(archive, "account", account_payload)
        portfolio_payload = client.get_portfolio()
        _private_raw(archive, "portfolio", portfolio_payload)
        for symbol in ("AL30", "AL30D", "GD30", "GD30D"):
            try:
                quotes = client.get_historical(symbol, quote_start, quote_end)
                _private_raw(archive, "quotes_" + symbol, quotes)
                quote_sets[symbol] = historical_closes(quotes, today)
            except IOLError:
                if symbol in ("AL30", "AL30D"):
                    raise
                warnings.append(f"No se actualizó la serie auxiliar {symbol}")
        shared_dates = sorted(set(quote_sets["AL30"]) & set(quote_sets["AL30D"]))
        if not shared_dates:
            raise IOLDataError("No hay cierres AL30 y AL30D de la misma fecha para calcular MEP")
        mep_quotes = {day: (quote_sets["AL30"][day] / quote_sets["AL30D"][day]).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP) for day in shared_dates}
        quote_day = shared_dates[-1]
        mep = mep_quotes[quote_day]
        if mep <= 0:
            raise IOLDataError("El cierre MEP no es positivo")
        if (today - date.fromisoformat(quote_day)).days > 10:
            raise IOLDataError("El cierre MEP disponible tiene más de diez días; revisar cobertura antes de publicar")
        balances = normalize_account(account_payload)
        positions, portfolio_basis = normalize_portfolio(portfolio_payload,
            balances["securities_ars"], mep)
        eligible = conn.execute("""SELECT id,participant_id,units FROM movements
          WHERE status='posted' AND substr(effective_at,1,10)<=?
          ORDER BY effective_at,created_at,id""", (today.isoformat(),)).fetchall()
        person_units = Counter()
        for row in eligible:
            person_units[row["participant_id"]] += number(row["units"], "cantidad de cuotapartes")
        if any(value < 0 for value in person_units.values()):
            raise IOLDataError("Un beneficiario tiene cuotapartes negativas al cierre")
        units = sum(person_units.values(), Decimal(0))
        if units <= 0:
            raise IOLDataError("El fondo no tiene cuotapartes positivas al cierre")
        total = balances["cash_usd"] + (balances["cash_ars"] + balances["securities_ars"]) / mep
        if total <= 0:
            raise IOLDataError("El fondo no tiene una valuación total positiva")
        unit_value = total / units
        snapshot_id = "iol:" + today.isoformat()
        core = {**{key: str(value) for key, value in balances.items()}, "mep": str(mep),
                "total_usd": str(total), "units": str(units), "unit_value": str(unit_value),
                "included_movement_ids": [row["id"] for row in eligible],
                "positions": [{key: str(value) for key, value in position.items()
                               if key in ("symbol", "name", "category", "currency", "quantity", "value_usd")}
                              for position in positions]}
        raw_hash = hashlib.sha256(db.json_text(core).encode("utf-8")).hexdigest()
        old = conn.execute("SELECT * FROM account_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        snapshot_status = "unchanged" if old and old["raw_hash"] == raw_hash else ("updated" if old else "inserted")
        op_counts, quote_counts, operation_coverage = Counter(), Counter(), Counter()
        # Each account operation is an aggregate keyed by numero. Repeated pages
        # or monthly boundaries must not multiply aggregate executions.
        deduplicated = {}
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("numero") is None:
                raise IOLDataError("Una operación descargada no contiene número identificador")
            key = str(operation["numero"])
            previous = deduplicated.get(key)
            if previous is not None and db.json_text(previous) != db.json_text(operation):
                raise IOLDataError("IOL devolvió versiones incompatibles de una operación en la misma descarga")
            deduplicated[key] = operation
        with conn:
            for number_key, payload in deduplicated.items():
                action = db.upsert_operation(conn, "iol", number_key, payload)
                op_counts[action] += 1
                # IOL also lists finished informational events with no executed
                # amount. Preserve them for audit, but never call them money
                # flows or infer a family contribution from their presence.
                amount_class = ("with_execution_amount" if payload.get("montoOperado") is not None
                                else "without_execution_amount")
                operation_coverage[amount_class] += 1
                if action == "inserted":
                    operation_coverage["inserted_" + amount_class] += 1
            for symbol, series in quote_sets.items():
                for day, price in series.items():
                    quote_counts[db.upsert_quote(conn, symbol, day, str(price), "IOL:historical")] += 1
            for day, price in mep_quotes.items():
                quote_counts[db.upsert_quote(conn, "MEP", day, str(price), "IOL:AL30/AL30D")] += 1
            if snapshot_status != "unchanged":
                metadata = {"observed_at": observed.isoformat(timespec="seconds"),
                    "quote_as_of": quote_day, "portfolio_basis": portfolio_basis,
                    "included_movement_ids": [row["id"] for row in eligible],
                    "run_id": run_id, "raw_directory": str(archive)}
                conn.execute("""INSERT INTO account_snapshots(id,as_of,cash_ars,cash_usd,
                    securities_ars,mep,total_usd,units,unit_value,source,raw_hash,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    cash_ars=excluded.cash_ars,cash_usd=excluded.cash_usd,
                    securities_ars=excluded.securities_ars,mep=excluded.mep,
                    total_usd=excluded.total_usd,units=excluded.units,
                    unit_value=excluded.unit_value,raw_hash=excluded.raw_hash,
                    metadata_json=excluded.metadata_json""",
                    (snapshot_id, today.isoformat(), str(balances["cash_ars"]),
                     str(balances["cash_usd"]), str(balances["securities_ars"]), str(mep),
                     str(total), str(units), str(unit_value), "IOL", raw_hash, db.json_text(metadata)))
                conn.execute("DELETE FROM positions WHERE snapshot_id=?", (snapshot_id,))
                conn.executemany("""INSERT INTO positions(snapshot_id,symbol,name,category,
                    currency,quantity,value_usd) VALUES(?,?,?,?,?,?,?)""",
                    [(snapshot_id, row["symbol"], row["name"], row["category"], row["currency"],
                      str(row["quantity"]), str(row["value_usd"])) for row in positions])
                db.audit(conn, snapshot_status, "snapshot", snapshot_id,
                         dict(old) if old else None, core)
            summary = {"run_id": run_id, "as_of": today.isoformat(), "quote_as_of": quote_day,
                "snapshot_id": snapshot_id, "snapshot_status": snapshot_status,
                "operations": {key: op_counts[key] for key in ("inserted", "updated", "unchanged")},
                "operation_coverage": dict(operation_coverage),
                "quotes": {key: quote_counts[key] for key in ("inserted", "updated", "unchanged")},
                "portfolio_count": len(positions), "portfolio_basis": portfolio_basis,
                "total_usd": str(total), "units": str(units), "unit_value": str(unit_value),
                "monthly_reconciliation": monthly, "warnings": warnings,
                "raw_directory": str(archive)}
            conn.execute("UPDATE runs SET completed_at=?,status='success',summary_json=? WHERE id=?",
                         (db.utcnow(), db.json_text(summary), run_id))
        return summary
    except Exception as exc:
        conn.rollback()
        # Known integration errors are deliberately safe; unexpected errors are
        # classified by type only. Raw API evidence remains in its private folder.
        message = str(exc) if isinstance(exc, IOLError) else "Error interno al procesar la actualización"
        with conn:
            conn.execute("UPDATE runs SET completed_at=?,status='failed',summary_json=? WHERE id=?",
                         (db.utcnow(), db.json_text({"error": message,
                                                   "error_type": type(exc).__name__}), run_id))
        raise
