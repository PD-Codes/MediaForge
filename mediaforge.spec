# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = [
    ("src/mediaforge/web/templates", "mediaforge/web/templates"),
    ("src/mediaforge/web/static",    "mediaforge/web/static"),
]

# Pull in non-Python assets from third-party packages
datas += collect_data_files("fake_useragent")
datas += collect_data_files("certifi")          # SSL certificates

a = Analysis(
    ["_pyinstaller_entry.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # WSGI server
        "waitress",
        "waitress.task",
        "waitress.server",
        "waitress.utilities",
        # Flask extensions
        "flask_wtf",
        "flask_wtf.csrf",
        "flask_limiter",
        "flask_limiter.util",
        # Auth / OIDC
        "authlib",
        "authlib.integrations.flask_client",
        "authlib.jose",
        "joserfc",
        # Crypto
        "cryptography.hazmat.primitives.asymmetric.ec",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        # DNS / networking
        "dns",
        "dns.resolver",
        # File-watching
        "watchdog",
        "watchdog.observers",
        "watchdog.observers.polling",
        "watchdog.events",
        # Push notifications
        "pywebpush",
        # Misc
        "packaging.version",
        "importlib.metadata",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # patchright/playwright ship browser binaries — not suitable for --onefile;
    # they are downloaded at runtime via autodeps anyway.
    excludes=["patchright", "playwright", "tkinter", "matplotlib", "numpy"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mediaforge",
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,   # macOS: let the launcher handle argv
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
