# -*- mode: python ; coding: utf-8 -*-
#
# IMPORTANT: Always build with the venv python, NOT the system python:
#   .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean DJI_RC2_KMZsync.spec
#
import glob
import os
from PyInstaller.utils.hooks import collect_all

# Collect PIL via hook (catches hiddenimports and data files)
pil_datas, pil_binaries, pil_hiddenimports = collect_all('PIL')

# Explicitly add every PIL .pyd binary so they land in PIL\ inside _internal.
# This is a safety net in case collect_all misses them (e.g. when venv is on a
# network drive and collect_all falls back to the system Python's PIL).
_pil_src = os.path.join(SPECPATH, '.venv', 'Lib', 'site-packages', 'PIL')

_extra_binaries = []
for _pyd in glob.glob(os.path.join(_pil_src, '*.pyd')):
    _entry = (_pyd, 'PIL')
    if _entry not in pil_binaries:
        _extra_binaries.append(_entry)

_extra_datas = []
for _py in glob.glob(os.path.join(_pil_src, '*.py')):
    _entry = (_py, 'PIL')
    if _entry not in pil_datas:
        _extra_datas.append(_entry)

_json_sources = [
    os.path.join(SPECPATH, 'kmz_sync_config.json'),
    os.path.join(SPECPATH, 'kmz_copy_map.json'),
]

a = Analysis(
    ['djirc2kmzsync.py'],
    pathex=[],
    binaries=pil_binaries + _extra_binaries,
    datas=pil_datas + _extra_datas,
    hiddenimports=pil_hiddenimports + [
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'PIL.ImageFile',
        'PIL._imaging',
        'PIL._imagingtk',
        'PIL.PngImagePlugin',
        'PIL.JpegImagePlugin',
        'PIL.BmpImagePlugin',
        'PIL.GifImagePlugin',
        'PIL.WebPImagePlugin',
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
    name='DJI_RC2_KMZsync',
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
    name='DJI_RC2_KMZsync',
)

# Post-build: copy JSON config files next to the exe (not into _internal/).
# datas land in _internal/ but get_runtime_base_dir() returns the exe's parent folder.
import shutil
_dist_dir = os.path.join(DISTPATH, 'DJI_RC2_KMZsync')
for _json in _json_sources:
    if os.path.isfile(_json):
        shutil.copy2(_json, os.path.join(_dist_dir, os.path.basename(_json)))
