# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Apex tracker. Builds TWO exes into one shared folder:
#   ApexTracker.exe    - console CLI (watch / batch / calibrate / setup / monitors)
#   ApexTrackerUI.exe  - windowed Tkinter app (apex_gui.py) - what friends launch
# Both share the same _internal (the heavy OCR models / DLLs), so the folder isn't
# doubled. One-folder build (fast startup; a one-file build would re-unpack the big
# ONNX models to temp on every launch). Build with:  pyinstaller apextracker.spec
#
# config.json and .env are NOT bundled - build_release.bat copies editable copies
# next to the exe (the scripts resolve them from the exe folder when frozen).

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# Packages with data files (OCR models / configs) and/or native extensions that
# PyInstaller won't pull in fully on its own.
for pkg in (
    "rapidocr_onnxruntime",  # OCR engine + bundled .onnx models + config yaml
    "onnxruntime",           # native inference DLLs
    "windows_capture",       # compiled WGC capture extension
    "sv_ttk",                # Sun Valley GUI theme - .tcl files, else UI exe crashes
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

# pygrabber + comtypes power OBS-mode device auto-detection (find the OBS Virtual
# Camera by name). comtypes generates wrapper modules at runtime, so collect it
# fully or the frozen build can't enumerate video devices.
for pkg in ("pygrabber", "comtypes"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# The OCR path is pure onnxruntime - none of the heavy ML / data stacks that happen
# to be installed in this env are needed. Excluding them keeps the build a few
# hundred MB instead of ~5 GB.
# NOTE: PIL/Pillow is REQUIRED (rapidocr's load_image.py imports it) and tkinter is
# REQUIRED (the GUI) - do not exclude either.
EXCLUDES = [
    "matplotlib",
    "torch", "torchaudio", "torchvision",
    "transformers", "tokenizers", "huggingface_hub", "safetensors",
    "accelerate", "datasets", "bitsandbytes",
    "scipy", "sklearn", "scikit-learn", "pandas",
    "sympy", "networkx", "numba", "llvmlite",
    "tensorflow", "onnx", "av", "IPython", "jupyter",
    "opentelemetry",
]


def analysis(entry):
    return Analysis(
        [entry],
        pathex=[],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDES,
        noarchive=False,
    )


# apex_gui imports apex_tracker, so its analysis is a superset of the CLI's deps
# (everything apex_tracker needs, plus tkinter). The shared _internal is built from
# it, so a single copy of every DLL/model serves both exes.
a_cli = analysis("apex_tracker.py")
a_gui = analysis("apex_gui.py")

pyz_cli = PYZ(a_cli.pure)
pyz_gui = PYZ(a_gui.pure)

exe_cli = EXE(
    pyz_cli, a_cli.scripts, [],
    exclude_binaries=True,
    name="ApexTracker",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)

exe_gui = EXE(
    pyz_gui, a_gui.scripts, [],
    exclude_binaries=True,
    name="ApexTrackerUI",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)

coll = COLLECT(
    exe_cli,
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ApexTracker",
)
