# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PC Game Roulette.
# Build:  python -m PyInstaller pc-game-roulette.spec --noconfirm
#
# Produces a single portable dist/PC Game Roulette.exe.  Runtime data
# (config.json, cache/, Epic tokens, WebView2 profile) is written NEXT TO the
# exe at runtime (see _data_dir() in backend.py / main.py), never bundled — so
# distributing the exe never leaks the builder's library or login.
#
# Requires the Microsoft Edge WebView2 Runtime on the target machine (present
# by default on up-to-date Windows 10/11).

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # Bundle the read-only assets the app loads at runtime.
    datas=[
        ('web', 'web'),
        ('app.ico', '.'),
    ],
    # Local modules are reached via the import graph, but list them explicitly
    # so a refactor can't silently drop one from the build.
    hiddenimports=[
        'backend', 'retroarch', 'epic_auth', 'epic_api', 'steam_names',
        'steam_api',
        # Modules split out of backend.py — reached via the import graph, but
        # listed so a refactor can't silently drop one from the build.
        'appconfig', 'steam_library', 'game_titles', 'dedup', 'game_names',
        'images', 'galaxy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PC Game Roulette',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # windowed — no console window
    disable_windowed_traceback=False,
    icon='app.ico',         # embedded in the exe -> taskbar icon
)
