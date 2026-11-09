"""Classify the same saved positions for the administrator and family reports."""
import json
from pathlib import Path

from .sync import _normalized


def _public_security_symbols(raw_root, metadata):
    # The saved "Bonos y letras" category can contain generic bonds. Use the
    # archived broker classification when it explicitly identifies public debt.
    archive = Path(metadata.get("raw_directory") or "").name
    if not archive or raw_root is None:
        return set()
    try:
        path = (raw_root / archive / "portfolio.json").resolve()
        if not path.is_relative_to(raw_root.resolve()):
            return set()
        assets = json.loads(path.read_text(encoding="utf-8"))["activos"]
        types = {}
        for asset in assets:
            title = asset.get("titulo") or {}
            symbol = title.get("simbolo") or asset.get("simbolo")
            kind = _normalized(title.get("tipo", asset.get("tipo")))
            if symbol and kind:
                types.setdefault(symbol.strip(), set()).add(kind)
        public_types = {"titulopublico", "titulospublicos"}
        return {symbol for symbol, kinds in types.items() if kinds <= public_types}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return set()


def classified_positions(conn, snapshot_id, metadata, raw_root=None):
    positions = [dict(row) for row in conn.execute("""SELECT symbol, name, category, quantity, value_usd
        FROM positions WHERE snapshot_id=? ORDER BY category, symbol, currency""", (snapshot_id,))]
    public_symbols = _public_security_symbols(raw_root, metadata) if any(
        row["category"] == "Bonos y letras" for row in positions) else set()
    labels = {"Acciones": "Acciones argentinas", "Otros": "Sin clasificar"}
    for position in positions:
        category = position["category"]
        if category == "Bonos y letras" and position["symbol"] in public_symbols:
            position["category"] = "Títulos públicos"
        else:
            position["category"] = labels.get(category, category)
    return positions
