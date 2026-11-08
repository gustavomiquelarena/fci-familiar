"""Entradas explícitas: nunca se descarga información al importar el paquete."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

from .config import Config, REPO_ROOT


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fondo familiar: administración y actualización local")
    parser.add_argument("--data-dir", help="Carpeta privada; por defecto LOCALAPPDATA/FCIFamiliar")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Configurar datos y migrar el Excel")
    init.add_argument("--source", required=True)
    init.add_argument("--encrypted-config", required=True)
    init.add_argument("--iol-user", required=True)
    init.add_argument("--backup-dir")
    server = commands.add_parser("serve", help="Abrir administrador en localhost")
    server.add_argument("--port", type=int)
    commands.add_parser("import", help="Repetir importación sin duplicar")
    commands.add_parser("state", help="Resumen privado del estado")
    commands.add_parser("sync", help="Descargar IOL y calcular cierre")
    commands.add_parser("publish", help="Cifrar y publicar el cierre aprobado")
    commands.add_parser("prepare", help="Preparar reportes cifrados sin push")
    commands.add_parser("backup", help="Respaldar y verificar SQLite")
    commands.add_parser("schedule", help="Registrar tarea semanal para este usuario")
    weekly = commands.add_parser("weekly", help="Descargar, conciliar, publicar y respaldar")
    weekly.add_argument("--if-due", action="store_true")
    restore = commands.add_parser("restore", help="Restaurar respaldo en una NUEVA carpeta privada")
    restore.add_argument("backup")
    args = parser.parse_args(argv)
    config = Config.load(args.data_dir)
    if args.command == "restore":
        import sqlite3
        if config.root == REPO_ROOT or REPO_ROOT in config.root.parents:
            raise ValueError("La restauración debe quedar fuera del repositorio público")
        if config.database.exists():
            raise ValueError("Para restaurar usá --data-dir con una carpeta nueva; no se sobrescribe la base activa")
        source_path = Path(args.backup).resolve()
        if not source_path.is_file():
            raise ValueError("No existe el respaldo indicado")
        saved_config = source_path.parent / "config-backup.json"
        if saved_config.is_file():
            target_root = str(config.root)
            values = json.loads(saved_config.read_text(encoding="utf-8"))
            config = Config(**{k: v for k, v in values.items() if k in Config.__dataclass_fields__})
            config.data_dir = target_root
        config.root.mkdir(parents=True, exist_ok=True)
        from contextlib import closing
        with closing(sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)) as source, closing(sqlite3.connect(config.database)) as dest:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("El respaldo no pasó la verificación")
            source.backup(dest)
        config.save()
        _print({"restored": str(config.database), "integrity": "ok"})
        return
    from .service import Service
    if args.command == "init":
        config.source_workbook = str(Path(args.source).resolve())
        config.encrypted_config_path = str(Path(args.encrypted_config).resolve())
        config.iol_username = args.iol_user
        if args.backup_dir:
            config.backup_dir = str(Path(args.backup_dir).resolve())
        config.save()
    service = Service(config)
    if args.command in {"init", "import"}:
        _print(service.import_source())
    elif args.command == "serve":
        import uvicorn
        from .app import create_app
        if args.port:
            config.port = args.port
        print(f"Administrador: http://127.0.0.1:{config.port}", flush=True)
        uvicorn.run(create_app(config), host="127.0.0.1", port=config.port, access_log=False)
    elif args.command == "state":
        _print(service.state())
    elif args.command == "sync":
        _print(service.sync())
    elif args.command == "publish":
        _print(service.publish())
    elif args.command == "prepare":
        _print(service.publish(push=False))
    elif args.command == "backup":
        _print(service.backup())
    elif args.command == "schedule":
        _print(service.install_schedule())
    elif args.command == "weekly":
        now = datetime.now().astimezone()
        # Windows task runs under the configured Argentina-local desktop clock.
        scheduled = (now - timedelta(days=(now.weekday() + 1) % 7)).replace(hour=18, minute=0, second=0, microsecond=0)
        if scheduled > now:
            scheduled -= timedelta(days=7)
        with service.connection() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key='last_weekly_published'").fetchone()
        if args.if_due and row:
            try:
                completed = datetime.fromisoformat(row[0])
                if completed.tzinfo is None:
                    completed = completed.replace(tzinfo=now.tzinfo)
                if completed >= scheduled:
                    _print({"skipped": True, "reason": "El ciclo semanal completo ya está publicado"})
                    return
            except (ValueError, TypeError):
                pass
        sync_result = service.sync()
        print("Descarga y cierre completados; publicando...", flush=True)
        publish_result = service.publish()
        if publish_result.get("state") != "published":
            raise RuntimeError("El cierre fue enviado, pero su publicación en Pages todavía está pendiente")
        with service.connection() as conn:
            conn.execute("INSERT INTO app_meta(key,value) VALUES('last_weekly_published',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (datetime.now().astimezone().isoformat(),))
            conn.commit()
        _print({"sync": sync_result, "publication": publish_result, "backup": service.backup()})


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        print("No se completó la operación: " + str(exc), file=sys.stderr)
        sys.exit(1)
