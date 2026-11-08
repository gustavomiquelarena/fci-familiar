"""Publicación de una versión completa; Git sólo recibe reportes cifrados."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.request
import uuid

from .config import Config, REPO_ROOT, atomic_json
from .crypto import encrypt_report, decrypt_report
from .db import utcnow


def initialize_publications(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS publications(
      id TEXT PRIMARY KEY, version TEXT NOT NULL, as_of TEXT NOT NULL,
      created_at TEXT NOT NULL, state TEXT NOT NULL,
      git_sha TEXT, error TEXT, payload_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    """)
    conn.commit()


@contextmanager
def process_lock(root: Path):
    """Non-blocking kernel lock, released even when the process dies."""
    root.mkdir(parents=True, exist_ok=True)
    file = (root / "process.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            file.seek(0)
            if not file.read(1):
                file.write(b"0")
                file.flush()
            file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Ya hay otra actualización o publicación en ejecución") from None
        else:
            import fcntl
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("Ya hay otra actualización o publicación en ejecución") from None
        yield
    finally:
        file.close()


def _git(*args: str, check: bool = True, timeout: int = 90) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    result = subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout)
    if check and result.returncode:
        # Never return credential-helper output, remote errors can include credentials.
        raise RuntimeError("Git no pudo completar la operación. Revisá la conexión y el acceso al repositorio")
    return result


def ensure_repository(config: Config) -> None:
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", config.repository_url):
        raise ValueError("Configurá una URL HTTPS de repositorio GitHub sin credenciales")
    if not (REPO_ROOT / ".git").exists():
        _git("init", "-b", "main")
    remote = _git("remote", "get-url", "origin", check=False)
    if remote.returncode:
        _git("remote", "add", "origin", config.repository_url)
    elif remote.stdout.strip().removesuffix(".git") != config.repository_url.removesuffix(".git"):
        raise ValueError("El repositorio configurado no coincide con origin")


def assert_public_tree() -> None:
    """Allowlist both checkout files and tracked contents before any push."""
    allowed_roots = {"local", "docs", "tests", "scripts", ".github"}
    allowed_files = {".gitignore", "README.md", "pyproject.toml", "requirements.txt", "LICENSE"}
    for entry in REPO_ROOT.iterdir():
        if entry.name in {".git", ".venv", "__pycache__"}:
            continue
        if entry.name not in allowed_roots | allowed_files:
            raise ValueError(f"Archivo inesperado fuera de la lista pública: {entry.name}")
    for file in REPO_ROOT.rglob("*"):
        relative = file.relative_to(REPO_ROOT)
        if any(p in {".git", ".venv", "__pycache__"} for p in relative.parts) or not file.is_file():
            continue
        if file.suffix.lower() in {".xlsx", ".xls", ".csv", ".ini", ".db", ".sqlite3", ".sqlite", ".log", ".pem", ".pfx"} or file.name == ".env":
            raise ValueError("Hay un archivo privado dentro del checkout público")
        if relative.parts[:2] == ("docs", "data") and file.name != "manifest.json":
            value = json.loads(file.read_text(encoding="utf-8"))
            if set(value) != {"version", "kdf", "iterations", "salt", "iv", "ciphertext"}:
                raise ValueError("Se detectó un reporte sin el sobre de cifrado esperado")
        if file.stat().st_size < 2_000_000 and file.suffix in {".py", ".js", ".json", ".html", ".md", ".yml", ".yaml"}:
            content = file.read_text(encoding="utf-8")
            if re.search(r"(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)", content):
                raise ValueError("Se detectó un posible secreto en un archivo público")


def current_payload(conn) -> dict | None:
    row = conn.execute("SELECT payload_json FROM publications WHERE state='published' ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def stage_reports(bundle: dict, passwords: dict[str, str], docs: Path | None = None) -> dict:
    directory = (docs or REPO_ROOT / "docs") / "data"
    directory.mkdir(parents=True, exist_ok=True)
    version = str(bundle.get("version") or uuid.uuid4().hex)
    manifest = {"schema_version": 1, "version": version, "as_of": bundle["as_of"], "participants": []}
    # All new files are prepared before the manifest is replaced.
    filenames = set()
    for participant in bundle["participants"]:
        pid = participant["id"]
        if pid not in passwords or not passwords[pid]:
            raise ValueError("Configurá las contraseñas de los tres hijos antes de publicar")
        envelope = encrypt_report(participant["report"], passwords[pid])
        digest = hashlib.sha256(json.dumps(envelope, sort_keys=True).encode()).hexdigest()[:16]
        filename = f"{pid}-{digest}.enc"
        filenames.add(filename)
        atomic_json(directory / filename, envelope)
        manifest["participants"].append({"id": pid, "name": participant["name"], "file": f"data/{filename}"})
    atomic_json(directory / "manifest.json", manifest)
    for file in directory.glob("*.enc"):
        if file.name not in filenames:
            file.unlink()
    return manifest


def publish(conn, config: Config, passwords: dict[str, str], *, bundle: dict | None = None,
            push: bool = True, verify: bool = True) -> dict:
    from .finance import build_reports
    initialize_publications(conn)
    bundle = bundle if bundle is not None else build_reports(conn)
    if not bundle.get("as_of") or not bundle.get("participants"):
        raise ValueError("Primero importá el histórico o descargá un cierre válido")
    blocking = [i for i in bundle.get("issues", []) if isinstance(i, dict) and (i.get("blocking") or i.get("severity") == "error")]
    if blocking:
        raise ValueError("Hay diferencias pendientes de conciliación; revisá Actualización")
    if not push:
        previous = conn.execute("SELECT * FROM publications WHERE state='prepared' ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
        try:
            manifest = json.loads((REPO_ROOT / "docs/data/manifest.json").read_text(encoding="utf-8"))
            if previous and manifest["version"] == previous["version"]:
                expected_reports = {p["id"]: p["report"] for p in bundle["participants"]}
                same = manifest["as_of"] == bundle["as_of"] and len(manifest["participants"]) == len(expected_reports)
                for p in manifest["participants"]:
                    filename = p["file"]
                    if not re.fullmatch(r"data/[a-z]+-[a-f0-9]{16}\.enc", filename):
                        same = False
                        break
                    envelope = json.loads((REPO_ROOT / "docs" / filename).read_text(encoding="utf-8"))
                    if decrypt_report(envelope, passwords[p["id"]]) != expected_reports[p["id"]]:
                        same = False
                        break
                if same:
                    return {"id": previous["id"], "state": "prepared", "as_of": bundle["as_of"], "reused": True}
        except Exception:
            pass
    publication_id = uuid.uuid4().hex
    # Fresh version also makes password reset propagation verifiable.
    bundle = dict(bundle, version=publication_id)
    conn.execute("INSERT INTO publications(id,version,as_of,created_at,state,git_sha,error,payload_json) VALUES(?,?,?,?,?,?,?,?)", (publication_id, publication_id, bundle["as_of"], utcnow(), "preparing", None, None, json.dumps(bundle, ensure_ascii=False)))
    conn.commit()
    try:
        manifest = stage_reports(bundle, passwords)
        assert_public_tree()
        if not push:
            conn.execute("UPDATE publications SET state='prepared' WHERE id=?", (publication_id,))
            conn.commit()
            return {"id": publication_id, "state": "prepared", "as_of": bundle["as_of"]}
        ensure_repository(config)
        # Weekly publishing owns only generated data. Source changes are committed
        # separately after development checks, never swept into a password reset.
        already_staged = _git("diff", "--cached", "--name-only").stdout.splitlines()
        if any(not p.startswith("docs/data/") for p in already_staged):
            raise ValueError("Hay cambios de código preparados en Git; completá esa revisión antes de publicar reportes")
        _git("add", "--", "docs/data")
        staged = _git("diff", "--cached", "--name-only").stdout.splitlines()
        if any(Path(p).suffix.lower() in {".xlsx", ".ini", ".db", ".sqlite3", ".csv"} for p in staged):
            raise ValueError("Git contiene un archivo privado preparado para publicar")
        _git("commit", "-m", f"Actualizar reportes cifrados: {bundle['as_of']}")
        sha = _git("rev-parse", "HEAD").stdout.strip()
        _git("push", "-u", "origin", "main", timeout=120)
        conn.execute("UPDATE publications SET state='pushed',git_sha=? WHERE id=?", (sha, publication_id))
        conn.commit()
        if not verify or not config.pages_url:
            return {"id": publication_id, "state": "pushed", "git_sha": sha, "as_of": bundle["as_of"], "url": config.pages_url}
        verify_publication(config.pages_url, manifest)
        conn.execute("UPDATE publications SET state='published' WHERE id=?", (publication_id,))
        conn.commit()
        return {"id": publication_id, "state": "published", "git_sha": sha, "as_of": bundle["as_of"], "url": config.pages_url}
    except Exception as exc:
        conn.execute("UPDATE publications SET state='failed',error=? WHERE id=?", (str(exc)[:500], publication_id))
        conn.commit()
        raise


def verify_publication(pages_url: str, expected: dict, *, timeout_seconds: int = 110) -> None:
    url = pages_url.rstrip("/") + "/data/manifest.json?version=" + expected["version"]
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                manifest = json.loads(response.read(200_000))
            if manifest.get("schema_version") != 1 or manifest.get("as_of") != expected.get("as_of") or manifest.get("version") != expected["version"] or manifest.get("participants") != expected.get("participants"):
                raise ValueError()
            for participant in manifest["participants"]:
                filename = participant["file"]
                if not re.fullmatch(r"data/[a-z]+-[a-f0-9]{16}\.enc", filename):
                    raise ValueError()
                with urllib.request.urlopen(pages_url.rstrip("/") + "/" + filename, timeout=10) as response:
                    envelope = json.loads(response.read(5_000_000))
                if envelope.get("kdf") != "PBKDF2-SHA256" or "ciphertext" not in envelope:
                    raise ValueError()
                digest = hashlib.sha256(json.dumps(envelope, sort_keys=True).encode()).hexdigest()[:16]
                if not filename.endswith("-" + digest + ".enc"):
                    raise ValueError()
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError("El push terminó, pero Pages todavía no confirmó esta versión. Reintentá la verificación") from None
            time.sleep(5)
