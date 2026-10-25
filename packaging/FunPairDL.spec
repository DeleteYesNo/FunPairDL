# PyInstaller spec for the Windows build: one folder, no console window.
# Run through packaging/build.ps1, which also zips the result.
import os
import re

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
VERSION = re.search(r'__version__ = "([^"]+)"',
                    open(os.path.join(ROOT, 'funpairdl', '__init__.py'), encoding='utf-8').read()).group(1)

a = Analysis(
    [os.path.join(ROOT, 'FunPairDL.pyw')],
    pathex=[ROOT],
    # Read at run time next to the code: the injected browser scripts,
    # the app icon and Qt's web channel shim.
    datas=[
        (os.path.join(ROOT, 'extension'), 'extension'),
        (os.path.join(ROOT, 'assets'), 'assets'),
        (os.path.join(ROOT, 'funpairdl', 'gui', 'qwebchannel.js'), os.path.join('funpairdl', 'gui')),
    ],
    # Providers and uvicorn's protocol/loop modules are imported by name.
    hiddenimports=collect_submodules('funpairdl') + collect_submodules('uvicorn'),
    excludes=['tkinter'],
)

# PySide6's hooks bring every QML module and the Qt libraries those need; a
# widgets + WebEngine app loads none of them. KEEP_QT is what the running app
# and its QtWebEngineProcess have loaded (2026-10-26), plus Svg / Pdf /
# OpenGLWidgets, which Qt may load on demand. Also dropped: Qt's own UI
# translations (the app is English; Chromium's locales stay) and the debug
# builds of the WebEngine resource packs.
KEEP_QT = {
    'Qt6Core', 'Qt6Gui', 'Qt6Network', 'Qt6OpenGL', 'Qt6OpenGLWidgets', 'Qt6Positioning',
    'Qt6PrintSupport', 'Qt6Qml', 'Qt6QmlMeta', 'Qt6QmlModels', 'Qt6QmlWorkerScript', 'Qt6Quick',
    'Qt6QuickWidgets', 'Qt6WebChannel', 'Qt6WebEngineCore', 'Qt6WebEngineWidgets', 'Qt6Widgets',
    'Qt6Svg', 'Qt6Pdf',
}


def _wanted(dest):
    d = dest.replace('\\', '/')
    if d.startswith('PySide6/qml/'):
        return False
    if d.startswith('PySide6/translations/') and not d.startswith('PySide6/translations/qtwebengine_locales/'):
        return False
    if d.startswith('PySide6/resources/') and d.endswith('.debug.pak'):
        return False
    name = d.rsplit('/', 1)[-1]
    if d.count('/') == 1 and name.startswith('Qt6') and name.endswith('.dll'):
        return name[:-4] in KEEP_QT
    return True


a.binaries = [e for e in a.binaries if _wanted(e[0])]
a.datas = [e for e in a.datas if _wanted(e[0])]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FunPairDL',
    icon=os.path.join(ROOT, 'assets', 'funpairdl.ico'),
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name='FunPairDL')
