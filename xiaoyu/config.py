import json
import os
import sys
from pathlib import Path

def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    return base / relative

def data_dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
    directory = base / 'data'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / '.write-test'
        probe.write_text('ok', encoding='utf-8'); probe.unlink()
    except OSError:
        directory = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'XiaoYuVoucher'
        directory.mkdir(parents=True, exist_ok=True)
    return directory

def load_config() -> dict:
    try:
        value = json.loads((data_dir() / 'settings.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}

def save_config(value: dict):
    target = data_dir() / 'settings.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(target)
