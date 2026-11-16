"""Shared helpers for the Bigfoot tests: build test trees, compute ground truth, drive the window, read the Recycle Bin."""
import ctypes
import hashlib
import os
import random
import struct
import subprocess
import time
from ctypes import wintypes as w

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

IN_SANDBOX = os.environ.get("USERNAME", "").upper() == "WDAGUTILITYACCOUNT"


def lp(p):
    """Extended-length form so tests can create and read names Windows normally rewrites."""
    if p.startswith("\\\\?\\"):
        return p
    if not os.path.isabs(p):
        p = os.path.abspath(p)  # (abspath on an absolute path would strip a trailing '.' or ' ')
    return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p


# ------------------------------------------------------------------ files
_k = ctypes.WinDLL("kernel32", use_last_error=True)
_k.CreateFileW.restype = w.HANDLE
_k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
_k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD),
                               w.LPVOID]
_k.SetFilePointerEx.argtypes = [w.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), w.DWORD]
_k.SetEndOfFile.argtypes = [w.HANDLE]
_k.CloseHandle.argtypes = [w.HANDLE]
INVALID = w.HANDLE(-1).value


def sparse(path, size):
    """A file with a big logical size and ~0 bytes on disk (never use f.truncate: it writes real zeros)."""
    os.makedirs(lp(os.path.dirname(path)), exist_ok=True)
    h = _k.CreateFileW(lp(path), 0xC0000000, 0, None, 2, 0x80, None)
    if h == INVALID:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        n = w.DWORD()
        if not _k.DeviceIoControl(h, 0x900C4, None, 0, None, 0, ctypes.byref(n), None):  # FSCTL_SET_SPARSE
            raise ctypes.WinError(ctypes.get_last_error())
        if not _k.SetFilePointerEx(h, size, None, 0) or not _k.SetEndOfFile(h):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k.CloseHandle(h)


def put(path, size=None, data=None, mtime=None):
    """A real file with random content (so hashes mean something)."""
    os.makedirs(lp(os.path.dirname(path)), exist_ok=True)
    if data is None:
        data = os.urandom(size if size is not None else random.randint(0, 4096))
    with open(lp(path), "wb") as f:
        f.write(data)
    if mtime is not None:
        os.utime(lp(path), (mtime, mtime))
    return path


def make_tree(root, spec):
    """spec: {"name": size_or_bytes_or_dict}; dicts are folders. Returns root."""
    os.makedirs(lp(root), exist_ok=True)
    for name, v in spec.items():
        p = os.path.join(root, name)
        if isinstance(v, dict):
            make_tree(p, v)
        elif isinstance(v, bytes):
            put(p, data=v)
        else:
            put(p, size=v)
    return root


def random_tree(root, seed, max_depth=5, max_entries=8, names=None):
    rnd = random.Random(seed)
    alphabet = names or ["a", "b", "data", "x y", "é", "日本", "файл", "v1.2", "😀", "Ünï", "z" * 40, "ab_c-d", "new"]

    def fill(d, depth):
        os.makedirs(lp(d), exist_ok=True)
        used = set()
        for _ in range(rnd.randint(0, max_entries)):
            nm = f"{rnd.choice(alphabet)}{rnd.randint(0, 999)}"
            if nm.lower() in used:
                continue
            used.add(nm.lower())
            if depth < max_depth and rnd.random() < 0.35:
                fill(os.path.join(d, nm), depth + 1)
            else:
                r = rnd.random()
                p = os.path.join(d, nm + rnd.choice(["", ".txt", ".mp4", ".jpg", ".zip", ".py", ".TS", ".tar.gz"]))
                if r < 0.1:
                    put(p, data=b"")
                elif r < 0.15:
                    sparse(p, rnd.randint(1, 1 << 34))
                else:
                    put(p, size=rnd.randint(1, 20000))
    fill(root, 0)
    return root


def truth(root):
    """Ground truth with Bigfoot's rules: links/junctions skipped, sizes are logical (st_size).
    Returns {dir_path: (size, nfiles, ndirs)} for every folder under root (root included)."""
    out = {}

    def walk(d):
        size = nf = nd = 0
        with os.scandir(lp(d)) as it:
            entries = list(it)
        for e in entries:
            if e.is_symlink() or e.is_junction():
                continue
            if e.is_dir(follow_symlinks=False):
                s, f, n = walk(os.path.join(d, e.name))
                size += s
                nf += f
                nd += n + 1
            else:
                size += e.stat(follow_symlinks=False).st_size
                nf += 1
        out[d] = (size, nf, nd)
        return size, nf, nd

    walk(root)
    return out


def walk_no_links(top):
    """os.walk that never enters junctions or symlinks (os.walk follows junctions even with followlinks=False)."""
    stack = [top]
    while stack:
        d = stack.pop()
        dirs, files = [], []
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_symlink() or e.is_junction():
                        continue
                    (dirs if e.is_dir(follow_symlinks=False) else files).append(e.name)
        except OSError:
            continue
        yield d, dirs, files
        stack.extend(os.path.join(d, x) for x in dirs)


def walk_files_no_links(top):
    for dp, _, fs in walk_no_links(top):
        for f in fs:
            yield os.path.join(dp, f)


def tree_hash(root):
    """{relative path: sha1} for every file (and '<dir>' for folders) under root."""
    out = {}
    base = lp(root).rstrip("\\") + "\\"
    for dp, dns, fns in walk_no_links(lp(root)):
        rel = dp[len(base):] if dp.startswith(base) else ""  # no relpath/normpath: they strip trailing dots
        for d in dns:
            out[rel + "\\" + d if rel else d] = "<dir>"
        for f in fns:
            h = hashlib.sha1()
            with open(dp + "\\" + f, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out[rel + "\\" + f if rel else f] = h.hexdigest()
    return out


def find_dir(root_dir, path):
    """The scanned Dir/File object for an absolute path."""
    base = root_dir.path.rstrip("\\") + "\\"
    assert path == root_dir.path or path.startswith(base), path
    parts = [] if path == root_dir.path else path[len(base):].split("\\")  # no relpath: it strips trailing dots
    it = root_dir
    for part in parts:
        nxt = next((d for d in it.dirs if d.name == part), None) or next((f for f in it.files if f.name == part), None)
        if nxt is None:
            raise KeyError(path)
        it = nxt
    return it


# ------------------------------------------------------------------ Qt driving
def pump(ms=0):
    end = time.time() + ms / 1000
    while True:
        QApplication.processEvents(QEventLoop.AllEvents, 20)
        if time.time() >= end:
            return
        time.sleep(0.002)


def reachable(path, timeout=10):
    """True if a (network) folder answers within timeout seconds."""
    import threading
    got = []
    t = threading.Thread(target=lambda: got.append(os.path.isdir(path)), daemon=True)
    t.start()
    t.join(timeout)
    return bool(got and got[0])


def wait_until(cond, timeout=120, msg="condition"):
    end = time.time() + timeout
    while not cond():
        if time.time() > end:
            raise TimeoutError(f"timed out after {timeout}s waiting for {msg}")
        QApplication.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.002)


def scan_finished(w):
    return w.scanner is not None and bool(w.scanner.t1) and w.btn_scan.text() == "Scan" and (
        w.status.text().startswith(("Scanned", "Stopped")))


def scan(w, path, timeout=600):
    w.path.setText(path)
    w.scan()
    wait_until(lambda: scan_finished(w), timeout, f"scan of {path}")
    pump(50)
    return w.scanner.root


def rect_of(view, idx):
    return view.visualRect(idx)


def click_index(view, idx, mods=Qt.NoModifier, button=Qt.LeftButton, double=False):
    view.scrollTo(idx)
    pump(10)
    r = view.visualRect(idx)
    pos = QPoint(min(r.left() + 30, r.right() - 2), r.center().y())
    if double:
        QTest.mouseClick(view.viewport(), button, mods, pos)  # Qt's DblClick event comes after a click
        QTest.mouseDClick(view.viewport(), button, mods, pos)
    else:
        QTest.mouseClick(view.viewport(), button, mods, pos)
    pump(10)


def select_items(w, items, mods=Qt.ControlModifier):
    """Real clicks in the tree: first item plain, the rest Ctrl+click."""
    for n, it in enumerate(items):
        w.select_in_tree(it) if not w.model.index_for(it).isValid() else None
        expand_to(w, it)
        idx = w.model.index_for(it)
        assert idx.isValid(), f"{it.name} not shown in the tree"
        click_index(w.tree, idx, Qt.NoModifier if n == 0 else mods)


def expand_to(w, it):
    chain, p = [], it.parent
    while p is not None:
        chain.append(p)
        p = p.parent
    for d in reversed(chain):
        w.tree.setExpanded(w.model.index_for(d), True)
    pump(5)


class Dialogs:
    """Answers every QMessageBox that pops up (real buttons, clicked) and records its text."""

    def __init__(self, answer="yes"):
        self.answer = answer
        self.texts = []
        self.timer = QTimer(interval=15, timeout=self._poll)
        self.timer.start()

    def _poll(self):
        m = QApplication.activeModalWidget()
        if isinstance(m, QMessageBox) and m.isVisible():
            self.texts.append(m.text())
            btn = m.button(QMessageBox.Yes if self.answer == "yes" else QMessageBox.No)
            if btn is None:
                btn = m.defaultButton() or m.buttons()[0]
            QTest.mouseClick(btn, Qt.LeftButton)

    def stop(self):
        self.timer.stop()


def run_menu_action(open_menu, text_prefix):
    """Opens a context menu (open_menu() blocks in QMenu.exec) and clicks the action whose text starts with
    text_prefix. Returns the list of action texts seen."""
    seen = []

    def poll():
        m = QApplication.activePopupWidget()
        if isinstance(m, QMenu) and m.isVisible():
            t.stop()
            acts = [a for a in m.actions() if not a.isSeparator()]
            seen.extend(a.text() for a in acts)
            for a in acts:
                if a.text().startswith(text_prefix):
                    QTest.mouseClick(m, Qt.LeftButton, Qt.NoModifier, m.actionGeometry(a).center())
                    return
            m.close()

    t = QTimer(interval=15, timeout=poll)
    t.start()
    open_menu()
    t.stop()
    pump(20)
    return seen


# ------------------------------------------------------------------ Recycle Bin (read the real one)
def my_sid():
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
    return out.strip().split(",")[-1].strip('"')


def recycle_bin_entries(drive="C:"):
    """[(original_path, size, $R path)] from the current user's Recycle Bin on a drive ($I file format v1/v2)."""
    d = os.path.join(drive + "\\", "$Recycle.Bin", my_sid())
    out = []
    if not os.path.isdir(d):
        return out
    for n in os.listdir(d):
        if not n.startswith("$I"):
            continue
        with open(os.path.join(d, n), "rb") as f:
            b = f.read()
        ver, size = struct.unpack_from("<qq", b, 0)
        if ver == 2:
            ln = struct.unpack_from("<i", b, 24)[0]
            orig = b[28:28 + ln * 2].decode("utf-16-le", "surrogatepass").rstrip("\0")
        else:
            orig = b[24:24 + 520].decode("utf-16-le", "surrogatepass").split("\0")[0]
        out.append((orig, size, os.path.join(d, "$R" + n[2:]), os.path.join(d, n)))
    return out


def in_bin(path):
    return [e for e in recycle_bin_entries(path[:2]) if os.path.normcase(e[0]) == os.path.normcase(path)]


def restore(path):
    """Does what Explorer's Restore does: move the $R item back to its original path, drop the $I record."""
    hits = in_bin(path)
    assert hits, f"{path} is not in the Recycle Bin"
    orig, _, r, i = hits[-1]
    try:
        os.rename(lp(r), lp(orig))
    except OSError:  # recycled from another volume (e.g. a mapped folder): copy back
        import shutil
        shutil.move(lp(r), lp(orig))
    os.remove(i)


def empty_bin():
    subprocess.run(["powershell", "-NoProfile", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
                   capture_output=True)


def make_vhd(path, size_mb):
    """A fixed-size VHD file (raw disk + 512-byte footer) that Windows can mount without diskpart."""
    import uuid
    size = size_mb * 2**20
    with open(path, "wb") as f:
        f.truncate(size)  # real zeros on purpose: a small fixed disk image
        cyl, heads, spt = size // 512 // (16 * 63), 16, 63
        footer = bytearray(512)
        struct.pack_into(">8sIIQI4sI4sQQHBBI", footer, 0, b"conectix", 2, 0x00010000, 0xFFFFFFFFFFFFFFFF, 0,
                         b"bft ", 0x00010000, b"Wi2k", size, size, min(cyl, 65535), heads, spt, 2)
        footer[68:84] = uuid.uuid4().bytes
        struct.pack_into(">I", footer, 64, ~sum(footer) & 0xFFFFFFFF)
        f.seek(size)
        f.write(footer)
    return path
