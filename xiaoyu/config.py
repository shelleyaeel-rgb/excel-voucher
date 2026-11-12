import json
import os
import sys
import tempfile
from pathlib import Path

# Set only by the explicit offline probe entry points, never by normal startup.
_TEST_DATA_DIR: Path | None = None

def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    return base / relative

def data_dir() -> Path:
    if _TEST_DATA_DIR is not None:
        _TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        return _TEST_DATA_DIR
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
    content = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         prefix='.settings-', suffix='.tmp',
                                         dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Preserve the original save error if Windows also blocks cleanup.
                pass
