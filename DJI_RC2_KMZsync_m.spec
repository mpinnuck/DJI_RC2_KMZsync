# -*- mode: python ; coding: utf-8 -*-

import os
import shutil
from PyInstaller.utils.hooks import collect_all

# Collect PIL hidden imports/data for bundled image preview support.
pil_datas, pil_binaries, pil_hiddenimports = collect_all("PIL")

_json_sources = [
    os.path.join(SPECPATH, "kmz_sync_config_m.json"),
    os.path.join(SPECPATH, "kmz_copy_map_m.json"),
]

a = Analysis(
    ["djirc2kmzsync.py"],
    pathex=[],
    binaries=pil_binaries,
    datas=pil_datas,
    hiddenimports=pil_hiddenimports + [
        "PIL",
        "PIL.Image",
        "PIL.ImageTk",
        "PIL.ImageFile",
        "PIL.PngImagePlugin",
        "PIL.JpegImagePlugin",
        "PIL.BmpImagePlugin",
        "PIL.GifImagePlugin",
        "PIL.WebPImagePlugin",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DJI_RC2_KMZsync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DJI_RC2_KMZsync",
)

app = BUNDLE(
    coll,
    name="DJI_RC2_KMZsync.app",
    icon=None,
    bundle_identifier=None,
)

# Copy mac runtime JSON files next to both the onedir executable and
# the .app executable location so get_runtime_base_dir() can find them.
_targets = [
    os.path.join(DISTPATH, "DJI_RC2_KMZsync"),
    os.path.join(DISTPATH, "DJI_RC2_KMZsync.app", "Contents", "MacOS"),
]
for _target in _targets:
    os.makedirs(_target, exist_ok=True)
    for _json in _json_sources:
        if os.path.isfile(_json):
            shutil.copy2(_json, os.path.join(_target, os.path.basename(_json)))
