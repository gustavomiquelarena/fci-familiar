"""Configuración privada por usuario, separada del checkout público."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import shutil
from pathlib import Path
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[2]


def replace_file(source: Path, destination: Path, *, attempts: int = 12, delay: float = 0.25) -> None:
    """Cloud sync/indexers can briefly hold a Windows destination after a write."""
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(Path(name), path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def default_data_dir() -> Path:
    override = os.environ.get("FCI_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    pointer = REPO_ROOT.parent / ".fci-familiar-local.json"
    if pointer.is_file():
        values = json.loads(pointer.read_text(encoding="utf-8"))
        if values.get("data_dir") and Path(values["data_dir"]).is_absolute():
            return Path(values["data_dir"]).resolve()
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
    # Packaged launchers can virtualize LOCALAPPDATA to their own LocalCache.
    # The app opened from Explorer/Task Scheduler must reach the same user data.
    if os.name == "nt" and "packages" in {p.lower() for p in local.parts} and local.parts[-2:] == ("LocalCache", "Local"):
        local = Path(os.environ.get("USERPROFILE", Path.home())) / "AppData" / "Local"
    return local / "FCIFamiliar"


@dataclass
class Config:
    data_dir: str
    source_workbook: str = ""
    encrypted_config_path: str = ""
    iol_username: str = ""
    credential_target: str = "secret_key"
    repository_url: str = ""
    pages_url: str = ""
    git_executable: str = ""
    backup_dir: str = ""
    port: int = 8765
    schedule: str = "Domingos 18:00 (Argentina)"

    @property
    def root(self) -> Path:
        return Path(self.data_dir).resolve()

    @property
    def database(self) -> Path:
        return self.root / "fondo.sqlite3"

    @property
    def file(self) -> Path:
        return self.root / "config.json"

    def save(self) -> None:
        if not self.git_executable:
            self.git_executable = shutil.which("git") or ""
        atomic_json(self.file, asdict(self))

    def remember_location(self) -> None:
        # Private pointer outside Git: Explorer, a packaged terminal and Task
        # Scheduler must all open the same physical data directory.
        atomic_json(REPO_ROOT.parent / ".fci-familiar-local.json", {"data_dir": str(self.root)})

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> "Config":
        root = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
        file = root / "config.json"
        fields = cls.__dataclass_fields__
        if file.exists():
            values = json.loads(file.read_text(encoding="utf-8"))
            config = cls(**{k: v for k, v in values.items() if k in fields})
            config.data_dir = str(root)
        else:
            config = cls(data_dir=str(root))
        return config
