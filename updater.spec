# Standalone updater: copy outside the running portable directory before use.
from pathlib import Path
import ssl
import sys

root = Path(SPECPATH)
channel = root / 'assets' / 'update-channel.json'
datas = [(str(channel), 'assets')] if channel.is_file() else []
ssl_companion = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
if ssl_companion.is_file():
    datas.append((str(ssl_companion), '.'))
python_dlls = Path(sys.base_prefix) / 'DLLs'
openssl_names = ('libssl-3-x64.dll', 'libcrypto-3-x64.dll')
binaries = [(str(python_dlls / name), '.') for name in openssl_names]
a = Analysis([str(root / 'update_helper.py')], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=[], hookspath=[],
             hooksconfig={}, runtime_hooks=[],
             excludes=['PySide6', 'openpyxl', 'xlrd', 'xlwt', 'PIL', 'boto3', 'botocore', 'pytest', 'tkinter'],
             noarchive=False)
a.binaries = [(destination, str(python_dlls / Path(destination).name), kind)
              if Path(destination).name.lower() in openssl_names
              else (destination, source, kind)
              for destination, source, kind in a.binaries]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='update-helper',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=str(root / 'assets' / 'app.ico'))
