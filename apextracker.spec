# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Apex tracker. One-folder build (fast startup; the
# ONNX OCR models are large and a one-file build would re-unpack them to temp on
# every launch). Build with:  pyinstaller apextracker.spec
#
# config.json and .env are intentionally NOT bundled — build_release.bat copies
# editable copies next to the .exe so friends can set their gamertag. The script
# resolves those + the CSV from the exe's folder when frozen (see HERE in
# apex_tracker.py).

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# Packages with data files (OCR models / configs) and/or native extensions that
# PyInstaller won't pull in fully on its own.
for pkg in (
    "rapidocr_onnxruntime",  # OCR engine + bundled .onnx models + config yaml
    "onnxruntime",           # native inference DLLs
    "windows_capture",       # compiled WGC capture extension
    "supabase",              # + its sub-clients below for the stats sync
    "postgrest",
    "gotrue",
    "realtime",
    "storage3",
    "supafunc",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# httpx/websockets are pulled by supabase but occasionally missed.
hiddenimports += ["httpx", "websockets", "h2", "anyio"]

a = Analysis(
    ["apex_tracker.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The OCR path is pure onnxruntime — none of the heavy ML / data stacks that
    # happen to be installed in this env are needed. Excluding them takes the
    # build from ~5 GB to a few hundred MB.
    # NOTE: PIL/Pillow is REQUIRED — rapidocr_onnxruntime's load_image.py imports
    # it. Do not exclude it (the build runs fine until the first OCR call, then
    # crashes with ModuleNotFoundError: No module named 'PIL').
    excludes=[
        "tkinter", "matplotlib",
        "torch", "torchaudio", "torchvision",
        "transformers", "tokenizers", "huggingface_hub", "safetensors",
        "accelerate", "datasets", "bitsandbytes",
        "scipy", "sklearn", "scikit-learn", "pandas",
        "sympy", "networkx", "numba", "llvmlite",
        "tensorflow", "onnx", "av", "IPython", "jupyter",
        "opentelemetry",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ApexTracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
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
    upx=False,
    upx_exclude=[],
    name="ApexTracker",
)
