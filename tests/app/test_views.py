"""C. Views (plan tests 16-21) plus 38, 39, 42, 44."""
import json
import os
import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import bigfoot as bf
import helpers as h

SPEC = {
    "videos": {"a.mp4": 50_000, "b.mkv": 30_000, "clips": {"c.mov": 9_000}},
    "music": {"s.flac": 20_000, "t.mp3": 4_000},
    "docs": {"r.pdf": 3_000, "n.txt": 100, "code": {"m.py": 700, "d.json": 300}},
    "pics": {f"p{i}.jpg": 1_000 + i for i in range(150)},
    "archive.zip": 40_000, "noext": 77, "disk.iso": 25_000,
}


@pytest.fixture
def scanned(window, work):
    h.make_tree(work, SPEC)
    t0 = time.time() - 86400 * 400
    for i, (dp, _, fs) in enumerate(os.walk(work)):
        for j, f in enumerate(fs):
            os.utime(os.path.join(dp, f), (t0 + i * 1000 + j, t0 + i * 1000 + j))
    root = h.scan(window, work)
    return window, root, work


def rows(w, parent_idx, col=0):
    m = w.model
    return [m.index(r, col, parent_idx) for r in range(m.rowCount(parent_idx))]


# 16
def test_tree_sort_by_every_column_expand_collapse_files_toggle(scanned):
    w, root, work = scanned
    ri = w.model.index_for(root)
    w.tree.expand(ri)
    hdr = w.tree.header()
    keys = {bf.C_NAME: lambda x: x.name.lower(), bf.C_SIZE: lambda x: x.size, bf.C_PCT: lambda x: x.size,
            bf.C_FILES: lambda x: x.nfiles, bf.C_DIRS: lambda x: x.ndirs, bf.C_MOD: lambda x: x.mtime}
    for col, key in keys.items():
        for _ in range(2):  # click twice: both directions
            x = hdr.sectionViewportPosition(col) + hdr.sectionSize(col) // 2
            QTest.mouseClick(hdr.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(x, hdr.height() // 2))
            h.pump(20)
            ri = w.model.index_for(root)
            got = [i.internalPointer() for i in rows(w, ri)]
            desc = hdr.sortIndicatorOrder() == Qt.DescendingOrder
            assert [key(x) for x in got] == sorted((key(x) for x in got), reverse=desc), (bf.COLS[col], desc)
            assert len(got) == len(root.dirs) + len(root.files)
    # expand / collapse with the keyboard
    ri = w.model.index_for(root)
    vids = h.find_dir(root, os.path.join(work, "videos"))
    w.tree.setCurrentIndex(w.model.index_for(vids))
    w.tree.setFocus()
    QTest.keyClick(w.tree, Qt.Key_Right)
    h.pump(10)
    assert w.tree.isExpanded(w.model.index_for(vids))
    QTest.keyClick(w.tree, Qt.Key_Left)
    h.pump(10)
    assert not w.tree.isExpanded(w.model.index_for(vids))
    # Files in tree off: only folders; on again: files back
    n_on = w.model.rowCount(w.model.index_for(root))
    QTest.mouseClick(w.btn_files, Qt.LeftButton)
    h.pump(20)
    assert w.model.rowCount(w.model.index_for(root)) == len(root.dirs) < n_on
    QTest.mouseClick(w.btn_files, Qt.LeftButton)
    h.pump(20)
    assert w.model.rowCount(w.model.index_for(root)) == n_on


def _hit_center(w, it):
    for r, x, _ in w.map.hits:
        if x is it:
            return r.center().toPoint()
    raise AssertionError(f"{it.name} not drawn on the map")


# 17
def test_map_click_zoom_hover_legend(scanned):
    w, root, work = scanned
    w.tabs.setCurrentIndex(0)
    w.map.repaint()
    h.pump(50)
    big = h.find_dir(root, os.path.join(work, "videos", "a.mp4"))
    QTest.mouseClick(w.map, Qt.LeftButton, Qt.NoModifier, _hit_center(w, big))
    h.pump(200)
    assert w.tree.currentIndex().internalPointer() is big and w.map.sel is big
    # hover: tooltip with path and size
    pos = QPointF(_hit_center(w, big))  # a move event straight to the map (a test has no real cursor)
    QApplication.sendEvent(w.map, QMouseEvent(QEvent.MouseMove, pos, w.map.mapToGlobal(pos), Qt.NoButton,
                                              Qt.NoButton, Qt.NoModifier))
    h.pump(20)
    assert big.path in w.map.toolTip() and bf.human(big.size) in w.map.toolTip()
    # legend colour: the middle of a video tile is pink-ish, a zip is amber-ish
    img = w.map.grab().toImage()
    for name, cat in (("videos\\a.mp4", "Video"), ("archive.zip", "Archives")):
        it = h.find_dir(root, os.path.join(work, name))
        c = QColor(img.pixel(_hit_center(w, it) * w.map.devicePixelRatioF()))
        want = QColor(dict((n, col) for n, col, _ in bf.CATS)[cat])
        assert abs(c.hueF() - want.hueF()) < 0.06 or abs(c.hueF() - want.hueF()) > 0.94, (name, c.name(), want.name())
    # double-click a folder: zoom in one level
    vids = h.find_dir(root, os.path.join(work, "videos"))
    clips = h.find_dir(root, os.path.join(work, "videos", "clips"))
    QTest.mouseDClick(w.map, Qt.LeftButton, Qt.NoModifier, _hit_center(w, clips))
    h.pump(50)
    assert w.map.focus is vids and w.focus is vids
    assert vids.path in w.crumb.toolTip()
    w.map.repaint()
    QTest.mouseDClick(w.map, Qt.LeftButton, Qt.NoModifier, _hit_center(w, clips))
    h.pump(50)
    assert w.map.focus is clips
    # right-click zooms out, Up button zooms out, Backspace (map focused) zooms out
    QTest.mouseClick(w.map, Qt.RightButton, Qt.NoModifier, QPoint(10, 40))
    h.pump(50)
    assert w.map.focus is vids
    QTest.mouseClick(w.btn_up, Qt.LeftButton)
    h.pump(50)
    assert w.map.focus is root and not w.btn_up.isEnabled()
    w.map.repaint()
    QTest.mouseDClick(w.map, Qt.LeftButton, Qt.NoModifier, _hit_center(w, vids))
    h.pump(50)
    assert w.map.focus is vids
    w.map.setFocus()
    QTest.keyClick(w.map, Qt.Key_Backspace)
    h.pump(50)
    assert w.map.focus is root
    # two quick right-clicks don't zoom out then back in
    w.set_focus(clips)
    h.pump(20)
    QTest.mouseDClick(w.map, Qt.RightButton, Qt.NoModifier, QPoint(10, 40))
    h.pump(50)
    assert w.map.focus in (vids, root)


# 18
def test_types_add_up_and_sort(scanned):
    w, root, work = scanned
    w.tabs.setCurrentIndex(1)
    h.pump(50)
    t = w.types
    tot = sum(t.item(r, 2).key for r in range(t.rowCount()))
    n = sum(t.item(r, 4).key for r in range(t.rowCount()))
    assert tot == root.size and n == root.nfiles
    exts = {t.item(r, 1).text() for r in range(t.rowCount())}
    assert {".mp4", ".jpg", ".zip", "(no extension)"} <= exts
    cats = {t.item(r, 1).text(): t.item(r, 0).text() for r in range(t.rowCount())}
    assert cats[".mp4"].endswith("Video") and cats[".iso"].endswith("Disk images") and cats[".py"].endswith("Code + data")
    assert cats["(no extension)"].endswith("Other")
    hdr = t.horizontalHeader()
    for col in (4, 1, 2):
        x = hdr.sectionViewportPosition(col) + hdr.sectionSize(col) // 2
        QTest.mouseClick(hdr.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(x, 5))
        h.pump(10)
        vals = [getattr(t.item(r, col), "key", t.item(r, col).text()) for r in range(t.rowCount())]
        asc = hdr.sortIndicatorOrder() == Qt.AscendingOrder
        assert vals == sorted(vals, reverse=not asc), col
    # follows the selected folder
    w.set_focus(h.find_dir(root, os.path.join(work, "music")))
    h.pump(20)
    assert {t.item(r, 1).text() for r in range(t.rowCount())} == {".flac", ".mp3"}


# 19
def test_top_100_order_cap_open_and_explorer(scanned, no_launch):
    w, root, work = scanned
    w.tabs.setCurrentIndex(2)
    h.pump(50)
    t = w.top
    allf = sorted((f for d in [root] for f in bf.walk_files(d)), key=lambda f: f.size, reverse=True)
    assert len(allf) > 100 and t.rowCount() == 100
    assert [t.item(r, 1).key for r in range(100)] == [f.size for f in allf[:100]]
    assert t.item(0, 0).text() == "a.mp4" and t.item(0, 2).text() == os.path.join(work, "videos")
    # double-click a row: Show in Explorer
    r = t.visualItemRect(t.item(0, 0))
    QTest.mouseClick(t.viewport(), Qt.LeftButton, Qt.NoModifier, r.center())  # a real double-click starts with a click
    QTest.mouseDClick(t.viewport(), Qt.LeftButton, Qt.NoModifier, r.center())
    h.pump(20)
    assert no_launch[-1] == ("explorer", f'explorer /select,"{os.path.join(work, "videos", "a.mp4")}"')
    # right-click menu: Open
    QTest.mouseClick(t.viewport(), Qt.LeftButton, Qt.NoModifier, t.visualItemRect(t.item(1, 0)).center())
    seen = h.run_menu_action(lambda: w.top_menu(t.visualItemRect(t.item(1, 0)).center()), "Open")
    assert seen[:2] == ["Open", "Show in Explorer"] and any(s.startswith("Send to Recycle Bin") for s in seen)
    assert no_launch[-1] == ("open", os.path.join(work, "archive.zip"))
    # the Delete key in the table does nothing (it belongs to the tree)
    t.setFocus()
    QTest.keyClick(t, Qt.Key_Delete)
    h.pump(50)
    assert os.path.exists(os.path.join(work, "archive.zip"))


# 20
def test_live_refresh_during_scan(window, work, monkeypatch):
    for i in range(40):
        h.make_tree(os.path.join(work, f"d{i:02d}"), {f"s{j}": {"f.bin": 1000} for j in range(5)})
    real = bf.Scanner._scan

    def slow(self, d):
        time.sleep(0.01)
        real(self, d)
    monkeypatch.setattr(bf.Scanner, "_scan", slow)
    window.live.setInterval(100)
    window.path.setText(work)
    window.scan()
    h.pump(300)
    root = window.scanner.root
    ri = window.model.index_for(root)
    assert window.tree.isExpanded(ri), "the tree should fill in as it scans"
    first = window.model.index(0, 0, ri)
    if first.isValid():
        h.click_index(window.tree, first)
        picked = first.internalPointer()
    sizes = []
    while not h.scan_finished(window):
        h.pump(120)
        sizes.append(root.size)
        if first.isValid():
            assert picked in window.selected_items(), "selection lost in a live refresh"
    assert sizes == sorted(sizes), "totals went down during the scan"
    assert root.size == 40 * 5 * 1000


# 21
def test_window_size_splitter_columns_last_folder_remembered(make_window, work, ini_settings):
    h.make_tree(work, {"a.bin": 5})
    w = make_window()
    h.scan(w, work)
    w.resize(1111, 707)
    w.move(60, 70)
    w.split.setSizes([300, 700])
    w.tree.setColumnWidth(bf.C_FILES, 140)
    h.pump(50)
    sizes, geo = w.split.sizes(), (w.width(), w.height(), w.x(), w.y())
    w.close()
    h.pump(50)
    w2 = make_window(size=None)
    h.pump(50)
    assert (w2.width(), w2.height()) == geo[:2]
    assert abs(w2.x() - geo[2]) <= 80 and abs(w2.y() - geo[3]) <= 80  # frame vs client origin; drift checked below
    pos2 = (w2.x(), w2.y())
    w2.close()
    h.pump(50)
    w3 = make_window(size=None)
    h.pump(50)
    assert (w3.x(), w3.y(), w3.width(), w3.height()) == pos2 + geo[:2], "the window drifts on every restart"
    w2 = w3
    assert w2.split.sizes() == sizes
    assert w2.tree.columnWidth(bf.C_FILES) == 140
    assert w2.path.text() == work


# 38
@pytest.mark.parametrize("scale", ["1", "1.5", "2"])
def test_high_dpi_and_extreme_window_sizes(work, scale):
    h.make_tree(work, SPEC)
    code = f"""
import os, sys
sys.path[:0] = [{os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))!r}, {os.path.dirname(os.path.abspath(__file__))!r}]
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
import bigfoot as bf, helpers as h
bf.QSettings = lambda *a, **k: QSettings({os.path.join(work, 'dpi.ini')!r}, QSettings.IniFormat)
app = QApplication([]); bf.apply_theme(app)
w = bf.Window(); w.show(); h.pump(50)
root = h.scan(w, {work!r})
for size in ((200, 150), (5000, 3000), (1300, 800)):
    w.resize(*size)
    for tab in (0, 1, 2):
        w.tabs.setCurrentIndex(tab); w.repaint(); h.pump(30)
    assert w.width() >= 150
w.map.repaint(); h.pump(30)
assert w.map.hits, "nothing drawn on the map"
assert w.map.pix.devicePixelRatio() == w.map.devicePixelRatioF()
print("OK", w.map.devicePixelRatioF())
"""
    env = dict(os.environ, QT_SCALE_FACTOR=scale, PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr


def test_keyboard_only(window, work, dialogs):
    h.make_tree(work, {"k": {"a.bin": 10, "b.bin": 20}})
    QTest.keyClick(window, Qt.Key_L, Qt.ControlModifier)
    QTest.keyClicks(window.path, work)
    QTest.keyClick(window.path, Qt.Key_Return)
    h.wait_until(lambda: h.scan_finished(window), 30)
    order = []
    window.activateWindow()
    window.path.setFocus()
    for _ in range(12):  # Tab walks through every control without getting stuck
        QTest.keyClick(QGuiApplication.focusObject(), Qt.Key_Tab)
        h.pump(5)
        order.append(type(QGuiApplication.focusObject()).__name__)
    assert "QTreeView" in order
    window.tree.setFocus()
    window.tree.setCurrentIndex(window.model.index_for(window.scanner.root))
    QTest.keyClick(window.tree, Qt.Key_Right)
    QTest.keyClick(window.tree, Qt.Key_Down)
    h.pump(10)
    sel = window.tree.currentIndex().internalPointer()
    window.tree.selectionModel().select(window.tree.currentIndex(), bf.QItemSelectionModel.ClearAndSelect |
                                        bf.QItemSelectionModel.Rows)
    dialogs.answer = "no"  # Escape-equivalent: the dialog is cancelled
    QTest.keyClick(window.tree, Qt.Key_Delete)
    h.wait_until(lambda: dialogs.texts, 5, "confirm dialog")
    assert sel.path in dialogs.texts[-1] and os.path.exists(sel.path)


# 39
@pytest.mark.parametrize("junk", ["text", "int", "bytes", "deleted_path", "network_path", "empty"])
def test_corrupt_settings_still_open(make_window, ini_settings, work, junk):
    s = QSettings(ini_settings, QSettings.IniFormat)
    val = {"text": "garbage!", "int": 12345, "bytes": b"\x00\xffnot a qt state", "empty": "",
           "deleted_path": "garbage", "network_path": "garbage"}[junk]
    for k in ("geometry", "split", "header2"):
        s.setValue(k, val)
    s.setValue("last_path", {"deleted_path": os.path.join(work, "gone"), "network_path": r"\\10.255.255.1\x"}.get(
        junk, val))
    s.sync()
    t0 = time.time()
    w = make_window()
    assert time.time() - t0 < 5
    assert w.isVisible() and w.width() >= 200 and w.height() >= 150
    h.make_tree(work, {"ok.bin": 3})
    assert h.scan(w, work).size == 3


# 42
@pytest.mark.sandbox  # uses the real clipboard
def test_clipboard_1000_paths_and_unicode(window, work):
    names = [f"f{i:04d}_日本_😀.bin" for i in range(1000)]
    for n in names:
        h.put(os.path.join(work, n), size=1)
    root = h.scan(window, work)
    window.copy_paths(list(root.files))
    got = QGuiApplication.clipboard().text().split("\n")
    assert sorted(got) == sorted(os.path.join(work, n) for n in names)
    assert window.status.text() == "Copied 1000 paths"
    # Ctrl+C in the tree copies the selection
    h.select_items(window, [root.files[0], root.files[1]])
    window.tree.setFocus()
    QTest.keyClick(window.tree, Qt.Key_C, Qt.ControlModifier)
    h.pump(20)
    assert sorted(QGuiApplication.clipboard().text().split("\n")) == sorted([root.files[0].path, root.files[1].path])
    # another app sees the same text: paste it into Notepad (WM_PASTE, no keyboard needed)
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        native = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
    assert sorted(native.splitlines()) == sorted([root.files[0].path, root.files[1].path])
    from pywinauto import Application
    n = subprocess.Popen(["notepad.exe"])
    try:
        notepad = Application(backend="win32").connect(process=n.pid, timeout=30).window(class_name="Notepad")
        ed = notepad.child_window(class_name="Edit")
        ed.wait("exists", 30)
        ed.send_message(0x0302, 0, 0)  # WM_PASTE
        h.pump(300)
        pasted = ed.window_text().splitlines()
        assert sorted(pasted) == sorted([root.files[0].path, root.files[1].path]), pasted
    finally:
        n.kill()
    window.activateWindow()  # shortcuts only fire in the active window (Notepad took it)
    h.wait_until(window.isActiveWindow, 10, "Bigfoot active again")
    window.tree.setFocus()  # (keys reach the shortcut only through the focused widget)
    # nothing selected: clipboard untouched
    QGuiApplication.clipboard().setText("keep me")
    window.tree.clearSelection()
    QTest.keyClick(window.tree, Qt.Key_C, Qt.ControlModifier)
    h.pump(20)
    assert QGuiApplication.clipboard().text() == "keep me"


# 44
@pytest.mark.parametrize("tz", ["Tokyo Standard Time", "Pacific Standard Time", "UTC"])
@pytest.mark.parametrize("culture", ["de-DE", "ar-SA", "ja-JP"])
@pytest.mark.sandbox
def test_locale_and_time_zone(work, tz, culture):
    h.put(os.path.join(work, "dated.bin"), size=1_234_567, mtime=1_700_000_000)  # 2023-11-14 22:13:20 UTC
    old_tz = subprocess.run(["tzutil", "/g"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["tzutil", "/s", tz], check=True)
    subprocess.run(["powershell", "-NoProfile", "-Command", f"Set-Culture {culture}"], check=True)
    try:
        code = f"""
import os, sys, time
sys.path[:0] = [{os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))!r}, {os.path.dirname(os.path.abspath(__file__))!r}]
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
import bigfoot as bf, helpers as h
bf.QSettings = lambda *a, **k: QSettings({os.path.join(work, 'loc.ini')!r}, QSettings.IniFormat)
app = QApplication([]); w = bf.Window(); w.show()
root = h.scan(w, {work!r})
f = root.files[0]
w.tabs.setCurrentIndex(2); h.pump(30)
import json
print(json.dumps([w.top.item(0, 3).text(), w.top.item(0, 1).text(), time.strftime('%Y-%m-%d %H:%M', time.localtime(1_700_000_000))]))
"""
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        assert r.returncode == 0, r.stderr
        date_cell, size_cell, expect = json.loads(r.stdout.strip().splitlines()[-1])
        assert date_cell == expect and size_cell == "1.2 MB"
        offsets = {"Tokyo Standard Time": "2023-11-15 07:13", "Pacific Standard Time": "2023-11-14 14:13",
                   "UTC": "2023-11-14 22:13"}
        assert date_cell == offsets[tz]
    finally:
        subprocess.run(["tzutil", "/s", old_tz])
        subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Culture en-US"])
