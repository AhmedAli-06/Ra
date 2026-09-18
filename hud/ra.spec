# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

datas = [('app.html', '.'), ('orb.html', '.')]
binaries = []
hiddenimports = ['openai', 'groq', 'edge_tts', 'vosk', 'playsound3', 'faster_whisper',
                 'ctranslate2', 'pyttsx3', 'pypdf', 'requests', 'psutil', 'pytesseract',
                 'PIL', 'ddgs', 'fastapi', 'uvicorn', 'websockets', 'anyio', 'httpx',
                 'pydantic', 'watchfiles', 'webview', 'pythonnet',
                 'clr_loader', 'sherpa_onnx']
hiddenimports += collect_submodules('ddgs')
hiddenimports += collect_submodules('uvicorn')
hiddenimports += collect_submodules('fastapi')
hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('pythonnet')
datas += collect_data_files('faster_whisper')
binaries += collect_dynamic_libs('vosk')
hiddenimports += collect_submodules('faster_whisper')
binaries += collect_dynamic_libs('sherpa_onnx')
# sherpa-onnx ships sherpa-onnx-core DLLs + onnxruntime; make sure PyInstaller
# also picks up the sherpa_onnx data (config files) if the package carries any.
from PyInstaller.utils.hooks import collect_all
_sherpa_data, _sherpa_bins, _sherpa_hidden = collect_all('sherpa_onnx')
datas += _sherpa_data
binaries += _sherpa_bins
hiddenimports += _sherpa_hidden
hiddenimports += ['ra.plugins', 'ra.scheduler', 'ra.tasklist', 'ra.transcribe',
                  'ra.redact', 'ra.rag.indexer', 'ra.rag.sources', 'ra.memory',
                  'ra.fastlane', 'ra.monitor', 'ra.selftest']

a = Analysis(
    ['run.py'],
    pathex=['.', r'..\src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Ra',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)