"""OctoSlave — autonomous AI research & coding assistant.

The version is single-sourced from the package metadata, which is generated
from ``[project] version`` in pyproject.toml. Do not hardcode a number here —
bump it in pyproject.toml only.
"""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("octoslave")
except PackageNotFoundError:
    # Running from a source checkout that was never installed (e.g. `python -m`
    # without `pip install`). Fall back to reading pyproject.toml directly.
    try:
        import tomllib
        from pathlib import Path

        _pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with open(_pyproject, "rb") as _f:
            __version__ = tomllib.load(_f)["project"]["version"]
    except Exception:
        __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
