"""Bigfoot - find what's eating your disk. Fast scan, biggest-first tree, treemap, type breakdown, top files."""
import ctypes
import gc
import html
import heapq
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import winreg
import suite_style

from PySide6.QtCore import (QAbstractEventDispatcher, QAbstractItemModel, QAbstractTableModel, QByteArray, QEvent,
                            QItemSelectionModel, QModelIndex, QObject, QPointF, QRectF, QSettings, QSize, QStorageInfo,
                            Qt, QTimer, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QFontMetrics, QGuiApplication, QKeySequence,
                           QLinearGradient, QPainter, QPalette, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QComboBox, QFileDialog, QFileIconProvider,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy, QSplitter, QStackedWidget, QStyle,
                               QStyledItemDelegate, QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton,
                               QTreeView, QVBoxLayout, QWidget)
from PySide6.QtCore import QFile

APP = "Bigfoot"
VERSION = "1.2.1"
ACCENT = "#2f9bff"

# ---------------------------------------------------------------- file types
CATS = [
    ("Video", "#ff5c8a", "mp4 mkv mov avi webm wmv m4v flv mpg mpeg ts m2ts 3gp vob ogv mts"),
    ("Audio", "#2f9bff", "wav mp3 flac aiff aif m4a aac ogg oga opus wma alac ape wv mka mid midi"),
    ("Images", "#2bd88a", "jpg jpeg png gif bmp tif tiff webp heic heif raw cr2 cr3 nef arw dng psd svg ico"),
    ("Archives", "#ffb020", "zip rar 7z tar gz tgz bz2 xz zst cab"),
    ("Disk images", "#ff7a2f", "iso img vhd vhdx vmdk vdi dmg wim esd"),
    ("Programs", "#a46bff", "exe msi dll sys msix appx appxbundle msixbundle jar apk aab"),
    ("Documents", "#e8e26a", "pdf doc docx xls xlsx ppt pptx txt rtf odt ods csv md epub"),
    ("Code + data", "#4fd6e8", "py js html css json xml c cpp h cs java go rs php sql db sqlite log"),
]
EXT_CAT = {e: (name, col) for name, col, exts in CATS for e in exts.split()}
OTHER = ("Other", "#7d8794")


def ext_of(name):
    i = name.rfind(".")
    return name[i + 1:].lower() if i > 0 else ""


def cat_of(name):
    return EXT_CAT.get(ext_of(name), OTHER)


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if u == "TB" or round(n, 0 if u == "B" else 1) < 1024:  # unit picked after rounding: no "1024.0 KB"
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def fdate(t):
    if not t:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    except (OSError, OverflowError, ValueError):  # NTFS allows dates localtime can't convert
        return "?"


def long_path(p):
    """Extended-length form: folders deeper than MAX_PATH, and names ending in '.' or ' ', scan as themselves."""
    if p.startswith("\\\\?\\"):
        return p
    return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p


def recycle_refusal(path, size, batch=None):
    """Why Windows would delete this permanently instead of recycling it, or None. In those cases
    QFile.moveToTrash deletes without a word and still reports success, so Bigfoot refuses up front.
    batch: {volume: bytes} of the items already accepted in this Delete: items that together don't fit make
    Windows purge the ones recycled a moment earlier."""
    k32 = ctypes.windll.kernel32
    vol, guid = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(64)
    if not (k32.GetVolumePathNameW(path, vol, 1024) and k32.GetVolumeNameForVolumeMountPointW(vol.value, guid, 64)):
        return None  # not a local volume (share, subst, long path): moveToTrash refuses those by itself
    if k32.GetDriveTypeW(vol.value) != 3:  # only fixed drives have a Recycle Bin (shares and subst returned above)
        return ("it's not on a fixed drive (a USB stick, memory card, CD/DVD or RAM disk has no Recycle Bin), "
                "so Windows would delete it for good")

    def setting(key, name, hive=winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None
    policy = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    bin_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + guid.value[10:-1]
    if (setting(policy, "NoRecycleFiles") or setting(policy, "NoRecycleFiles", winreg.HKEY_LOCAL_MACHINE)
            or setting(bin_key, "NukeOnDelete")):
        return "Windows is set to delete files on this drive instead of recycling them"
    total = QStorageInfo(vol.value).bytesTotal()
    cap = setting(bin_key, "MaxCapacity")  # MB; Windows' default when it was never changed is 5% of the drive
    cap = cap * 2**20 if cap is not None else total // 20
    pcts = [p for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
            if (p := setting(policy, "RecycleBinSize", hive)) is not None]
    if pcts:  # a Group Policy size limit (percent of the drive, 0 included) overrides the user's; the smaller wins
        cap = min(cap, total * min(pcts) // 100)
    batch = {} if batch is None else batch
    before = batch.get(guid.value, 0)
    if before + size > cap:
        why = "it's bigger than" if not before else "with the rest of the selection it won't fit in"
        return f"{why} this drive's Recycle Bin ({human(cap)}), so Windows would delete it for good"
    batch[guid.value] = before + size
    return None


# ---------------------------------------------------------------- size on disk
# Every size Bigfoot ranks and totals is what the file occupies on the drive ("size on disk"), not its length.
# A sparse file (a VM disk, a placeholder) can be 400 GB long and take nothing; a compressed file takes less than
# its length; a cloud placeholder (OneDrive "online-only") takes nothing here; and a hard link is the same
# bytes under a second name. Adding up lengths made a drive "1.3 TB" on a 455 GB disk.
# The length stays on each file as lsize, for the one place Windows itself goes by length: the Recycle Bin size check.
_PLACEHOLDER = 0x1000 | 0x40000 | 0x400000  # OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS: data isn't on this PC
_ALLOCATED = 0x200 | 0x800 | 0x400          # SPARSE_FILE, COMPRESSED, REPARSE_POINT (Windows compact, dedup, OneDrive)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetCompressedFileSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
_k32.GetCompressedFileSizeW.restype = ctypes.c_ulong


def disk_size(path, attrs, length):
    """Bytes the file occupies on disk. path: long form. attrs: its st_file_attributes. A placeholder counts 0 and is
    never opened (opening one can make OneDrive download it). Only sparse, compressed and other reparse-point
    files need a call; for any other file the length is the size."""
    if attrs & _PLACEHOLDER:
        return 0
    if attrs & _ALLOCATED:
        hi = ctypes.c_ulong(0)
        ctypes.set_last_error(0)
        lo = _k32.GetCompressedFileSizeW(path, ctypes.byref(hi))
        if lo == 0xFFFFFFFF and ctypes.get_last_error():
            return length  # can't ask (access denied, a link Windows won't open): the length is the safe answer
        return (hi.value << 32) | lo
    return length


# ---------------------------------------------------------------- tree
class File:
    __slots__ = ("name", "parent", "size", "lsize", "mtime", "_row", "link")
    is_dir = False
    nfiles, ndirs = 1, 0

    def __init__(self, name, parent, size, mtime, lsize=None):
        # size: on disk (what is ranked and totalled). lsize: the file's length (Recycle Bin check only).
        # link: the list of Files (sorted by path) that are hard links to one file, or None for a file with one name
        self.name, self.parent, self.size, self.mtime, self._row = name, parent, size, mtime, 0
        self.lsize = size if lsize is None else lsize
        self.link = None

    @property
    def path(self):
        return os.path.join(self.parent.path, self.name)


class Dir:
    __slots__ = ("name", "path", "parent", "dirs", "files", "own", "lown", "own_mt", "size", "lsize", "nfiles",
                 "ndirs", "mtime", "denied", "skipped", "_row", "_kids")
    is_dir = True

    def __init__(self, name, path, parent, mtime=0):
        self.name, self.path, self.parent = name, path, parent
        self.dirs, self.files = [], []
        self.own = self.lown = self.size = self.lsize = self.nfiles = self.ndirs = 0
        self.own_mt = self.mtime = mtime
        self.denied = self.skipped = False
        self._row, self._kids = 0, None


def retotal(root):
    """Post-order sum of sizes/counts/newest mtime. Cheap: uses per-dir accumulators, not per-file sums."""
    order, stack = [], [root]
    while stack:
        d = stack.pop()
        order.append(d)
        stack.extend(d.dirs)
    for d in reversed(order):
        size, lsize, nf, nd, mt = d.own, d.lown, len(d.files), len(d.dirs), d.own_mt
        for c in d.dirs:
            size += c.size
            lsize += c.lsize
            nf += c.nfiles
            nd += c.ndirs
            if c.mtime > mt:
                mt = c.mtime
        d.size, d.lsize, d.nfiles, d.ndirs, d.mtime = size, lsize, nf, nd, mt
        d._kids = None


def walk_files(d):
    stack = [d]
    while stack:
        x = stack.pop()
        yield from x.files
        stack.extend(x.dirs)


# ---------------------------------------------------------------- scanner
class Scanner(QObject):
    finished = Signal(object)  # carries the scanner, so a stale finish can be told apart

    def __init__(self, path, threads=16):
        super().__init__()
        path = os.path.abspath(path)
        self.root = Dir(path, path, None)
        self.q = queue.Queue()
        self.stopped = False
        self.files = self.dirs = self.bytes = self.denied = 0
        self.current = path
        self.t0 = self.t1 = 0
        self.n = threads
        self.links = {}  # while scanning: (volume, file id) -> the Files that are hard links to one file
        self.links_lock = threading.Lock()
        self.groups = []  # after the scan: the lists of 2+ names for one file (File.link points at its list)
        self.go = threading.Event()  # cleared while the window has work to do: see Window.gui_busy
        self.go.set()

    def start(self):
        self.t0 = time.time()
        self.q.put(self.root)
        self.workers = [threading.Thread(target=self._work, daemon=True) for _ in range(self.n)]
        for t in self.workers:
            t.start()
        threading.Thread(target=self._wait, daemon=True).start()

    def stop(self):
        self.stopped = True

    def _wait(self):
        self.q.join()
        for _ in self.workers:
            self.q.put(None)
        self._count_links_once()
        self.t1 = time.time()
        self.finished.emit(self)

    def _count_links_once(self):
        """Hard links are one file under several names: the first name by path keeps the size on disk, the others
        count 0 (still listed, still counted as files). Decided here, once every name is known, so the result
        doesn't depend on which scanner thread got there first."""
        for files in self.links.values():
            if len(files) > 1:
                files.sort(key=lambda f: f.path)
                self.groups.append(files)
                for f in files:
                    f.link = files  # kept: recycling the name that holds the size hands it to the next name
                for f in files[1:]:
                    f.parent.own -= f.size
                    self.bytes -= f.size
                    f.size = 0
        self.links = {}

    def _work(self):
        while True:
            d = self.q.get()
            if d is None:
                return
            try:
                if self.stopped:
                    d.skipped = True  # never listed: its size is unknown, not zero
                else:
                    self._scan(d)
            except Exception:
                d.denied = True
            finally:
                self.q.task_done()

    def _scan(self, d):
        self.current = d.path
        own, nf = 0, 0
        try:
            with os.scandir(long_path(d.path)) as it:
                for n, e in enumerate(it):
                    if not n & 255:
                        self.go.wait(0.2)  # (never more than 0.2 s: the window may stay busy)
                    try:
                        if e.is_symlink() or e.is_junction():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            sub = Dir(e.name, os.path.join(d.path, e.name), d, st.st_mtime)
                            d.dirs.append(sub)
                            self.dirs += 1
                            self.q.put(sub)
                        else:
                            st = e.stat(follow_symlinks=False)
                            attrs, length, link = st.st_file_attributes, st.st_size, None
                            size = disk_size(e.path, attrs, length)
                            if length and not attrs & _PLACEHOLDER:  # (a placeholder is never opened, not even for this)
                                try:  # the directory listing has no link count: one cheap attribute open per file
                                    s2 = os.stat(e.path, follow_symlinks=False)
                                    if s2.st_nlink > 1:
                                        link = (s2.st_dev, s2.st_ino)
                                except OSError:
                                    pass
                            f = File(e.name, d, size, st.st_mtime, length)
                            d.files.append(f)
                            if link is not None:
                                with self.links_lock:
                                    self.links.setdefault(link, []).append(f)
                            d.own += size  # kept current so a live refresh shows the size so far
                            d.lown += length
                            own += size
                            nf += 1
                            if st.st_mtime > d.own_mt:
                                d.own_mt = st.st_mtime
                    except OSError:
                        pass
        except OSError:
            d.denied = True
            self.denied += 1
        self.files += nf
        self.bytes += own


# ---------------------------------------------------------------- model
COLS = ["Name", "Size on disk", "% of drive", "Files", "Folders", "Modified"]
C_NAME, C_SIZE, C_PCT, C_FILES, C_DIRS, C_MOD = range(6)


class TreeModel(QAbstractItemModel):
    def __init__(self):
        super().__init__()
        self.root = None
        self.sort_col, self.desc = C_SIZE, True
        self.show_files = True
        self.drive_total = 0
        self.gen = 0
        ip = QFileIconProvider()
        self.ic_dir = ip.icon(QFileIconProvider.Folder)
        self.ic_file = ip.icon(QFileIconProvider.File)
        self.ic_drive = ip.icon(QFileIconProvider.Drive)

    def set_root(self, root):
        self.beginResetModel()
        self.root = root
        self.gen += 1
        self.endResetModel()

    def kids(self, d):
        if d._kids is None or d._kids[0] != self.gen:
            items = list(d.dirs) + list(d.files) if self.show_files else list(d.dirs)
            c = self.sort_col
            if c == C_NAME:
                key = lambda x: x.name.lower()
            elif c in (C_SIZE, C_PCT):
                key = lambda x: x.size
            elif c == C_FILES:
                key = lambda x: x.nfiles
            elif c == C_DIRS:
                key = lambda x: x.ndirs
            else:
                key = lambda x: x.mtime
            items.sort(key=key, reverse=self.desc)
            for i, x in enumerate(items):
                x._row = i
            d._kids = (self.gen, items)
        return d._kids[1]

    def item(self, idx):
        return idx.internalPointer() if idx.isValid() else None

    def index_for(self, it, col=0):
        if it is None or self.root is None:
            return QModelIndex()
        if it is self.root:
            return self.createIndex(0, col, it)
        if not self.listed(it):
            return QModelIndex()
        return self.createIndex(it._row, col, it)

    def listed(self, it):
        """True if it is shown under the current root (not hidden, recycled, added mid-scan or from an old scan)."""
        while it is not self.root:
            p = it.parent
            if p is None:
                return False
            k = self.kids(p)
            if not (0 <= it._row < len(k) and k[it._row] is it):
                return False
            it = p
        return True

    def index(self, row, col, parent=QModelIndex()):
        if self.root is None:
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(0, col, self.root) if row == 0 else QModelIndex()
        p = parent.internalPointer()
        k = self.kids(p) if p.is_dir else []
        return self.createIndex(row, col, k[row]) if 0 <= row < len(k) else QModelIndex()

    def parent(self, idx):
        if not idx.isValid():
            return QModelIndex()
        it = idx.internalPointer()
        if it is self.root or it.parent is None:
            return QModelIndex()
        return self.index_for(it.parent)

    def rowCount(self, parent=QModelIndex()):
        if self.root is None:
            return 0
        if not parent.isValid():
            return 1
        if parent.column() > 0:
            return 0
        it = parent.internalPointer()
        if not it.is_dir:
            return 0
        return len(self.kids(it))

    def hasChildren(self, parent=QModelIndex()):
        return self.rowCount(parent) > 0

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[s]
        if o == Qt.Horizontal and role == Qt.TextAlignmentRole and s != C_NAME:
            return int(Qt.AlignRight | Qt.AlignVCenter)

    def pct(self, it):
        """Share of the whole drive's capacity."""
        return it.size / self.drive_total if self.drive_total else 0.0

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        it, c = idx.internalPointer(), idx.column()
        if role == Qt.DisplayRole:
            if c == C_NAME:
                return it.path if it is self.root else it.name
            if c == C_SIZE:
                return human(it.size)
            if c == C_PCT:
                return f"{self.pct(it) * 100:.1f} %"
            if c == C_FILES:
                return f"{it.nfiles:,}" if it.is_dir else ""
            if c == C_DIRS:
                return f"{it.ndirs:,}" if it.is_dir else ""
            if c == C_MOD:
                return fdate(it.mtime)
        elif role == Qt.UserRole:
            return self.pct(it)
        elif role == Qt.DecorationRole and c == C_NAME:
            if it is self.root:
                return self.ic_drive if len(it.path) <= 3 else self.ic_dir
            return self.ic_dir if it.is_dir else self.ic_file
        elif role == Qt.TextAlignmentRole and c != C_NAME:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ForegroundRole:
            if it.is_dir and (it.denied or it.skipped):
                return QColor("#ff8a8a")
            if c in (C_FILES, C_DIRS, C_MOD):
                return QColor("#9aa0aa")
        elif role == Qt.ToolTipRole and c == C_NAME:
            tip = it.path
            if it.is_dir and it.denied:
                tip += "\n(access denied: some contents not counted)"
            if it.is_dir and it.skipped:
                tip += "\n(not scanned: the scan was stopped)"
            return tip
        return None

    def sort(self, col, order=Qt.DescendingOrder):
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        keep = [(i.internalPointer(), i.column()) for i in old]
        self.sort_col, self.desc = col, order == Qt.DescendingOrder
        self.gen += 1
        self.changePersistentIndexList(old, [self.index_for(it, c) for it, c in keep])
        self.layoutChanged.emit()


# ---------------------------------------------------------------- list (the other view of a folder, in the Map tab)
LIST_COLS = ["Name", "Size on disk", "% of folder", "Type", "Modified"]
L_NAME, L_SIZE, L_PCT, L_TYPE, L_MOD = range(5)


def kind_of(it):
    if it.is_dir:
        return "Folder"
    e = ext_of(it.name)
    return f"{e.upper()} file" if e else "File"


def folder_total(d, kids=None):
    """What a folder's contents are shares of: its size, or what its contents add up to if that is more. The boxes
    draw their areas by it (see TreeMap._draw) and the list's "% of folder" is by it, so they agree. kids: the
    contents as already read (one snapshot while a scan is adding to them)."""
    if kids is None:
        kids = (*d.dirs, *d.files)
    return max(d.size, sum(k.size for k in kids))


class ListModel(QAbstractTableModel):
    """The direct contents of one folder, folders and files together, in the order the user chose."""

    def __init__(self, icons):
        super().__init__()
        self.dir, self.items, self.total, self.sig = None, [], 0, None
        self.sort_col, self.desc = L_SIZE, True  # kept while the user moves between folders
        self.ic_dir, self.ic_file = icons

    def _order(self, items):
        """Sort in place: by name first, then (stable) by the chosen column, so rows that tie on it stay in name
        order A to Z whichever way the column goes."""
        c = self.sort_col
        items.sort(key=lambda x: x.name.lower())
        if c == L_NAME:
            if self.desc:
                items.reverse()
            return
        key = ((lambda x: x.size) if c in (L_SIZE, L_PCT) else
               (lambda x: kind_of(x).lower()) if c == L_TYPE else (lambda x: x.mtime))
        items.sort(key=key, reverse=self.desc)

    def load(self, d):
        """Show d's contents. Returns False and changes nothing when that is exactly what is shown already (a live
        refresh that found nothing new must not redraw the list)."""
        items = [*d.dirs, *d.files] if d is not None else []
        sig = (id(d), d.size if d is not None else 0,
               tuple((id(x), x.size, x.mtime, x.is_dir and (x.denied, x.skipped)) for x in items))
        if sig == self.sig:
            return False
        self._order(items)
        self.beginResetModel()
        self.dir, self.items, self.sig = d, items, sig
        self.total = folder_total(d, items) if d is not None else 0
        self.endResetModel()
        return True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(LIST_COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return LIST_COLS[s]
        if o == Qt.Horizontal and role == Qt.TextAlignmentRole:  # a header sits over its column's text
            return int((Qt.AlignRight if s in (L_SIZE, L_PCT) else Qt.AlignLeft) | Qt.AlignVCenter)

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid() or not 0 <= idx.row() < len(self.items):
            return None
        it, c = self.items[idx.row()], idx.column()
        if role == Qt.DisplayRole:
            if c == L_NAME:
                return it.name
            if c == L_SIZE:
                return human(it.size)
            if c == L_PCT:
                return f"{it.size / self.total * 100:.1f} %" if self.total else "0.0 %"
            if c == L_TYPE:
                return kind_of(it)
            if c == L_MOD:
                return fdate(it.mtime)
        elif role == Qt.DecorationRole and c == L_NAME:
            return self.ic_dir if it.is_dir else self.ic_file
        elif role == Qt.TextAlignmentRole and c in (L_SIZE, L_PCT):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ForegroundRole:
            if it.is_dir and (it.denied or it.skipped):
                return QColor("#ff8a8a")
            if c in (L_TYPE, L_MOD):
                return QColor("#9aa0aa")
        elif role == Qt.ToolTipRole and c == L_NAME:
            tip = it.path
            if it.is_dir and it.denied:
                tip += "\n(access denied: some contents not counted)"
            if it.is_dir and it.skipped:
                tip += "\n(not scanned: the scan was stopped)"
            return tip
        return None

    def sort(self, col, order=Qt.DescendingOrder):
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        keep = [(self.items[i.row()], i.column()) for i in old]
        self.sort_col, self.desc = col, order == Qt.DescendingOrder
        self._order(self.items)
        pos = {id(x): r for r, x in enumerate(self.items)}
        self.changePersistentIndexList(old, [self.index(pos[id(it)], c) for it, c in keep])
        self.layoutChanged.emit()


class BarDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, color=ACCENT):
        super().__init__(parent)
        self.color = QColor(color)

    def paint(self, qp, opt, idx):
        v = idx.data(Qt.UserRole) or 0
        r = opt.rect.adjusted(3, 4, -3, -4)
        qp.save()
        if opt.state & QStyle.State_Selected:
            qp.fillRect(opt.rect, opt.palette.highlight())
        qp.setPen(Qt.NoPen)
        qp.setBrush(QColor(255, 255, 255, 18))
        qp.drawRoundedRect(r, 2, 2)
        if v > 0:
            w = max(2.0, r.width() * min(v, 1.0))
            qp.setBrush(self.color)
            qp.drawRoundedRect(QRectF(r.x(), r.y(), w, r.height()), 2, 2)
        qp.setPen(QColor("#e6e8ec"))
        qp.drawText(r.adjusted(0, 0, -4, 0), Qt.AlignRight | Qt.AlignVCenter, idx.data(Qt.DisplayRole) or "")
        qp.restore()


# ---------------------------------------------------------------- treemap
def worst(row, side):
    s = sum(row)
    return max(side * side * row[0] / (s * s), (s * s) / (side * side * row[-1]))


def squarify(areas, x, y, w, h):
    """areas sorted descending, sum(areas) == w*h. Returns rects in the same order."""
    out, i, n = [], 0, len(areas)
    while i < n:
        if w < 0.5 or h < 0.5:
            out.extend([(x, y, 0, 0)] * (n - i))
            break
        side = min(w, h)
        row, j = [areas[i]], i + 1
        while j < n and worst(row + [areas[j]], side) <= worst(row, side):
            row.append(areas[j])
            j += 1
        s = sum(row)
        if w >= h:
            cw, yy = s / h, y
            for a in row:
                rh = a / cw
                out.append((x, yy, cw, rh))
                yy += rh
            x, w = x + cw, w - cw
        else:
            rh, xx = s / w, x
            for a in row:
                cw = a / rh
                out.append((xx, y, cw, rh))
                xx += cw
            y, h = y + rh, h - rh
        i = j
    return out


class TreeMap(QWidget):
    picked = Signal(object)      # single click: select item in tree
    zoom = Signal(object)        # double click on a folder / right click: change focus

    MAXD = 6
    HEAD = 15

    def __init__(self):
        super().__init__()
        self.focus = None
        self.sel = None
        self.hits = []
        self.pix = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMinimumSize(200, 150)
        self.font_ = QFont(self.font())
        self.font_.setPointSizeF(8)

    def set_focus(self, d):
        self.focus = d
        self.pix = None
        self.update()

    def set_sel(self, it):
        self.sel = it
        self.update()

    def invalidate(self):
        self.pix = None
        self.update()

    def resizeEvent(self, e):
        self.pix = None

    def paintEvent(self, e):
        qp = QPainter(self)
        if self.pix is None or self.pix.size() != self.size() * self.devicePixelRatioF():
            self.render_map()
        qp.drawPixmap(0, 0, self.pix)
        if self.sel is not None:
            for r, it, _ in self.hits:
                if it is self.sel:
                    qp.setPen(QPen(QColor("#ffffff"), 2))
                    qp.setBrush(Qt.NoBrush)
                    qp.drawRect(r.adjusted(1, 1, -1, -1))
                    break
        if self.focus is None:
            qp.setPen(QColor("#7d8088"))
            qp.drawText(self.rect(), Qt.AlignCenter, "Scan a drive or folder to see the map")

    def render_map(self):
        dpr = self.devicePixelRatioF()
        self.pix = QPixmap(self.size() * dpr)
        self.pix.setDevicePixelRatio(dpr)
        self.pix.fill(QColor("#0d1015"))
        self.hits = []
        if self.focus is None or self.focus.size <= 0:
            return
        qp = QPainter(self.pix)
        qp.setRenderHint(QPainter.Antialiasing, False)
        qp.setFont(self.font_)
        self.fm = QFontMetrics(self.font_)
        self._draw(qp, self.focus, QRectF(0, 0, self.width(), self.height()), 0)
        qp.end()

    def _draw(self, qp, it, r, depth):
        if not (1 <= r.width() <= 20000 and 1 <= r.height() <= 20000):
            return
        if it.is_dir and depth < self.MAXD and r.width() >= 30 and r.height() >= 30 and it.size > 0:
            shade = QColor(30 + depth * 6, 38 + depth * 6, 50 + depth * 7)
            qp.fillRect(r, shade)
            qp.setPen(QColor("#0d1015"))
            qp.drawRect(r.adjusted(0, 0, -1, -1))
            self.hits.append((r, it, depth))
            inner = r.adjusted(2, 2, -2, -2)
            if r.height() > 46 and r.width() > 60:
                qp.setPen(QColor("#c9d3e0"))
                label = f"{it.name if it.parent else it.path}  {human(it.size)}"
                qp.drawText(QRectF(r.x() + 4, r.y() + 1, r.width() - 8, self.HEAD),
                            Qt.AlignLeft | Qt.AlignVCenter, self.fm.elidedText(label, Qt.ElideRight, int(r.width() - 8)))
                inner = r.adjusted(2, self.HEAD + 1, -2, -2)
            kids = sorted((k for k in (*list(it.dirs), *list(it.files)) if k.size > 0), key=lambda k: k.size, reverse=True)
            total = folder_total(it)  # (the list's "% of folder" is by the same number)
            area = inner.width() * inner.height()
            if area <= 0 or not kids or total <= 0:
                return
            scale = area / total
            areas = []
            for k in kids:
                a = k.size * scale
                if a < 4:
                    break
                areas.append(a)
            if not areas:
                return
            # remaining tiny items are left as background inside the folder
            s = sum(areas)
            fit = s / area
            if fit < 1:
                # shrink the box so the visible items fill it proportionally, leftovers stay as a strip
                if inner.width() >= inner.height():
                    inner = QRectF(inner.x(), inner.y(), inner.width() * fit, inner.height())
                else:
                    inner = QRectF(inner.x(), inner.y(), inner.width(), inner.height() * fit)
            for k, (x, y, w, h) in zip(kids, squarify(areas, inner.x(), inner.y(), inner.width(), inner.height())):
                self._draw(qp, k, QRectF(x, y, w, h), depth + 1)
            return
        # leaf
        if it.is_dir:
            col = QColor("#3a4a5e")
        else:
            col = QColor(cat_of(it.name)[1])
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, col.lighter(125))
        g.setColorAt(1, col.darker(160))
        qp.fillRect(r, QBrush(g))
        if r.width() > 3 and r.height() > 3:
            qp.setPen(QColor(0, 0, 0, 120))
            qp.drawRect(r.adjusted(0, 0, -1, -1))
        self.hits.append((r, it, depth))
        if r.width() > 48 and r.height() > 15:
            lum = 0.299 * col.red() + 0.587 * col.green() + 0.114 * col.blue()
            qp.setPen(QColor("#0b0d10") if lum > 140 else QColor("#f2f4f7"))
            t = it.name + (f"\n{human(it.size)}" if r.height() > 30 else "")
            lines = t.split("\n")
            el = "\n".join(self.fm.elidedText(l, Qt.ElideRight, int(r.width() - 6)) for l in lines)
            qp.drawText(r.adjusted(3, 2, -3, -2), Qt.AlignLeft | Qt.AlignTop, el)

    def hit(self, pos):
        for r, it, _ in reversed(self.hits):
            if r.contains(QPointF(pos)):
                return it
        return None

    def mouseMoveEvent(self, e):
        it = self.hit(e.position())
        if it is None:
            self.setToolTip("")
            return
        extra = f"\n{it.nfiles:,} files" if it.is_dir else f"\n{cat_of(it.name)[0]}"
        self.setToolTip(f"{it.path}\n{human(it.size)} on disk{extra}")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            it = self.hit(e.position())
            if it is not None:
                self.picked.emit(it)
        elif e.button() == Qt.RightButton and self.focus is not None and self.focus.parent is not None:
            self.zoom.emit(self.focus.parent)

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.LeftButton:
            return self.mousePressEvent(e)
        it = self.hit(e.position())
        if it is None:
            return
        d = it if it.is_dir else it.parent
        # zoom one level below the current focus along the clicked path
        while d is not None and d.parent is not None and d.parent is not self.focus and d is not self.focus:
            d = d.parent
        if d is not None and d is not self.focus:
            self.zoom.emit(d)


# ---------------------------------------------------------------- window
class Window(QMainWindow):
    def __init__(self, start=None):
        super().__init__()
        self.setWindowTitle(f"{APP} {VERSION}")
        self.settings = QSettings(APP, APP)
        self.scanner = None
        self.focus = None
        self.map_pick = False

        # top bar
        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(8, 6, 8, 6)
        self.drives = QComboBox()
        self.drives.setMinimumWidth(240)
        self.fill_drives()
        self.drives.activated.connect(self.drive_picked)
        self.drives.installEventFilter(self)  # no scan from a stray wheel notch or arrow key
        self.path = QLineEdit()
        self.path.setPlaceholderText("Folder or drive to scan")
        self.path.returnPressed.connect(self.scan)
        browse = QToolButton()
        browse.setText("…")
        browse.setToolTip("Pick a folder")
        browse.clicked.connect(self.browse)
        self.btn_scan = QPushButton("Scan")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.clicked.connect(self.scan_or_stop)
        self.btn_files = QToolButton()
        self.btn_files.setText("Files in tree")
        self.btn_files.setCheckable(True)
        self.btn_files.setChecked(True)
        self.btn_files.toggled.connect(self.toggle_files)
        tl.addWidget(self.drives)
        tl.addWidget(self.path, 1)
        tl.addWidget(browse)
        tl.addWidget(self.btn_scan)
        tl.addWidget(self.btn_files)

        # drive usage
        self.usage = QProgressBar()
        self.usage.setTextVisible(True)
        self.usage.setMaximumHeight(16)
        self.usage.hide()

        # tree
        self.model = TreeModel()
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(True)
        self.tree.header().setSortIndicator(C_SIZE, Qt.DescendingOrder)
        self.tree.header().setSectionsMovable(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(C_NAME, QHeaderView.Stretch)
        self.size_col = self.tree.header().fontMetrics().horizontalAdvance(COLS[C_SIZE]) + 44  # (room for the arrow)
        self.mod_col = self.tree.fontMetrics().horizontalAdvance(fdate(1700000000)) + 26  # (a whole date and time)
        for c, w in ((C_SIZE, self.size_col), (C_PCT, 120), (C_FILES, 75), (C_DIRS, 65), (C_MOD, self.mod_col)):
            self.tree.setColumnWidth(c, w)
        self.tree.setItemDelegateForColumn(C_PCT, BarDelegate(self.tree))
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(lambda p: self.menu(self.tree.viewport().mapToGlobal(p),
                                                                          self.selected_items()))
        self.tree.doubleClicked.connect(self.tree_double)
        self.tree.selectionModel().currentChanged.connect(self.current_changed)

        # right tabs
        self.tabs = QTabWidget()
        mapw = QWidget()
        ml = QVBoxLayout(mapw)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(4)
        crumb = QHBoxLayout()
        crumb.setContentsMargins(6, 4, 6, 0)
        self.btn_up = QToolButton()
        self.btn_up.setText("↑ Up")
        self.btn_up.clicked.connect(self.map_up)
        self.crumb = QLabel("")
        self.crumb.setObjectName("crumb")
        self.crumb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.crumb.installEventFilter(self)  # re-elide whenever the crumb itself is resized
        crumb.addWidget(self.btn_up)
        crumb.addWidget(self.crumb, 1)
        # Boxes | List: two views of the selected folder
        self.view_boxes = QToolButton()
        self.view_boxes.setText("Boxes")
        self.view_list = QToolButton()
        self.view_list.setText("List")
        grp = QButtonGroup(self)  # (makes the two buttons exclusive)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        for b in (self.view_boxes, self.view_list):
            b.setCheckable(True)
            b.setAutoRaise(False)
            grp.addButton(b)
            seg.addWidget(b)
        self.view_boxes.setChecked(True)
        crumb.addLayout(seg)
        ml.addLayout(crumb)
        self.view_stack = QStackedWidget()
        boxes = QWidget()
        bl = QVBoxLayout(boxes)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        self.map = TreeMap()
        self.map.picked.connect(self.map_picked)
        self.map.zoom.connect(self.set_focus)
        bl.addWidget(self.map, 1)
        legend = QHBoxLayout()
        legend.setContentsMargins(6, 0, 6, 4)
        for name, col, _ in CATS + [(OTHER[0], OTHER[1], "")]:
            lab = QLabel(f"<span style='color:{col}'>■</span> {name}")
            legend.addWidget(lab)
        legend.addStretch(1)
        bl.addLayout(legend)
        self.view_stack.addWidget(boxes)
        self.list_model = ListModel((self.model.ic_dir, self.model.ic_file))
        self.listv = QTableView()
        self.listv.setModel(self.list_model)
        self.listv.verticalHeader().hide()
        self.listv.verticalHeader().setDefaultSectionSize(22)
        self.listv.setShowGrid(False)
        self.listv.setAlternatingRowColors(True)
        self.listv.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.listv.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.listv.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.listv.setWordWrap(False)
        lh = self.listv.horizontalHeader()
        lh.setSortIndicator(L_SIZE, Qt.DescendingOrder)
        self.listv.setSortingEnabled(True)
        lh.setStretchLastSection(False)
        lh.setSectionResizeMode(L_NAME, QHeaderView.Stretch)
        for c, w in ((L_SIZE, self.size_col), (L_PCT, 95), (L_TYPE, 80),
                     (L_MOD, self.mod_col)):
            self.listv.setColumnWidth(c, w)
        self.listv.setContextMenuPolicy(Qt.CustomContextMenu)
        self.listv.customContextMenuRequested.connect(
            lambda p: self.menu(self.listv.viewport().mapToGlobal(p), self.list_items()))
        self.listv.activated.connect(self.list_activated)  # double-click and Enter
        self.view_stack.addWidget(self.listv)
        ml.addWidget(self.view_stack, 1)
        self.view_list.toggled.connect(self.view_changed)
        self.tabs.addTab(mapw, "Map")

        self.types = QTableWidget(0, 5)
        self.types.setHorizontalHeaderLabels(["Type", "Extension", "Size on disk", "% of drive", "Files"])
        self.setup_table(self.types)
        self.types.setItemDelegateForColumn(3, BarDelegate(self.types))
        self.tabs.addTab(self.types, "Types")

        self.top = QTableWidget(0, 4)
        self.top.setHorizontalHeaderLabels(["Name", "Size on disk", "Folder", "Modified"])
        self.setup_table(self.top)
        self.top.setContextMenuPolicy(Qt.CustomContextMenu)
        self.top.customContextMenuRequested.connect(self.top_menu)
        self.top.itemDoubleClicked.connect(lambda i: self.reveal(self.top.item(i.row(), 0).data(Qt.UserRole)))
        self.tabs.addTab(self.top, "Top 100 files")
        self.tabs.currentChanged.connect(lambda _: self.refresh_tables())

        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self.tree)
        self.split.addWidget(self.tabs)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)

        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(top)
        u = QWidget()
        ul = QHBoxLayout(u)
        ul.setContentsMargins(8, 0, 8, 6)
        ul.addWidget(self.usage)
        cl.addWidget(u)
        cl.addWidget(self.split, 1)
        self.setCentralWidget(central)
        self.status = QLabel("Pick a drive or folder and hit Scan.")
        self.statusBar().addWidget(self.status, 1)

        disp = QAbstractEventDispatcher.instance()
        disp.awake.connect(self.gui_busy)
        disp.aboutToBlock.connect(self.gui_idle)
        self.tick = QTimer(self, interval=150, timeout=self.progress)
        self.live = QTimer(self, interval=2000, timeout=self.live_refresh)
        self.sel_timer = QTimer(self, singleShot=True, interval=120, timeout=self.apply_selection)

        QShortcut(QKeySequence.Delete, self.tree, lambda: self.trash(self.selected_items())).setContext(
            Qt.WidgetWithChildrenShortcut)
        QShortcut(QKeySequence.Delete, self.listv, lambda: self.trash(self.list_items())).setContext(
            Qt.WidgetWithChildrenShortcut)
        self.tree.installEventFilter(self)  # Ctrl+C: see eventFilter
        self.listv.installEventFilter(self)
        QShortcut(QKeySequence("F5"), self, self.scan)
        QShortcut(QKeySequence("Ctrl+L"), self, lambda: (self.path.setFocus(), self.path.selectAll()))
        QShortcut(QKeySequence("Backspace"), self.map, self.map_up).setContext(Qt.WidgetShortcut)

        # saved layout: anything that isn't a valid Qt state (damaged or edited settings) falls back to defaults
        def state(key):
            v = self.settings.value(key)
            return v if isinstance(v, (QByteArray, bytes)) else None
        if not (state("geometry") and self.restoreGeometry(state("geometry"))):
            ag = self.screen().availableGeometry()
            self.resize(min(1500, ag.width() - 40), min(900, ag.height() - 60))
        if not (state("split") and self.split.restoreState(state("split"))):
            self.split.setSizes([int(self.width() * 0.6), int(self.width() * 0.4)])
        if state("header2"):
            self.tree.header().restoreState(state("header2"))
            # a state saved by an older version has the Size column at its old, narrower width: never narrower
            # than the "Size on disk" header needs
            hd = self.tree.header()
            hd.resizeSection(C_SIZE, max(hd.sectionSize(C_SIZE), self.size_col))
            hd.resizeSection(C_MOD, max(hd.sectionSize(C_MOD), self.mod_col))  # (and a whole date and time)
        if self.settings.value("view") == "list":  # the view chosen last time
            self.view_list.setChecked(True)

        if start:
            self.path.setText(start)
            QTimer.singleShot(0, self.scan)
        else:
            last = self.settings.value("last_path")
            self.path.setText(last if isinstance(last, str) and last else "C:\\")

    # ---------- helpers
    def setup_table(self, t):
        t.verticalHeader().hide()
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setShowGrid(False)
        t.setAlternatingRowColors(True)
        t.horizontalHeader().setStretchLastSection(True)
        t.verticalHeader().setDefaultSectionSize(22)

    def eventFilter(self, obj, e):
        if obj is self.drives and (e.type() == QEvent.Wheel or (
                e.type() == QEvent.KeyPress and e.text().isprintable() and e.text()  # type-to-search
                and e.key() != Qt.Key_Space) or (  # Space opens the list
                e.type() == QEvent.KeyPress and e.key() in (
                Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End)
                and not (e.key() == Qt.Key_Down and e.modifiers() & Qt.AltModifier))):  # Alt+Down opens the list
            return True
        if obj is getattr(self, "tree", None) and e.type() == QEvent.KeyPress and e.matches(QKeySequence.Copy):
            self.copy_paths(self.selected_items())  # full paths; never Qt's own copy of just the current row's name
            return True
        if obj is getattr(self, "listv", None) and e.type() == QEvent.KeyPress and e.matches(QKeySequence.Copy):
            self.copy_paths(self.list_items())
            return True
        if obj is getattr(self, "crumb", None) and e.type() == QEvent.Resize:
            self.update_crumb()
        return super().eventFilter(obj, e)

    def fill_drives(self):
        self.drives.clear()
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        for i in range(26):
            if not mask >> i & 1:
                continue
            root = f"{chr(65 + i)}:\\"
            kind = k32.GetDriveTypeW(root)
            if kind < 2:
                continue
            if kind == 4:  # DRIVE_REMOTE: don't ask for sizes on the GUI thread, an offline share hangs it
                self.drives.addItem(f"{root}  network drive", root)
                continue
            v = QStorageInfo(root)
            if not v.isValid() or not v.isReady():
                # a subst drive (it reports as fixed): Qt has no sizes for it, but it scans. Only fixed drives
                # are probed: an empty card reader or DVD drive could pop up "There is no disk in the drive".
                if kind == 3 and os.path.isdir(root):
                    self.drives.addItem(root, root)
                continue
            used = v.bytesTotal() - v.bytesAvailable()
            name = v.displayName() if v.displayName() != v.rootPath() else ""
            self.drives.addItem(f"{root}  {name}  {human(used)} / {human(v.bytesTotal())}".replace("    ", "  "), root)

    def busy(self):
        if self.scanner and not self.scanner.t1:
            self.status.setText("Stop the current scan first.")
            return True
        return False

    def drive_picked(self, i):
        if self.busy():
            self.sync_drive(self.scanner.root.path)  # keep showing the drive being scanned
            return
        self.path.setText(self.drives.itemData(i))
        self.scan()

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to scan", self.path.text() or "C:\\")
        if d and not self.busy():
            self.path.setText(os.path.normpath(d))
            self.scan()

    def selected_items(self):
        seen, out = set(), []
        for i in self.tree.selectionModel().selectedRows(0):
            it = i.internalPointer()
            if id(it) not in seen:
                seen.add(id(it))
                out.append(it)
        return out

    # ---------- scanning
    def scan_or_stop(self):
        if self.scanner and not self.scanner.t1:
            self.scanner.stop()
            self.btn_scan.setEnabled(False)
            self.status.setText("Stopping…")
        else:
            self.scan()

    def scan(self):
        if self.scanner and not self.scanner.t1:
            return
        p = self.path.text().strip().strip('"')
        if len(p) == 2 and p[1] == ":":
            p += "\\"
        remote = p.startswith("\\\\") or (p[1:2] == ":" and ctypes.windll.kernel32.GetDriveTypeW(p[:2] + "\\") == 4)
        found = []  # a network path is probed off the GUI thread: an offline share would freeze the window
        probe = threading.Thread(target=lambda: found.append((os.path.isdir(p), QStorageInfo(p))), daemon=True)
        probe.start()
        probe.join(3 if remote else None)
        if not found:
            self.status.setText(f"Network drive not responding: {p}")
            return
        isdir, info = found[0]
        if not isdir:
            self.status.setText(f"Not a folder: {p}")
            return
        p = os.path.abspath(p)
        self.path.setText(p)
        self.settings.setValue("last_path", p)
        self.scanner = Scanner(p)
        self.scanner.finished.connect(self.scan_done, Qt.QueuedConnection)
        t = info.bytesTotal()
        self.model.drive_total = t if t > 0 else 0  # -1 on UNC paths
        for tbl in (self.types, self.top):
            tbl.clearSelection()
            tbl.setRowCount(0)
        self.map.sel, self.map.hits = None, []
        self.list_model.load(None)  # no rows of the old scan while the new one starts
        self.model.set_root(self.scanner.root)
        self.tree.expand(self.model.index_for(self.scanner.root))
        self.set_focus(self.scanner.root)
        self.btn_scan.setText("Stop")
        self.btn_scan.setEnabled(True)
        self.show_usage(info)
        self.sync_drive(p)
        self.scanner.start()
        self.tick.start()
        self.live.start()

    def sync_drive(self, p):
        self.drives.setCurrentIndex(-1)  # no listed drive (UNC path, folder mount): show none, not a wrong one
        for i in range(self.drives.count()):
            if p.upper().startswith(self.drives.itemData(i).upper()):
                self.drives.setCurrentIndex(i)

    def show_usage(self, v):
        if not v.isValid() or v.bytesTotal() <= 0:
            self.usage.hide()
            return
        used = v.bytesTotal() - v.bytesAvailable()
        self.usage.setRange(0, 1000)
        self.usage.setValue(int(used / v.bytesTotal() * 1000))
        self.usage.setFormat(f"Drive {v.rootPath().replace('/', chr(92))}  {human(used)} used of "
                             f"{human(v.bytesTotal())}   ·   {human(v.bytesAvailable())} free")
        self.usage.show()

    def progress(self):
        s = self.scanner
        if not s or s.t1 or s.stopped:
            return
        el = time.time() - s.t0
        cur = s.current
        if len(cur) > 90:
            cur = cur[:40] + "…" + cur[-45:]
        self.status.setText(f"Scanning…  {s.files:,} files  ·  {s.dirs:,} folders  ·  {human(s.bytes)} on disk  ·  "
                            f"{el:.0f}s   {cur}")

    def snapshot_view(self):
        exp, stack = [], [self.model.root] if self.model.root else []
        while stack:
            d = stack.pop()
            idx = self.model.index_for(d)
            if self.tree.isExpanded(idx):
                exp.append(d)
                stack.extend(d.dirs)
        cur = self.tree.currentIndex()
        sel = [i.internalPointer() for i in self.tree.selectionModel().selectedRows(0)]
        return exp, (cur.internalPointer() if cur.isValid() else None), self.tree.verticalScrollBar().value(), sel

    def restore_view(self, snap):
        exp, cur, scroll, sel = snap
        for d in exp:
            self.tree.setExpanded(self.model.index_for(d), True)
        if cur is not None:
            # current row only: selection comes back from sel, so a deselected row stays deselected
            self.tree.selectionModel().setCurrentIndex(self.model.index_for(cur), QItemSelectionModel.NoUpdate)
        sm = self.tree.selectionModel()
        for it in sel:
            idx = self.model.index_for(it)
            if idx.isValid():
                sm.select(idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self.tree.verticalScrollBar().setValue(scroll)

    def rebuild(self):
        """Recompute totals and reset the model, keeping expanded folders, current row and scroll."""
        snap = self.snapshot_view()
        self.map_pick = True
        retotal(self.model.root)
        self.model.set_root(self.model.root)
        self.restore_view(snap)
        self.map_pick = False
        self.map.invalidate()
        self.update_crumb()
        self.refresh_list()

    def live_refresh(self):
        if self.scanner and not self.scanner.t1:
            self.rebuild()

    # The scanner threads share Python's GIL with the window: while it scans a big drive, every redraw, click
    # or scroll had to fight 16 threads for it and the window froze for seconds. The window now has priority:
    # the threads pause whenever its event loop wakes up with work, and carry on as soon as it goes idle.
    def gui_busy(self):
        if self.scanner:
            self.scanner.go.clear()

    def gui_idle(self):
        if self.scanner:
            self.scanner.go.set()

    def scan_done(self, s):
        if s is not self.scanner:
            return  # a newer scan already started
        self.tick.stop()
        self.live.stop()
        gc.collect()
        self.rebuild()
        if self.model.root and not self.tree.isExpanded(self.model.index_for(self.model.root)):
            self.tree.expand(self.model.index_for(self.model.root))
        self.btn_scan.setText("Scan")
        self.btn_scan.setEnabled(True)
        r = s.root
        msg = (f"{'Stopped' if s.stopped else 'Scanned'} {r.path}  ·  {r.nfiles:,} files  ·  {r.ndirs:,} folders  ·  "
               f"{human(r.size)} on disk  ·  {s.t1 - s.t0:.1f}s")
        if s.denied:
            msg += f"  ·  {s.denied:,} folders access denied (run as admin to count them)"
        self.status.setText(msg)
        self.refresh_tables()
        self.fill_drives()
        self.sync_drive(s.root.path)

    # ---------- focus / selection
    def current_changed(self, cur, prev):
        if self.map_pick:
            return
        self.sel_timer.start()

    def apply_selection(self):
        cur = self.tree.currentIndex()
        if not cur.isValid():
            return
        it = cur.internalPointer()
        d = it if it.is_dir else it.parent
        if d is not self.focus:
            self.set_focus(d)
        self.map.set_sel(None if it is d else it)

    def set_focus(self, d):
        self.focus = d
        self.map.set_focus(d)
        self.update_crumb()
        self.refresh_list()
        self.refresh_tables()

    # ---------- the list view of the selected folder (the other half of Boxes | List)
    def view_changed(self, on):
        self.view_stack.setCurrentIndex(1 if on else 0)
        self.settings.setValue("view", "list" if on else "boxes")
        self.refresh_list()

    def list_items(self):
        m = self.list_model
        return [m.items[i.row()] for i in self.listv.selectionModel().selectedRows(0) if i.row() < len(m.items)]

    def list_activated(self, idx):
        if not 0 <= idx.row() < len(self.list_model.items):
            return
        it = self.list_model.items[idx.row()]
        if it.is_dir:  # opens in the list, and the left tree follows
            self.select_in_tree(it)
            self.set_focus(it)
        else:
            self.open_item(it)

    def refresh_list(self):
        """Fill the list with the focused folder. A refresh that finds nothing new leaves it alone (no redraw), and
        one that does keeps the selection, the current row and the scroll position."""
        if not self.view_list.isChecked():
            return
        lv, m = self.listv, self.list_model
        d = self.focus
        same = m.dir is d
        keep, cur = [], None
        if same:
            keep = self.list_items()
            ci = lv.currentIndex()
            cur = m.items[ci.row()] if ci.isValid() and ci.row() < len(m.items) else None
        scroll = lv.verticalScrollBar().value()
        if not m.load(d):
            return
        if not same:
            lv.scrollToTop()
            return
        pos = {id(x): r for r, x in enumerate(m.items)}
        sm = lv.selectionModel()
        for it in keep:
            if id(it) in pos:
                sm.select(m.index(pos[id(it)], 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
        if cur is not None and id(cur) in pos:
            sm.setCurrentIndex(m.index(pos[id(cur)], 0), QItemSelectionModel.NoUpdate)
        lv.verticalScrollBar().setValue(scroll)

    def update_crumb(self):
        d = self.focus
        if d is None:
            self.crumb.setText("")
            return
        rest = f"  ·  {human(d.size)} on disk  ·  {d.nfiles:,} files"
        bold = QFont(self.crumb.font())
        bold.setBold(True)
        room = max(120, self.crumb.width() - self.crumb.fontMetrics().horizontalAdvance(rest) - 12)
        path = QFontMetrics(bold).elidedText(d.path, Qt.ElideMiddle, room)
        self.crumb.setText(f"<b>{html.escape(path)}</b>{rest}")
        self.crumb.setToolTip(d.path)
        self.btn_up.setEnabled(d.parent is not None)

    def map_up(self):
        if self.focus is not None and self.focus.parent is not None:
            self.select_in_tree(self.focus.parent)
            self.set_focus(self.focus.parent)

    def select_in_tree(self, it):
        p = it.parent
        chain = []
        while p is not None:
            chain.append(p)
            p = p.parent
        for d in reversed(chain):
            self.tree.setExpanded(self.model.index_for(d), True)
        idx = self.model.index_for(it)
        self.map_pick = True
        if not idx.isValid():
            self.tree.clearSelection()  # not shown in the tree (Files off): don't leave an old row selected
        self.tree.setCurrentIndex(idx)
        self.map_pick = False
        self.tree.scrollTo(idx)

    def map_picked(self, it):
        self.select_in_tree(it)
        self.map.set_sel(it)

    def tree_double(self, idx):
        it = idx.internalPointer()
        if not it.is_dir:
            self.open_item(it)

    def open_item(self, it):
        if self.usable(it):
            try:
                os.startfile(it.path)
            except OSError as e:  # deleted or moved since the scan, no app for this type, ...
                self.status.setText(f"Can't open {it.name!r}: {e.strerror or e}")

    def usable(self, it):
        if self._safe_path(it.path):
            return True
        self.status.setText(f"Can't open {it.name!r}: Windows would act on a different file or device. "
                            "Use Explorer for this one.")
        return False

    def toggle_files(self, on):
        snap = self.snapshot_view()
        self.model.show_files = on
        self.model.set_root(self.model.root)
        if snap[1] is not None and not snap[1].is_dir and not on:
            snap = (snap[0], snap[1].parent, snap[2], snap[3])
        self.restore_view(snap)

    # ---------- tables
    def refresh_tables(self):
        d = self.focus
        tab = self.tabs.currentIndex()
        if d is None or tab == 0:
            return
        if self.scanner and not self.scanner.t1:
            return  # tables fill when the scan finishes
        if tab == 1:
            agg = {}
            for f in walk_files(d):
                e = ext_of(f.name)
                a = agg.get(e)
                if a is None:
                    agg[e] = [f.size, 1]
                else:
                    a[0] += f.size
                    a[1] += 1
            rows = sorted(agg.items(), key=lambda kv: kv[1][0], reverse=True)[:500]
            t = self.types
            t.clearSelection()
            t.setSortingEnabled(False)
            t.setRowCount(len(rows))
            for r, (e, (sz, n)) in enumerate(rows):
                cname, col = EXT_CAT.get(e, OTHER)
                it0 = QTableWidgetItem(f"■ {cname}")
                it0.setForeground(QColor(col))
                pct = sz / self.model.drive_total if self.model.drive_total else 0
                vals = [it0, QTableWidgetItem(f".{e}" if e else "(no extension)"), SortItem(human(sz), sz),
                        SortItem(f"{pct * 100:.1f} %", pct), SortItem(f"{n:,}", n)]
                vals[3].setData(Qt.UserRole, pct)
                for c, v in enumerate(vals):
                    if c >= 2:
                        v.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    t.setItem(r, c, v)
            t.setSortingEnabled(True)
            t.sortItems(2, Qt.DescendingOrder)
            t.resizeColumnsToContents()
        elif tab == 2:
            big = heapq.nlargest(100, walk_files(d), key=lambda f: f.size)
            t = self.top
            t.clearSelection()
            t.setSortingEnabled(False)
            t.setRowCount(len(big))
            for r, f in enumerate(big):
                n = QTableWidgetItem(f.name)
                n.setData(Qt.UserRole, f)
                n.setForeground(QColor(cat_of(f.name)[1]).lighter(115))
                s = SortItem(human(f.size), f.size)
                s.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                for c, v in enumerate([n, s, QTableWidgetItem(f.parent.path), SortItem(fdate(f.mtime), f.mtime)]):
                    t.setItem(r, c, v)
            t.setSortingEnabled(True)
            t.sortItems(1, Qt.DescendingOrder)
            t.resizeColumnsToContents()
            t.setColumnWidth(2, min(t.columnWidth(2), 420))

    def top_menu(self, pos):
        rows = sorted({i.row() for i in self.top.selectedIndexes()})
        items = [self.top.item(r, 0).data(Qt.UserRole) for r in rows]
        if items:
            self.menu(self.top.viewport().mapToGlobal(pos), items)

    # ---------- actions
    def menu(self, gpos, items):
        if not items:
            return
        m = QMenu(self)
        one = items[0]
        if len(items) == 1:
            m.addAction("Open", lambda: self.open_item(one))
            m.addAction("Show in Explorer", lambda: self.reveal(one))
            if one.is_dir:
                m.addAction("Show in map", lambda: (self.select_in_tree(one), self.set_focus(one),
                                                       self.tabs.setCurrentIndex(0)))
                m.addAction("Scan only this folder", lambda: self.busy() or (self.path.setText(one.path), self.scan()))
        m.addAction("Copy path" + ("s" if len(items) > 1 else ""), lambda: self.copy_paths(items))
        m.addSeparator()
        total = self.freed(items)
        m.addAction(f"Send to Recycle Bin ({human(total)})", lambda: self.trash(items))
        m.exec(gpos)
        m.deleteLater()

    def reveal(self, it):
        if self.usable(it):
            subprocess.Popen(f'explorer /select,"{it.path}"')

    def copy_paths(self, items):
        if not items:
            return
        QGuiApplication.clipboard().setText("\n".join(i.path for i in items))
        self.status.setText(f"Copied {len(items)} path{'s' if len(items) > 1 else ''}")

    def trash(self, items):
        items = [i for i in items if i.parent is not None]
        # a folder and something inside it: recycle only the folder (no double count, no stale child)
        ids = {id(i) for i in items}
        items = [i for i in items if not self._under(i, ids)]
        if not items:
            return
        if self.scanner and not self.scanner.t1:
            self.status.setText("Wait for the scan to finish before deleting.")
            return
        bad, batch = [], {}  # batch: what the selection adds to each drive's Recycle Bin
        for i in items:
            if not self._safe_path(i.path):
                bad.append((i, "Windows would act on a different file or device"))
            elif self._has_skipped(i):  # its real size is unknown, so the Recycle Bin size check can't be made
                bad.append((i, "its size isn't known because the scan was stopped; rescan it first"))
            elif why := recycle_refusal(i.path, i.lsize, batch):  # by length: that is what Windows compares
                bad.append((i, why))
        if bad:
            refused = {id(b) for b, _ in bad}
            items = [i for i in items if id(i) not in refused]
            many = f"{len(bad)} items (first {bad[0][0].name!r})" if len(bad) > 1 else repr(bad[0][0].name)
            self.status.setText(f"Can't recycle {many}: {bad[0][1]}. Use Explorer for these.")
            if not items:
                return
        total = self.freed(items)
        names = "\n".join(i.path for i in items[:8]) + ("\n…" if len(items) > 8 else "")
        skipped = f"\n\n{len(bad)} other item{'s' if len(bad) > 1 else ''} can't be recycled: see the status bar."
        if QMessageBox.question(self, "Send to Recycle Bin",
                                f"Send {len(items)} item{'s' if len(items) > 1 else ''} ({human(total)}) "
                                f"to the Recycle Bin?\n\n{names}{skipped if bad else ''}") != QMessageBox.Yes:
            return
        failed = []
        for it in items:
            files = self._files_under(it) if self.scanner and self.scanner.groups else []  # (before it leaves the tree)
            r = QFile.moveToTrash(it.path)
            ok = r[0] if isinstance(r, tuple) else bool(r)
            if not ok:
                failed.append(it.path)
                continue
            self._release_links(files)
            p = it.parent
            if it.is_dir:
                p.dirs.remove(it)
            else:
                p.files.remove(it)
                p.own -= it.size
                p.lown -= it.lsize
            if self.focus is it or self._inside(self.focus, it):
                self.focus = p
        cur = self.tree.currentIndex()
        if cur.isValid() and any(self._inside(cur.internalPointer(), i) for i in items):
            self.tree.setCurrentIndex(self.model.index_for(items[0].parent))
        if self.map.sel is not None and any(self._inside(self.map.sel, i) for i in items):
            self.map.sel = None
        self.rebuild()
        self.set_focus(self.focus)
        self.status.setText(f"Sent {len(items) - len(failed)} to the Recycle Bin" +
                            (f"  ·  couldn't move: {', '.join(failed[:3])}" if failed else "") +
                            (f"  ·  skipped {len(bad)} (first {bad[0][0].name!r}; use Explorer)" if bad else ""))

    @staticmethod
    def _files_under(it):
        return list(walk_files(it)) if it.is_dir else [it]

    @staticmethod
    def _release_links(files):
        """Names recycled that were hard links: the one that held the size hands it to the next name still there
        (by path), because that data stays on disk and has to stay in the totals."""
        for f in files:
            g = f.link
            if g is None:
                continue
            g.remove(f)
            f.link = None
            if f.size and g:
                g[0].size += f.size
                g[0].parent.own += f.size

    def freed(self, items):
        """What recycling items gives back: their size on disk, less hard-linked data that stays because another
        name for it isn't among them."""
        total = sum(i.size for i in items)
        ids = {id(i) for i in items}

        def inside(f):
            while f is not None:
                if id(f) in ids:
                    return True
                f = f.parent
            return False
        for g in (self.scanner.groups if self.scanner else ()):
            owner = next((f for f in g if f.size), None)
            if owner is not None and inside(owner) and not all(inside(f) for f in g):
                total -= owner.size
        return total

    DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
               *(f"{p}{n}" for p in ("COM", "LPT") for n in "0123456789¹²³")}

    @classmethod
    def _safe_path(cls, path):
        """False if any folder or name in the path is one Windows silently rewrites (trailing '.' or ' ',
        unpaired surrogates: open / recycle would act on a sibling) or reads as a device (CON, NUL.txt, COM1)."""
        for name in path.split("\\")[1:]:
            if name.endswith((".", " ")) or name.split(".")[0].rstrip(" ").upper() in cls.DEVICES:
                return False
            try:
                name.encode("utf-16-le")
            except UnicodeEncodeError:
                return False
        return True

    @staticmethod
    def _under(it, ids):
        p = it.parent
        while p is not None:
            if id(p) in ids:
                return True
            p = p.parent
        return False

    @staticmethod
    def _has_skipped(it):
        stack = [it] if it.is_dir else []
        while stack:
            d = stack.pop()
            if d.skipped:
                return True
            stack.extend(d.dirs)
        return False

    @staticmethod
    def _inside(it, anc):
        while it is not None:
            if it is anc:
                return True
            it = it.parent
        return False

    def closeEvent(self, e):
        if self.scanner:
            self.scanner.stop()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("split", self.split.saveState())
        self.settings.setValue("header2", self.tree.header().saveState())
        super().closeEvent(e)


class SortItem(QTableWidgetItem):
    def __init__(self, text, key):
        super().__init__(text)
        self.key = key

    def __lt__(self, other):
        return self.key < getattr(other, "key", 0)


def foot_icon(size=256):
    """App icon: a big blue footprint on a dark tile."""
    from PySide6.QtGui import QIcon, QImage, QPainterPath
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    qp = QPainter(img)
    qp.setRenderHint(QPainter.Antialiasing)
    s = size / 256
    qp.setPen(Qt.NoPen)
    qp.setBrush(QColor("#13161c"))
    qp.drawRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 52 * s, 52 * s)
    g = QLinearGradient(0, 0, 0, size)
    g.setColorAt(0, QColor("#7cc4ff"))
    g.setColorAt(1, QColor(ACCENT))
    qp.setBrush(QBrush(g))
    ball = QPainterPath()
    ball.addEllipse(QRectF(74 * s, 104 * s, 116 * s, 100 * s))
    heel = QPainterPath()
    heel.addEllipse(QRectF(88 * s, 160 * s, 80 * s, 80 * s))
    arch = QPainterPath()
    arch.addRect(QRectF(92 * s, 150 * s, 80 * s, 40 * s))
    qp.drawPath(ball.united(heel).united(arch))
    for cx, cy, r in ((96, 76, 22), (134, 56, 16), (163, 58, 13.5), (186, 72, 11.5), (204, 94, 9.5)):
        qp.drawEllipse(QPointF(cx * s, cy * s), r * s, r * 1.15 * s)
    qp.end()
    return QIcon(QPixmap.fromImage(img)), img


# ---------------------------------------------------------------- theme + main
def apply_theme(app):
    suite_style.apply(app, suite_style.BLUE)


def install_error_log():
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP)
    log = os.path.join(d, "error.log")

    def hook(t, v, tb):
        try:  # the folder is made on the first error; if it can't be, the error just isn't logged
            os.makedirs(d, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "".join(traceback.format_exception(t, v, tb)) + "\n")
        except OSError:
            pass
        sys.__excepthook__(t, v, tb)
    sys.excepthook = hook


def main():
    install_error_log()
    # the uninstaller checks this mutex and refuses to remove files while Bigfoot is running
    k32 = ctypes.windll.kernel32
    main.mutex = [k32.CreateMutexW(None, False, n) for n in ("BigfootAppMutex", "Global\\BigfootAppMutex")]
    # Python's cycle collector can run on any thread; if it fires on a scanner thread while Qt objects are in
    # the garbage it frees them off the GUI thread and crashes. Collect only on the GUI thread, on a timer.
    gc.disable()
    app = QApplication(sys.argv)
    gc_timer = QTimer(interval=30000, timeout=gc.collect)
    gc_timer.start()
    app.setApplicationName(APP)
    apply_theme(app)
    app.setWindowIcon(foot_icon()[0])
    w = Window(sys.argv[1] if len(sys.argv) > 1 else None)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
