from pathlib import Path


VERSION_PATH = Path(__file__).resolve().parents[1] / "VERSION"
__version__ = VERSION_PATH.read_text(encoding="utf-8").strip()

if not __version__:
    raise RuntimeError(f"version file is empty: {VERSION_PATH}")
