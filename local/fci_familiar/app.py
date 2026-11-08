"""Administrador limitado a loopback, con escrituras verificadas por origen y CSRF."""
from __future__ import annotations

import secrets
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .service import Service


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.load()
    service = Service(config)
    app = FastAPI(title="Fondo familiar · Administrador", docs_url=None, redoc_url=None, openapi_url=None)
    token = secrets.token_urlsafe(32)
    static = Path(__file__).parent / "static"

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"ok": False, "error": "Acceso local requerido"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            allowed = {f"http://127.0.0.1:{config.port}", f"http://localhost:{config.port}"}
            if (origin and origin not in allowed) or not secrets.compare_digest(request.headers.get("x-csrf-token", ""), token):
                return JSONResponse({"ok": False, "error": "Recargá el administrador antes de continuar"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(Exception)
    async def error_handler(request: Request, exc: Exception):
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "La operación no pudo completarse; revisá el registro local"
        return JSONResponse({"ok": False, "error": message}, status_code=400)

    @app.get("/")
    def index():
        return FileResponse(static / "admin.html")

    @app.get("/api/state")
    def state():
        return dict(service.state(), csrf_token=token)

    @app.get("/api/health")
    def health():
        return {"app": "fci-familiar", "version": "1.0.0"}

    @app.get("/api/logs")
    def logs():
        with service.connection() as conn:
            return {"runs": [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 30")],
                    "audit": [dict(r) for r in conn.execute("SELECT at,action,entity,entity_key FROM audit_log ORDER BY id DESC LIMIT 50")]}

    @app.post("/api/import")
    def import_source(data: dict):
        return {"ok": True, "result": service.import_source(data.get("path"))}

    @app.post("/api/movements")
    def movement(data: dict):
        return {"ok": True, "result": service.add_movement(data)}

    @app.post("/api/movements/{movement_id}/reverse")
    def reverse(movement_id: str, data: dict):
        return {"ok": True, "result": service.reverse_movement(movement_id, str(data.get("reason", "")))}

    @app.post("/api/movements/{movement_id}/post")
    def post_movement(movement_id: str, data: dict):
        return {"ok": True, "result": service.post_movement(movement_id)}

    @app.post("/api/sync")
    def sync(data: dict):
        return {"ok": True, "result": service.sync()}

    @app.post("/api/publish")
    def publish(data: dict):
        return {"ok": True, "result": service.publish()}

    @app.post("/api/publications/{publication_id}/restore")
    def restore_publication(publication_id: str, data: dict):
        return {"ok": True, "result": service.restore_publication(publication_id)}

    @app.post("/api/users/{pid}/password")
    def password(pid: str, data: dict):
        return {"ok": True, "result": service.set_password(pid, data.get("password", ""), data.get("confirm", ""), publish=data.get("publish", True))}

    @app.post("/api/backup")
    def backup(data: dict):
        return {"ok": True, "result": service.backup()}

    @app.post("/api/settings")
    def settings(data: dict):
        return {"ok": True, "result": service.update_settings(data)}

    @app.post("/api/schedule")
    def schedule(data: dict):
        return {"ok": True, "result": service.install_schedule()}

    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")
    return app
