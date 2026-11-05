"""Test the final ZIP from a new path without developer PATH/PYTHONPATH entries."""
from pathlib import Path, PurePosixPath
import json
import hashlib
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
            try:
                manifest = json.loads(z.read('❤/release-files.json'))
                entries = manifest['files']
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError('压缩包缺少有效的程序文件清单。') from exc
            if (manifest.get('schema') != 1 or manifest.get('app') != 'xiaoyu-voucher'
                    or manifest.get('data_schema') != 1 or not isinstance(entries, dict)):
                raise ValueError('程序文件清单不适用于本软件。')
            actual = {str(PurePosixPath(name).relative_to('❤')) for name in z.namelist()
                      if not name.endswith('/') and name != '❤/release-files.json'}
            if set(entries) != actual:
                raise ValueError('压缩包与程序文件清单不一致。')
            for name, expected in entries.items():
                if 'data' in (part.casefold() for part in PurePosixPath(name).parts):
                    raise ValueError('发布包不得包含用户 data 目录。')
                with z.open('❤/' + name) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                        raise ValueError('程序文件校验失败：' + name)
            z.extractall(target)
        # The independent helper has its own embedded trust anchor. Verify that
        # a stale or externally supplied helper cannot silently use another key.
        from PyInstaller.archive.readers import CArchiveReader
        helper = target / '❤' / '_internal' / 'update-helper.exe'
        if not helper.is_file():
            raise ValueError('发行包缺少独立更新程序。')
        reader = CArchiveReader(str(helper))
        channel_names = [name for name in reader.toc
                         if name.replace('\\', '/') == 'assets/update-channel.json']
        channel_path = target / '❤' / '_internal' / 'assets' / 'update-channel.json'
        if channel_path.exists():
            if (len(channel_names) != 1
                    or json.loads(reader.extract(channel_names[0])) != json.loads(channel_path.read_bytes())):
                raise ValueError('主程序与独立更新程序的更新地址或公钥不一致。')
        elif channel_names:
            raise ValueError('主程序与独立更新程序的更新启用状态不一致。')
        env = {k: v for k, v in os.environ.items()
               if k.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE')}
        env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
        process = subprocess.run([str(target / '❤' / '❤.exe'),
                                  '--self-test', str(report)], cwd=target, env=env,
                                 timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        assert process.returncode == 0, f'Portable self-test exited with {process.returncode}'
        result = json.loads(report.read_text(encoding='utf-8'))
        assert result['ok'], result
        if result.get('version') != manifest.get('version'):
            raise ValueError('新包程序版本与文件清单不一致。')
    return result


if __name__ == '__main__':
    verify_archive(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'release' / '❤.zip')
    print('Portable ZIP passes from a new path with system-only PATH.')
