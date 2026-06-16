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
    # The pcgr package is reached via the import graph, but list its modules
    # explicitly so a refactor can't silently drop one from the build.
    hiddenimports=[
        'pcgr', 'pcgr.api', 'pcgr.config', 'pcgr.platforms', 'pcgr.titles',
        'pcgr.dedup',
        # launchers (behind the common Launcher interface)
        'pcgr.launchers', 'pcgr.launchers.base', 'pcgr.launchers.steam',
        'pcgr.launchers.gog', 'pcgr.launchers.epic', 'pcgr.launchers.retroarch',
        # cross-cutting services
        'pcgr.services', 'pcgr.services.names', 'pcgr.services.art',
        'pcgr.services.filters',
        # stateless low-level sources
        'pcgr.sources', 'pcgr.sources.steam_files', 'pcgr.sources.galaxy',
        'pcgr.sources.store', 'pcgr.sources.images', 'pcgr.sources.retroarch',
        'pcgr.sources.epic_auth', 'pcgr.sources.epic_api', 'pcgr.sources.steam_api',
        'pcgr.sources.steam_names',
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
