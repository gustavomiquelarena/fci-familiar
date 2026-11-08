"""Casos de uso compartidos por la interfaz local y la tarea semanal."""
from __future__ import annotations

from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from .config import Config, REPO_ROOT, atomic_json, replace_file
from .credentials import SecretStore
from . import db, finance, importer, publication


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.secrets = SecretStore()
        if config.root == REPO_ROOT or REPO_ROOT in config.root.parents:
            raise ValueError("La base privada debe estar fuera del repositorio público")
        self.config.root.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            db.initialize(conn)
            publication.initialize_publications(conn)

    @contextmanager
    def connection(self):
        conn = db.connect(self.config.database)
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def secret_target(pid: str, pending: bool = False) -> str:
        if pid not in {"tommy", "juli", "pili"}:
            raise ValueError("Participante inválido")
        return "FCIFamiliar/viewer/" + pid + ("/pending" if pending else "")

    def secret_or_none(self, target: str) -> str | None:
        try:
            return self.secrets.get(target)
        except Exception:
            return None

    def passwords(self) -> dict[str, str]:
        result = {}
        for pid in ("tommy", "juli", "pili"):
            value = self.secret_or_none(self.secret_target(pid, True)) or self.secret_or_none(self.secret_target(pid))
            if value:
                result[pid] = value
        if len(result) != 3:
            raise ValueError("Elegí las contraseñas de los tres hijos en Usuarios")
        return result

    def promote_passwords(self, used_passwords: dict[str, str]):
        for pid in ("tommy", "juli", "pili"):
            value = self.secret_or_none(self.secret_target(pid, True))
            if value and value == used_passwords.get(pid):
                self.secrets.set(self.secret_target(pid), value)
                self.secrets.delete(self.secret_target(pid, True))

    def state(self) -> dict:
        with self.connection() as conn:
            bundle = finance.build_reports(conn)
            participants = []
            for row in conn.execute("SELECT * FROM participants ORDER BY CASE id WHEN 'tommy' THEN 1 WHEN 'juli' THEN 2 ELSE 3 END"):
                participant = dict(row)
                participant["password_configured"] = bool(self.secret_or_none(self.secret_target(row["id"])))
                participant["password_pending"] = bool(self.secret_or_none(self.secret_target(row["id"], True)))
                report = next((p["report"] for p in bundle.get("participants", []) if p["id"] == row["id"]), {})
                participant.update({k: report.get(k) for k in ("balance_usd", "profit_usd", "units")})
                participants.append(participant)
            movements = [dict(row) for row in conn.execute("SELECT m.*,p.name AS participant_name FROM movements m JOIN participants p ON p.id=m.participant_id ORDER BY effective_at DESC,created_at DESC")]
            pubs = [dict(row) for row in conn.execute("SELECT id,version,as_of,created_at,state,git_sha,error FROM publications ORDER BY created_at DESC,rowid DESC LIMIT 12")]
            latest_runs = [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 12")]
        return {"summary": bundle.get("summary", {}), "participants": participants,
                "pending": bundle.get("pending", []), "issues": bundle.get("issues", []),
                "movements": movements, "publications": pubs, "runs": latest_runs,
                "config": {"repository_url": self.config.repository_url, "pages_url": self.config.pages_url,
                           "source_workbook": self.config.source_workbook, "encrypted_config_path": self.config.encrypted_config_path,
                           "iol_username": self.config.iol_username, "credentials_configured": bool(self.config.encrypted_config_path and self.config.iol_username),
                           "schedule": self.config.schedule, "data_dir": self.config.data_dir}}

    def import_source(self, path: str | None = None) -> dict:
        source = Path(path or self.config.source_workbook)
        if not source.is_absolute() or not source.is_file():
            raise ValueError("Indicá la ruta absoluta del Excel histórico")
        with publication.process_lock(self.config.root), self.connection() as conn:
            result = importer.import_workbook(conn, source)
            conn.commit()
        self.backup()
        return result

    def add_movement(self, data: dict) -> dict:
        data = dict(data)
        if isinstance(data.get("allocations"), list):
            allocations = data["allocations"]
            if len({a.get("participant_id") for a in allocations}) != len(allocations):
                raise ValueError("No repitas un participante en la distribución")
            data["allocations"] = {a["participant_id"]: a["amount"] for a in allocations}
        with publication.process_lock(self.config.root), self.connection() as conn:
            result = finance.add_movement(conn, data)
            conn.commit()
        self.backup()
        return result

    def reverse_movement(self, movement_id: str, reason: str) -> dict:
        with publication.process_lock(self.config.root), self.connection() as conn:
            result = finance.reverse_movement(conn, movement_id, reason)
            conn.commit()
        self.backup()
        return result

    def post_movement(self, movement_id: str) -> dict:
        with publication.process_lock(self.config.root), self.connection() as conn:
            result = finance.set_movement_status(conn, movement_id, "posted")
            conn.commit()
        self.backup()
        return result

    def sync(self) -> dict:
        from .iol import IOLClient
        from .sync import sync_account
        if not self.config.encrypted_config_path or not self.config.iol_username:
            raise ValueError("Configurá la ruta de credenciales y el usuario IOL")
        with publication.process_lock(self.config.root), self.connection() as conn:
            client = IOLClient(self.config.encrypted_config_path, self.config.iol_username, self.config.credential_target)
            try:
                result = sync_account(conn, client, self.config.root / "raw")
            finally:
                client.close()
        self.backup()
        return result

    def publish(self, *, bundle: dict | None = None, push: bool = True, verify: bool = True) -> dict:
        with publication.process_lock(self.config.root), self.connection() as conn:
            passwords = self.passwords()
            result = publication.publish(conn, self.config, passwords, bundle=bundle, push=push, verify=verify)
            if result["state"] == "published":
                self.promote_passwords(passwords)
        self.backup()
        return result

    def set_password(self, pid: str, password: str, confirm: str, *, publish: bool = True) -> dict:
        if not password or password != confirm:
            raise ValueError("Las contraseñas deben coincidir y no estar vacías")
        if len(password.encode("utf-8")) > 1024:
            raise ValueError("La contraseña es demasiado larga")
        with publication.process_lock(self.config.root):
            self.secrets.set(self.secret_target(pid, True), password)
        with self.connection() as conn:
            payload = publication.current_payload(conn)
            db.audit(conn, "password_pending", "participant", pid, None, {"changed": True})
            conn.commit()
        result = {"participant_id": pid, "state": "pending", "message": "Contraseña guardada, pendiente de publicación"}
        if publish and self.config.repository_url:
            try:
                self.passwords()
                # Existing users get the last PUBLISHED close, never unapproved new movements.
                result["publication"] = self.publish(bundle=payload)
                if result["publication"]["state"] == "published":
                    result.update(state="published", message="Contraseña publicada")
            except Exception as exc:
                result["message"] = "Contraseña guardada. " + str(exc)
        return result

    def restore_publication(self, publication_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT payload_json FROM publications WHERE id=? AND state='published'", (publication_id,)).fetchone()
            if not row:
                raise ValueError("Seleccioná una publicación verificada para recuperarla")
            bundle = json.loads(row[0])
        return self.publish(bundle=bundle)

    def update_settings(self, values: dict) -> dict:
        allowed = {"repository_url", "pages_url", "source_workbook", "encrypted_config_path", "iol_username"}
        prepared = {}
        for key in allowed & values.keys():
            value = str(values[key]).strip()
            if key in {"source_workbook", "encrypted_config_path"} and value and not Path(value).is_absolute():
                raise ValueError("Las rutas de configuración deben ser absolutas")
            if key == "repository_url" and value:
                import re
                if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value):
                    raise ValueError("Usá una URL HTTPS de GitHub sin credenciales")
            if key == "pages_url" and value and not value.startswith("https://"):
                raise ValueError("Pages debe usar HTTPS")
            prepared[key] = value
        for key, value in prepared.items():
            setattr(self.config, key, value)
        self.config.save()
        return {"saved": True}

    def backup(self) -> dict:
        directory = Path(self.config.backup_dir).resolve() if self.config.backup_dir else self.config.root / "backups"
        if directory == REPO_ROOT or REPO_ROOT in directory.parents:
            raise ValueError("El respaldo debe estar fuera del repositorio público")
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        week = now.strftime("%G-W%V")
        paths = []
        for name in (f"weekly-{week}.sqlite3", f"monthly-{now:%Y-%m}.sqlite3"):
            destination = directory / name
            temporary = directory / (name + "." + uuid.uuid4().hex + ".tmp")
            with self.connection() as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("No se pudo verificar el respaldo")
            replace_file(temporary, destination)
            paths.append(str(destination))
        for prefix in ("weekly-", "monthly-"):
            for file in sorted(directory.glob(prefix + "*.sqlite3"), reverse=True)[12:]:
                if file.resolve().parent == directory:
                    file.unlink()
        if self.config.file.exists():
            atomic_json(directory / "config-backup.json", json.loads(self.config.file.read_text(encoding="utf-8")))
        return {"files": paths, "integrity": "ok"}

    def install_schedule(self) -> dict:
        if sys.platform != "win32":
            raise ValueError("La tarea programada requiere Windows")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(REPO_ROOT / "scripts" / "register-task.ps1"),
                                 "-PythonPath", sys.executable, "-DataDir", str(self.config.root),
                                 "-ProjectPath", str(REPO_ROOT)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=40)
        if result.returncode:
            raise RuntimeError("Windows no permitió registrar la tarea. Ejecutá scripts/register-task.ps1 bajo tu usuario")
        self.config.schedule = "FCIFamiliar-Semanal: domingos 18:00, recuperación al iniciar sesión"
        self.config.save()
        return {"task_name": "FCIFamiliar-Semanal", "installed": True}
