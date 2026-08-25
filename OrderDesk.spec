from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = (
    collect_submodules("uvicorn")
    + ["passlib.handlers.bcrypt"]
    # System-tray icon: pystray's Windows backend and Pillow are imported
    # lazily inside windows_launcher, so PyInstaller can't see them by static
    # analysis — name them explicitly. If tray build ever fails at runtime the
    # app falls back to running without it, but we want it to actually ship.
    + ["pystray._win32", "PIL.Image", "PIL.ImageDraw"]
)
datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
    ("migrations", "migrations"),
    ("alembic.ini", "."),
    ("THIRD_PARTY_NOTICES.md", "."),
    # Shipped so the "Про застосунок" changelog renders offline in the
    # installed app — resource_path("CHANGELOG.md") resolves here in the bundle.
    ("CHANGELOG.md", "."),
    # App/tray icon (resource_path("assets/orderdesk.ico")).
    ("assets/orderdesk.ico", "assets"),
]
datas += collect_data_files("tzdata")

a = Analysis(
    ["app/windows_launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OrderDesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/orderdesk.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="OrderDesk",
)
