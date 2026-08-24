from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = collect_submodules("uvicorn") + ["passlib.handlers.bcrypt"]
datas = [
    ("app/templates", "app/templates"),
    ("app/static", "app/static"),
    ("migrations", "migrations"),
    ("alembic.ini", "."),
    ("THIRD_PARTY_NOTICES.md", "."),
    # Shipped so the "Про застосунок" changelog renders offline in the
    # installed app — resource_path("CHANGELOG.md") resolves here in the bundle.
    ("CHANGELOG.md", "."),
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="OrderDesk",
)
