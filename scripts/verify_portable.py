"""Test the final ZIP from a new path without developer PATH/PYTHONPATH entries."""
from pathlib import Path, PurePosixPath
import json
import os
import subprocess
import tempfile
import zipfile
import sys

ROOT = Path(__file__).resolve().parents[1]


def validate_members(bundle: zipfile.ZipFile) -> None:
    """Validate names for Windows extraction, including ADS and link hazards."""
    seen = set()
    for item in bundle.infolist():
        name = item.orig_filename
        path = PurePosixPath(name)
        if ("\\" in name or ":" in name or path.is_absolute() or ".." in path.parts
                or not path.parts or path.parts[0] != "❤"
                or any(part.endswith((" ", ".")) for part in path.parts)
                or (item.external_attr >> 16) & 0o170000 == 0o120000):
            raise ValueError("压缩包含有不安全的文件路径。")
        normalized = str(path).casefold()
        if normalized in seen:
            raise ValueError("压缩包含有重复文件路径。")
        seen.add(normalized)
    if "❤/❤.exe" not in seen:
        raise ValueError("压缩包缺少 ❤/❤.exe。")


def verify_archive(archive: Path):
    qa = ROOT / 'outputs' / 'qa'
    qa.mkdir(parents=True, exist_ok=True)
    report = qa / 'extracted-package-smoke.json'
    with tempfile.TemporaryDirectory(prefix='portable path ', dir=qa) as directory:
        target = Path(directory).resolve()
        assert target.is_relative_to(qa.resolve())
        with zipfile.ZipFile(archive) as z:
            if z.testzip() is not None:
                raise ValueError("压缩包完整性检查未通过。")
            validate_members(z)
            z.extractall(target)
        env = {k: v for k, v in os.environ.items()
               if k.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE')}
        env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
        process = subprocess.run([str(target / '❤' / '❤.exe'),
                                  '--self-test', str(report)], cwd=target, env=env,
                                 timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        assert process.returncode == 0, f'Portable self-test exited with {process.returncode}'
        result = json.loads(report.read_text(encoding='utf-8'))
        assert result['ok'], result
    return result


if __name__ == '__main__':
    verify_archive(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'release' / '❤.zip')
    print('Portable ZIP passes from a new path with system-only PATH.')
