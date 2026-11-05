# Build using: .venv\Scripts\python.exe -m PyInstaller --noconfirm xiaoyu.spec
from pathlib import Path
import os
import ssl
import sys
root = Path(SPECPATH)
python_dlls = Path(sys.base_prefix) / 'DLLs'
openssl_names = ('libssl-3-x64.dll', 'libcrypto-3-x64.dll')
openssl_binaries = [(str(python_dlls / name), '.') for name in openssl_names]
for source, _ in openssl_binaries:
    if not Path(source).is_file():
        raise RuntimeError(f'Python 配套的 OpenSSL 组件不存在：{source}')
# Some managed Python runtimes load a companion distrust list from ssl.py.
# Keep it beside the frozen ssl module; ordinary Python has no such file.
ssl_companion = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
ssl_datas = [(str(ssl_companion), '.')] if ssl_companion.is_file() else []
channel = root / 'assets' / 'update-channel.json'
channel_datas = [(str(channel), 'assets')] if channel.is_file() else []
helper = Path(os.environ.get('XIAOYU_HELPER_PATH', root / 'build' / 'helper' / 'update-helper.exe'))
if not helper.is_file():
    raise RuntimeError('请使用 scripts/build.ps1，先构建独立更新程序。')
a = Analysis(
    [str(root / 'main.py')], pathex=[str(root)],
    binaries=openssl_binaries,
    datas=[(str(root / 'assets' / name), 'assets') for name in
           ['logo.png', 'app.ico', '凭证导入模板.xls']] + ssl_datas + channel_datas + [(str(helper), '.')],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.QtQuick', 'PySide6.QtQml', 'tkinter', 'pytest'],
    noarchive=False,
)
# Qt 6 uses the Windows ICU API. A developer PATH may contain Poppler's
# incompatible ICU with the same filename; do not shadow the OS copy.
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in ('icuuc.dll', 'icuin.dll')
              and not Path(entry[0]).name.lower().startswith('icudt')]
# A developer PATH can also contain Poppler's incompatible OpenSSL build.
# Always pair Python's _ssl.pyd with the DLLs shipped in that Python runtime.
a.binaries = [(destination, str(python_dlls / Path(destination).name), kind)
              if Path(destination).name.lower() in openssl_names
              else (destination, source, kind)
              for destination, source, kind in a.binaries]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name='❤', debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, console=False,
          icon=str(root / 'assets' / 'app.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='❤')
