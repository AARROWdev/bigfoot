"""Test setup. Tests that recycle, install or create huge trees only run inside Windows Sandbox
(tests/sandbox/run-tests.ps1). On a normal PC they are skipped, and Bigfoot's settings go to a temp .ini
file instead of the registry."""
import gc
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zlib

import pytest

try:
    import win32ui  # noqa: F401  (pywinauto imports it; it needs the MFC runtime, which Windows Sandbox lacks)
except ImportError:
    import types
    sys.modules["win32ui"] = types.ModuleType("win32ui")  # pywinauto only uses it for screenshots

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

# Tests never show windows, take the mouse or the keyboard on a PC somebody is using: off the Sandbox the UI tests
# run headless. This has to be set before Qt starts. (QT_QPA_PLATFORM=windows in the environment overrides it,
# for looking at a test on purpose.) In the Sandbox nobody's screen is at risk and the real platform is used.
if os.environ.get("USERNAME", "").upper() != "WDAGUTILITYACCOUNT":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import bigfoot as bf  # noqa: E402
import helpers as h  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "sandbox: changes the PC (Recycle Bin, drives, ACLs, shares): Sandbox only")
    config.addinivalue_line("markers", "big: creates hundreds of thousands of files: Sandbox only")


def pytest_collection_modifyitems(config, items):
    if h.IN_SANDBOX:
        return
    skip = pytest.mark.skip(reason="runs only inside Windows Sandbox (tests/sandbox/run-tests.ps1)")
    for it in items:
        if "sandbox" in it.keywords or "big" in it.keywords:
            it.add_marker(skip)


def _reg_export():
    out = os.path.join(tempfile.gettempdir(), f"bf-reg-{os.getpid()}.reg")
    subprocess.run(["reg", "export", r"HKCU\Software\Bigfoot", out, "/y"], capture_output=True)
    try:
        with open(out, "rb") as f:
            return f.read()
    except OSError:
        return b""
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


@pytest.fixture(scope="session", autouse=True)
def qapp():
    before = None if h.IN_SANDBOX else _reg_export()
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(bf.APP)
    bf.apply_theme(app)
    gc.disable()  # same as main(): cycles are collected on the GUI thread only
    yield app
    gc.enable()
    if before is not None:
        assert _reg_export() == before, "a test changed the real HKCU\\Software\\Bigfoot"


@pytest.fixture(autouse=True)
def ini_settings(tmp_path, monkeypatch):
    ini = str(tmp_path / "bigfoot.ini")
    monkeypatch.setattr(bf, "QSettings", lambda *a, **k: QSettings(ini, QSettings.IniFormat))
    return ini


@pytest.fixture
def work(request, tmp_path):
    """A folder for test trees on a local NTFS volume (the Sandbox's own C:, so the Recycle Bin is real)."""
    if h.IN_SANDBOX:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in request.node.name)[:60]
        d = os.path.join("C:\\bftest", f"{safe}_{zlib.crc32(request.node.name.encode()) % 10000}")
        if os.path.exists(d):
            subprocess.run(["cmd", "/c", "rd", "/s", "/q", h.lp(d)], capture_output=True)
        os.makedirs(d)
    else:
        d = str(tmp_path / "w")
        os.makedirs(d)
    yield d
    # (no attrib /s, icacls /t or os.walk here: they follow the test junctions round in a loop)
    for f in h.walk_files_no_links(h.lp(d)):
        try:
            os.chmod(f, stat.S_IWRITE)  # read-only files would block rd
        except OSError:
            pass
    subprocess.run(["cmd", "/c", "rd", "/s", "/q", h.lp(d)], capture_output=True)  # \\?\: also odd names


@pytest.fixture
def make_window(qapp):
    made = []

    def make(start=None, size=(1300, 800)):
        w = bf.Window(start)
        if size:
            w.resize(*size)
        w.show()
        h.pump(50)
        made.append(w)
        return w

    yield make
    for w in made:
        if w.scanner and not w.scanner.t1:
            w.scanner.stop()
            h.wait_until(lambda: w.scanner.t1, 60, "scanner stop")
        w.close()
        w.deleteLater()
    h.pump(50)
    gc.collect()


@pytest.fixture
def window(make_window):
    return make_window()


@pytest.fixture
def dialogs():
    d = h.Dialogs("yes")
    yield d
    d.stop()


@pytest.fixture
def no_launch(monkeypatch):
    """Record Open / Show in Explorer instead of starting programs."""
    calls = []

    class FakeSubprocess:  # only Bigfoot's module sees this; the tests keep the real subprocess
        @staticmethod
        def Popen(cmd, *a, **k):
            calls.append(("explorer", cmd))
    monkeypatch.setattr(bf.os, "startfile", lambda p, *a: calls.append(("open", p)))
    monkeypatch.setattr(bf, "subprocess", FakeSubprocess)
    return calls


def rmtree(p):
    shutil.rmtree(h.lp(p), ignore_errors=True)
