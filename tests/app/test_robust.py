"""E. Robustness (plan tests 29-31) plus 40 and 41."""
import gc
import os
import random
import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

import bigfoot as bf
import helpers as h

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


class Watchdog:
    """Measures the longest time the GUI thread didn't run its event loop."""

    def __init__(self):
        self.last = time.time()
        self.worst = 0.0
        self.t = QTimer(interval=50, timeout=self.beat)
        self.t.start()

    def beat(self):
        now = time.time()
        self.worst = max(self.worst, now - self.last)
        self.last = now


# 29
@pytest.mark.sandbox
@pytest.mark.parametrize("seed,steps", [(1, 500), (2, 500), (3, 500), (4, 3000)])
def test_monkey_500_random_clicks_and_keys(window, work, no_launch, monkeypatch, seed, steps):
    rnd = random.Random(seed)
    for i in range(25):
        h.make_tree(os.path.join(work, f"d{i}"), {f"s{j}": {f"f{k}.{rnd.choice(['mp4', 'txt', 'zip', 'bin'])}": rnd.randint(0, 9000)
                                                             for k in range(6)} for j in range(4)})
    monkeypatch.setattr(bf.QFileDialog, "getExistingDirectory", lambda *a: work)
    answers = h.Dialogs("no")

    def close_menus():
        m = QApplication.activePopupWidget()
        if isinstance(m, QMenu):
            acts = [a for a in m.actions() if not a.isSeparator() and not a.text().startswith("Send to")]
            if acts and rnd.random() < 0.5:
                rnd.choice(acts).trigger()
            m.close()
        elif m is not None:
            m.close()
    closer = QTimer(interval=40, timeout=close_menus)
    closer.start()
    h.scan(window, work)
    dog = Watchdog()
    keys = [Qt.Key_Tab, Qt.Key_Up, Qt.Key_Down, Qt.Key_Left, Qt.Key_Right, Qt.Key_Return, Qt.Key_F5, Qt.Key_Delete,
            Qt.Key_Space, Qt.Key_Backspace, Qt.Key_Escape, Qt.Key_Home, Qt.Key_End, Qt.Key_PageDown, Qt.Key_A]
    try:
        for n in range(steps):
            answers.answer = "yes" if rnd.random() < 0.15 else "no"
            target = QApplication.widgetAt(window.mapToGlobal(QPoint(rnd.randrange(window.width()),
                                                                     rnd.randrange(window.height())))) or window
            if not target.window() is window:
                target = window
            pos = QPoint(rnd.randrange(max(1, target.width())), rnd.randrange(max(1, target.height())))
            r = rnd.random()
            if r < 0.45:
                QTest.mouseClick(target, rnd.choice([Qt.LeftButton] * 4 + [Qt.RightButton]),
                                 rnd.choice([Qt.NoModifier] * 4 + [Qt.ControlModifier, Qt.ShiftModifier]), pos)
            elif r < 0.55:
                QTest.mouseDClick(target, Qt.LeftButton, Qt.NoModifier, pos)
            elif r < 0.6:
                QApplication.sendEvent(target, QMouseEvent(QEvent.MouseMove, QPointF(pos), QPointF(target.mapToGlobal(pos)), Qt.NoButton, Qt.NoButton, Qt.NoModifier))
            else:
                k = rnd.choice(keys)
                mod = Qt.ControlModifier if rnd.random() < 0.1 else Qt.NoModifier
                fw = QApplication.focusWidget() or window
                if fw is window.path and k == Qt.Key_A:
                    continue  # don't type garbage into the path box
                QTest.keyClick(fw, k, mod)
            if n % 50 == 0:
                gc.collect()
            h.pump(rnd.choice([0, 0, 5, 30]))
            if window.path.text() != work and h.scan_finished(window) and rnd.random() < 0.05:
                window.path.setText(work)
        h.wait_until(lambda: window.scanner is None or window.scanner.t1, 300, "last scan")
        h.pump(200)
    finally:
        closer.stop()
        answers.stop()
    assert dog.worst < 2.0, f"the window froze for {dog.worst:.1f}s"
    assert window.isVisible()
    root = h.scan(window, work)  # still works, and totals match what's on disk
    assert (root.size, root.nfiles) == h.truth(work)[work][:2]


# 30
@pytest.mark.sandbox
def test_whole_c_drive_and_memory_released(window, record_property):
    import psutil
    proc = psutil.Process()
    m0 = proc.memory_info().rss
    dog = Watchdog()  # and the window stays responsive while it scans a whole drive
    t0 = time.time()
    root = h.scan(window, "C:\\", timeout=3600)
    secs = time.time() - t0
    dog.t.stop()
    assert dog.worst < 2.0, f"the window froze for {dog.worst:.1f}s during the whole-drive scan"
    m1 = proc.memory_info().rss
    assert root.nfiles > 10_000 and window.status.text().startswith("Scanned C:\\")
    for tab in (1, 2, 0):
        window.tabs.setCurrentIndex(tab)
        h.pump(100)
    root = None
    h.scan(window, "C:\\", timeout=3600)
    gc.collect()
    h.pump(200)
    m2 = proc.memory_info().rss
    record_property("c_drive_scan_seconds", round(secs, 1))
    record_property("c_drive_rss_mb", [round(m0 / 2**20), round(m1 / 2**20), round(m2 / 2**20)])
    print(f"\nC:\\ scan {secs:.1f}s, RSS {m0 / 2**20:.0f} -> {m1 / 2**20:.0f} -> {m2 / 2**20:.0f} MB")
    assert m2 - m0 < (m1 - m0) * 1.6 + 50 * 2**20, "the first scan's tree wasn't released"


# 31
def test_error_log_written_and_app_keeps_running(window, work, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    old = sys.excepthook
    try:
        bf.install_error_log()
        QTimer.singleShot(0, lambda: 1 / 0)
        h.pump(100)
        log = tmp_path / "lad" / "Bigfoot" / "error.log"
        assert log.exists() and "ZeroDivisionError" in log.read_text(encoding="utf-8")
    finally:
        sys.excepthook = old
    h.make_tree(work, {"x.bin": 9})
    assert h.scan(window, work).size == 9


# 40
def test_two_windows_close_one_mid_scan(make_window, work, monkeypatch):
    for i in range(30):
        h.make_tree(os.path.join(work, "a", f"d{i}"), {f"f{j}.bin": 10 for j in range(10)})
    h.make_tree(os.path.join(work, "b"), {"x": {"y.bin": 5}, "z.bin": 6})
    real = bf.Scanner._scan

    def slow(self, d):
        time.sleep(0.02)
        real(self, d)
    monkeypatch.setattr(bf.Scanner, "_scan", slow)
    w1, w2 = make_window(), make_window()
    w1.path.setText(os.path.join(work, "a"))
    w1.scan()
    w2.path.setText(os.path.join(work, "b"))
    w2.scan()
    h.pump(150)
    w1.close()
    h.wait_until(lambda: h.scan_finished(w2), 60)
    assert w2.scanner.root.size == 11
    h.wait_until(lambda: w1.scanner.t1, 60, "closed window's scan to stop")
    gc.collect()
    h.pump(100)
    assert w2.isVisible()
    monkeypatch.setattr(bf.Scanner, "_scan", real)
    assert h.scan(w2, os.path.join(work, "a")).size == 3000


# 41
@pytest.mark.parametrize("case", ["unset", "is_a_file", "read_only_log"])
def test_error_log_location_unusable(work, case):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if case == "unset":
        env.pop("LOCALAPPDATA", None)
        env["USERPROFILE"] = work
    elif case == "is_a_file":
        h.put(os.path.join(work, "lad"), size=1)
        env["LOCALAPPDATA"] = os.path.join(work, "lad")  # makedirs can't create a folder here
    else:
        h.put(os.path.join(work, "lad", "Bigfoot", "error.log"), size=1)
        subprocess.run(["attrib", "+r", os.path.join(work, "lad", "Bigfoot", "error.log")], check=True)
        env["LOCALAPPDATA"] = os.path.join(work, "lad")
    code = f"""
import os, sys
sys.path[:0] = [{REPO!r}, {HERE!r}]
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
import bigfoot as bf, helpers as h
bf.QSettings = lambda *a, **k: QSettings({os.path.join(work, 'e.ini')!r}, QSettings.IniFormat)
bf.install_error_log()
app = QApplication([]); w = bf.Window(); w.show()
QTimer.singleShot(0, lambda: 1 / 0)  # an error while the log can't be written
h.pump(200)
os.makedirs({os.path.join(work, 'scanme')!r}, exist_ok=True)
print("alive", h.scan(w, {os.path.join(work, 'scanme')!r}).size, flush=True)
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert "alive 0" in r.stdout, r.stdout + r.stderr
