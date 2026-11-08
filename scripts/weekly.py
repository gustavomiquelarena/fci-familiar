"""Launcher absoluto para Task Scheduler; deja un log privado sin secretos."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local'))
from fci_familiar.cli import main
from fci_familiar.config import Config
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone

if __name__ == '__main__':
    index = sys.argv.index('--data-dir') if '--data-dir' in sys.argv else -1
    config = Config.load(sys.argv[index + 1] if index >= 0 else None)
    log_dir = config.root / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / 'weekly.log').open('a', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log):
        print(datetime.now(timezone.utc).isoformat(), 'Inicio de tarea', flush=True)
        try:
            main()
        except Exception as exc:
            print('Error:', str(exc), flush=True)
            sys.exit(1)
