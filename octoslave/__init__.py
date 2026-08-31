"""OctoSlave — autonomous AI research & coding assistant.

The version is single-sourced from ``[project] version`` in pyproject.toml. Do
not hardcode a number here — bump it in pyproject.toml only. Three ways to get
back to it, in order of how trustworthy they are for the copy that is running:

1. A frozen bundle carries ``_build_version.txt``, stamped by the PyInstaller
   spec at build time. PyInstaller ships no dist-info, so without this file the
   installers would report ``0.0.0+unknown`` — and the self-updater would think
   every release is newer than what is installed.
2. An installed package has real dist metadata.
3. A plain source checkout has pyproject.toml sitting right there.
"""
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path


def _resolve_version() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent
        try:
            stamp = Path(base) / "octoslave" / "_build_version.txt"
            text = stamp.read_text().strip()
            if text:
                return text
        except OSError:
            pass
    try:
        return _pkg_version("octoslave")
    except PackageNotFoundError:
        pass
    try:
        import tomllib

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with open(pyproject, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return "0.0.0+unknown"


__version__ = _resolve_version()

__all__ = ["__version__"]
