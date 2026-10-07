"""Paths are anchored to the installation, never the process working directory."""
import os
from pathlib import Path
import sys

PLUGIN_ROOT = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
INSTALL_ROOT = PLUGIN_ROOT.parents[3]
DATA_ROOT = INSTALL_ROOT / 'PageOutline'

def encode_path(path,base=INSTALL_ROOT):
    path=str(Path(path).resolve())
    try:
        return Path(os.path.relpath(path,base)).as_posix()
    except ValueError:
        # Windows cannot represent another drive relative to this installation.
        return path

def decode_path(path,base=INSTALL_ROOT):
    value=Path(path)
    return str((value if value.is_absolute() else Path(base)/value).resolve())
