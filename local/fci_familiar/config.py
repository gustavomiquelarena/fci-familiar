"""Configuración privada por usuario, separada del checkout público."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[2]


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def default_data_dir() -> Path:
    override = os.environ.get("FCI_DATA_DIR")
    return Path(override).expanduser().resolve() if override else Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "FCIFamiliar"


@dataclass
class Config:
    data_dir: str
    source_workbook: str = ""
    encrypted_config_path: str = ""
    iol_username: str = ""
    credential_target: str = "secret_key"
    repository_url: str = ""
    pages_url: str = ""
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
        atomic_json(self.file, asdict(self))

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
