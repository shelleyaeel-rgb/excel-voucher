"""Developer diagnostic for unresolved named imports within the bundled DLL set."""
from pathlib import Path
import pefile

base = Path(__file__).resolve().parents[1] / 'dist' / '❤' / '_internal'
paths = [p for p in base.rglob('*') if p.suffix.lower() in ('.dll', '.pyd')]
libraries = {p.name.lower(): p for p in paths}
exports = {}
for name, path in libraries.items():
    pe = pefile.PE(str(path), fast_load=True, max_symbol_exports=100000)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
    exports[name] = {s.name for s in getattr(getattr(pe, 'DIRECTORY_ENTRY_EXPORT', None), 'symbols', [])}
    pe.close()
for path in paths:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']])
    for dependency in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', []):
        name = dependency.dll.decode().lower()
        if name in libraries:
            missing = [i.name.decode() for i in dependency.imports if i.name and i.name not in exports[name]]
            if missing:
                print(path.name, '->', name, missing[:20])
    pe.close()
