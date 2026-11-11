"""Bigfoot - find what's eating your disk. Fast scan, biggest-first tree, treemap, type breakdown, top files."""
import heapq
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

from PySide6.QtCore import (QAbstractItemModel, QModelIndex, QObject, QPointF, QRectF, QSettings, QSize,
                            QStorageInfo, Qt, QTimer, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QFontMetrics, QGuiApplication, QKeySequence,
                           QLinearGradient, QPainter, QPalette, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog, QFileIconProvider,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QSplitter, QStyle, QStyledItemDelegate, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolButton, QTreeView, QVBoxLayout, QWidget)
from PySide6.QtCore import QFile

APP = "Bigfoot"
VERSION = "1.0"
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
    ("Code + data", "#4fd6e8", "py js ts html css json xml c cpp h cs java go rs php sql db sqlite log"),
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
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def fdate(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


# ---------------------------------------------------------------- tree
class File:
    __slots__ = ("name", "parent", "size", "mtime", "_row")
    is_dir = False
    nfiles, ndirs = 1, 0

    def __init__(self, name, parent, size, mtime):
        self.name, self.parent, self.size, self.mtime, self._row = name, parent, size, mtime, 0

    @property
    def path(self):
        return os.path.join(self.parent.path, self.name)


class Dir:
    __slots__ = ("name", "path", "parent", "dirs", "files", "own", "own_mt", "size", "nfiles", "ndirs",
                 "mtime", "denied", "_row", "_kids")
    is_dir = True

    def __init__(self, name, path, parent, mtime=0):
        self.name, self.path, self.parent = name, path, parent
        self.dirs, self.files = [], []
        self.own = self.size = self.nfiles = self.ndirs = 0
        self.own_mt = self.mtime = mtime
        self.denied = False
        self._row, self._kids = 0, None


def retotal(root):
    """Post-order sum of sizes/counts/newest mtime. Cheap: uses per-dir accumulators, not per-file sums."""
    order, stack = [], [root]
    while stack:
        d = stack.pop()
        order.append(d)
        stack.extend(d.dirs)
    for d in reversed(order):
        size, nf, nd, mt = d.own, len(d.files), len(d.dirs), d.own_mt
        for c in d.dirs:
            size += c.size
            nf += c.nfiles
            nd += c.ndirs
            if c.mtime > mt:
                mt = c.mtime
        d.size, d.nfiles, d.ndirs, d.mtime = size, nf, nd, mt
        d._kids = None


def walk_files(d):
    stack = [d]
    while stack:
        x = stack.pop()
        yield from x.files
        stack.extend(x.dirs)


# ---------------------------------------------------------------- scanner
class Scanner(QObject):
    finished = Signal()

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
        self.t1 = time.time()
        retotal(self.root)
        self.finished.emit()

    def _work(self):
        while True:
            d = self.q.get()
            if d is None:
                return
            try:
                if not self.stopped:
                    self._scan(d)
            except Exception:
                d.denied = True
            finally:
                self.q.task_done()

    def _scan(self, d):
        self.current = d.path
        own, mt, nf = 0, d.own_mt, 0
        try:
            with os.scandir(d.path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.is_junction() or e.is_symlink():
                                continue
                            st = e.stat(follow_symlinks=False)
                            sub = Dir(e.name, os.path.join(d.path, e.name), d, st.st_mtime)
                            d.dirs.append(sub)
                            self.dirs += 1
                            self.q.put(sub)
                        else:
                            st = e.stat(follow_symlinks=False)
                            d.files.append(File(e.name, d, st.st_size, st.st_mtime))
                            own += st.st_size
                            nf += 1
                            if st.st_mtime > mt:
                                mt = st.st_mtime
                    except OSError:
                        pass
        except OSError:
            d.denied = True
            self.denied += 1
        d.own, d.own_mt = own, mt
        self.files += nf
        self.bytes += own


# ---------------------------------------------------------------- model
COLS = ["Name", "Size", "% of parent", "Files", "Folders", "Modified"]
C_NAME, C_SIZE, C_PCT, C_FILES, C_DIRS, C_MOD = range(6)


class TreeModel(QAbstractItemModel):
    def __init__(self):
        super().__init__()
        self.root = None
        self.sort_col, self.desc = C_SIZE, True
        self.show_files = True
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
            items = d.dirs + d.files if self.show_files else list(d.dirs)
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
        self.kids(it.parent)
        return self.createIndex(it._row, col, it)

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
        return len(it.dirs) + (len(it.files) if self.show_files else 0)

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
        p = it.parent
        if p is None or it is self.root:
            return 1.0
        return it.size / p.size if p.size else 0.0

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
            if it.is_dir and it.denied:
                return QColor("#ff8a8a")
            if c in (C_FILES, C_DIRS, C_MOD):
                return QColor("#9aa0aa")
        elif role == Qt.ToolTipRole and c == C_NAME:
            tip = it.path
            if it.is_dir and it.denied:
                tip += "\n(access denied: some contents not counted)"
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


class BarDelegate(QStyledItemDelegate):
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
            qp.setBrush(QColor(ACCENT))
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
        if w <= 0 or h <= 0:
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
        if r.width() < 1 or r.height() < 1:
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
            kids = sorted((k for k in (*it.dirs, *it.files) if k.size > 0), key=lambda k: k.size, reverse=True)
            total = it.size
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
        self.setToolTip(f"{it.path}\n{human(it.size)}{extra}")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            it = self.hit(e.position())
            if it is not None:
                self.picked.emit(it)
        elif e.button() == Qt.RightButton and self.focus is not None and self.focus.parent is not None:
            self.zoom.emit(self.focus.parent)

    def mouseDoubleClickEvent(self, e):
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
        for c, w in ((C_SIZE, 90), (C_PCT, 130), (C_FILES, 80), (C_DIRS, 70), (C_MOD, 125)):
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
        crumb.addWidget(self.btn_up)
        crumb.addWidget(self.crumb, 1)
        ml.addLayout(crumb)
        self.map = TreeMap()
        self.map.picked.connect(self.map_picked)
        self.map.zoom.connect(self.set_focus)
        ml.addWidget(self.map, 1)
        legend = QHBoxLayout()
        legend.setContentsMargins(6, 0, 6, 4)
        for name, col, _ in CATS + [(OTHER[0], OTHER[1], "")]:
            lab = QLabel(f"<span style='color:{col}'>■</span> {name}")
            legend.addWidget(lab)
        legend.addStretch(1)
        ml.addLayout(legend)
        self.tabs.addTab(mapw, "Map")

        self.types = QTableWidget(0, 5)
        self.types.setHorizontalHeaderLabels(["Type", "Extension", "Size", "% of folder", "Files"])
        self.setup_table(self.types)
        self.types.setItemDelegateForColumn(3, BarDelegate(self.types))
        self.tabs.addTab(self.types, "Types")

        self.top = QTableWidget(0, 4)
        self.top.setHorizontalHeaderLabels(["Name", "Size", "Folder", "Modified"])
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

        self.tick = QTimer(self, interval=150, timeout=self.progress)
        self.live = QTimer(self, interval=2000, timeout=self.live_refresh)
        self.sel_timer = QTimer(self, singleShot=True, interval=120, timeout=self.apply_selection)

        QShortcut(QKeySequence.Delete, self.tree, lambda: self.trash(self.selected_items()))
        QShortcut(QKeySequence.Copy, self.tree, lambda: self.copy_paths(self.selected_items()))
        QShortcut(QKeySequence("F5"), self, self.scan)
        QShortcut(QKeySequence("Ctrl+L"), self, lambda: (self.path.setFocus(), self.path.selectAll()))
        QShortcut(QKeySequence("Backspace"), self.map, self.map_up)

        g = self.settings.value("geometry")
        if g:
            self.restoreGeometry(g)
        else:
            self.resize(1500, 900)
        s = self.settings.value("split")
        if s:
            self.split.restoreState(s)
        else:
            self.split.setSizes([int(self.width() * 0.52), int(self.width() * 0.48)])
        h = self.settings.value("header")
        if h:
            self.tree.header().restoreState(h)

        if start:
            self.path.setText(start)
            QTimer.singleShot(0, self.scan)
        else:
            self.path.setText(self.settings.value("last_path") or "C:\\")

    # ---------- helpers
    def setup_table(self, t):
        t.verticalHeader().hide()
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setShowGrid(False)
        t.setAlternatingRowColors(True)
        t.horizontalHeader().setStretchLastSection(True)
        t.verticalHeader().setDefaultSectionSize(22)

    def fill_drives(self):
        self.drives.clear()
        for v in QStorageInfo.mountedVolumes():
            if not v.isValid() or not v.isReady():
                continue
            root = v.rootPath().replace("/", "\\")
            used = v.bytesTotal() - v.bytesAvailable()
            name = v.displayName() if v.displayName() != v.rootPath() else ""
            self.drives.addItem(f"{root}  {name}  {human(used)} / {human(v.bytesTotal())}".replace("    ", "  "), root)

    def drive_picked(self, i):
        self.path.setText(self.drives.itemData(i))
        self.scan()

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to scan", self.path.text() or "C:\\")
        if d:
            self.path.setText(os.path.normpath(d))
            self.scan()

    def selected_items(self):
        seen, out = set(), []
        for i in self.tree.selectionModel().selectedRows(0):
            it = i.internalPointer()
            if id(it) not in seen:
                seen.add(id(it))
                out.append(it)
        if not out:
            cur = self.tree.currentIndex()
            if cur.isValid():
                out.append(cur.internalPointer())
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
        if not os.path.isdir(p):
            self.status.setText(f"Not a folder: {p}")
            return
        p = os.path.abspath(p)
        self.path.setText(p)
        self.settings.setValue("last_path", p)
        self.scanner = Scanner(p)
        self.scanner.finished.connect(self.scan_done, Qt.QueuedConnection)
        self.model.set_root(self.scanner.root)
        self.set_focus(self.scanner.root)
        self.btn_scan.setText("Stop")
        self.btn_scan.setEnabled(True)
        self.show_usage(p)
        for i in range(self.drives.count()):
            if p.upper().startswith(self.drives.itemData(i).upper()):
                self.drives.setCurrentIndex(i)
        self.scanner.start()
        self.tick.start()
        self.live.start()

    def show_usage(self, p):
        v = QStorageInfo(p)
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
        if not s or s.t1:
            return
        el = time.time() - s.t0
        cur = s.current
        if len(cur) > 90:
            cur = cur[:40] + "…" + cur[-45:]
        self.status.setText(f"Scanning…  {s.files:,} files  ·  {s.dirs:,} folders  ·  {human(s.bytes)}  ·  "
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
        return exp, (cur.internalPointer() if cur.isValid() else None), self.tree.verticalScrollBar().value()

    def restore_view(self, snap):
        exp, cur, scroll = snap
        for d in exp:
            self.tree.setExpanded(self.model.index_for(d), True)
        if cur is not None:
            self.tree.selectionModel().setCurrentIndex(self.model.index_for(cur),
                                                       QItemSelectionModelFlags())
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

    def live_refresh(self):
        if self.scanner and not self.scanner.t1:
            self.rebuild()

    def scan_done(self):
        self.tick.stop()
        self.live.stop()
        s = self.scanner
        self.rebuild()
        if self.model.root and not self.tree.isExpanded(self.model.index_for(self.model.root)):
            self.tree.expand(self.model.index_for(self.model.root))
        self.btn_scan.setText("Scan")
        self.btn_scan.setEnabled(True)
        r = s.root
        msg = (f"{'Stopped' if s.stopped else 'Scanned'} {r.path}  ·  {r.nfiles:,} files  ·  {r.ndirs:,} folders  ·  "
               f"{human(r.size)}  ·  {s.t1 - s.t0:.1f}s")
        if s.denied:
            msg += f"  ·  {s.denied:,} folders access denied (run as admin to count them)"
        self.status.setText(msg)
        self.refresh_tables()
        cur = self.drives.currentIndex()
        self.fill_drives()
        self.drives.setCurrentIndex(cur)

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
        self.refresh_tables()

    def update_crumb(self):
        d = self.focus
        if d is None:
            self.crumb.setText("")
            return
        self.crumb.setText(f"<b>{d.path}</b>  ·  {human(d.size)}  ·  {d.nfiles:,} files")
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
        self.tree.setCurrentIndex(idx)
        self.map_pick = False
        self.tree.scrollTo(idx)

    def map_picked(self, it):
        self.select_in_tree(it)
        self.map.set_sel(it)

    def tree_double(self, idx):
        it = idx.internalPointer()
        if not it.is_dir:
            os.startfile(it.path)

    def toggle_files(self, on):
        snap = self.snapshot_view()
        self.model.show_files = on
        self.model.set_root(self.model.root)
        if snap[1] is not None and not snap[1].is_dir and not on:
            snap = (snap[0], snap[1].parent, snap[2])
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
            t.setSortingEnabled(False)
            t.setRowCount(len(rows))
            for r, (e, (sz, n)) in enumerate(rows):
                cname, col = EXT_CAT.get(e, OTHER)
                it0 = QTableWidgetItem(f"■ {cname}")
                it0.setForeground(QColor(col))
                pct = sz / d.size if d.size else 0
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
            m.addAction("Open", lambda: os.startfile(one.path))
            m.addAction("Show in Explorer", lambda: self.reveal(one))
            if one.is_dir:
                m.addAction("Show in map", lambda: (self.select_in_tree(one), self.set_focus(one)))
                m.addAction("Scan only this folder", lambda: (self.path.setText(one.path), self.scan()))
        m.addAction("Copy path" + ("s" if len(items) > 1 else ""), lambda: self.copy_paths(items))
        m.addSeparator()
        total = sum(i.size for i in items)
        m.addAction(f"Send to Recycle Bin ({human(total)})", lambda: self.trash(items))
        m.exec(gpos)

    def reveal(self, it):
        subprocess.Popen(f'explorer /select,"{it.path}"')

    def copy_paths(self, items):
        QGuiApplication.clipboard().setText("\n".join(i.path for i in items))
        self.status.setText(f"Copied {len(items)} path{'s' if len(items) > 1 else ''}")

    def trash(self, items):
        items = [i for i in items if i.parent is not None]
        if not items:
            return
        if self.scanner and not self.scanner.t1:
            self.status.setText("Wait for the scan to finish before deleting.")
            return
        total = sum(i.size for i in items)
        names = "\n".join(i.path for i in items[:8]) + ("\n…" if len(items) > 8 else "")
        if QMessageBox.question(self, "Send to Recycle Bin",
                                f"Send {len(items)} item{'s' if len(items) > 1 else ''} ({human(total)}) "
                                f"to the Recycle Bin?\n\n{names}") != QMessageBox.Yes:
            return
        failed = []
        for it in items:
            r = QFile.moveToTrash(it.path)
            ok = r[0] if isinstance(r, tuple) else bool(r)
            if not ok:
                failed.append(it.path)
                continue
            p = it.parent
            if it.is_dir:
                p.dirs.remove(it)
            else:
                p.files.remove(it)
                p.own -= it.size
            if self.focus is it or self._inside(self.focus, it):
                self.focus = p
        cur = self.tree.currentIndex()
        if cur.isValid() and any(cur.internalPointer() is i or self._inside(cur.internalPointer(), i) for i in items):
            self.tree.setCurrentIndex(self.model.index_for(items[0].parent))
        self.rebuild()
        self.set_focus(self.focus)
        self.status.setText(f"Sent {len(items) - len(failed)} to the Recycle Bin" +
                            (f"  ·  couldn't move: {', '.join(failed[:3])}" if failed else ""))

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
        self.settings.setValue("header", self.tree.header().saveState())
        super().closeEvent(e)


def QItemSelectionModelFlags():
    from PySide6.QtCore import QItemSelectionModel
    return QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows


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
    app.setStyle("Fusion")
    p = QPalette()
    text = QColor("#e6e8ec")
    for role, col in ((QPalette.Window, "#1f242c"), (QPalette.Base, "#13161c"), (QPalette.AlternateBase, "#181c23"),
                      (QPalette.Button, "#1f242c"), (QPalette.ToolTipBase, "#181c23"), (QPalette.Highlight, "#24405e")):
        p.setColor(role, QColor(col))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.ToolTipText):
        p.setColor(role, text)
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.PlaceholderText, QColor("#7d8088"))
    app.setPalette(p)
    app.setStyleSheet(f"""
        QTreeView, QTableWidget {{ border:1px solid #2a3039; }}
        QHeaderView::section {{ background:#181c23; color:#b9bcc4; border:0; border-right:1px solid #2a3039;
                                padding:4px 6px; }}
        QLineEdit, QComboBox {{ background:#13161c; border:1px solid #2a3039; border-radius:4px; padding:4px 6px; }}
        QLineEdit:focus {{ border-color:{ACCENT}; }}
        QPushButton#primary {{ background:{ACCENT}; color:#04121f; font-weight:700; border:0; border-radius:4px;
                               padding:5px 18px; }}
        QPushButton#primary:hover {{ background:#5cb2ff; }}
        QPushButton#primary:disabled {{ background:#33404f; color:#8a96a3; }}
        QToolButton {{ padding:4px 8px; border-radius:4px; }}
        QToolButton:hover {{ background:#2a3039; }}
        QToolButton:checked {{ background:#24405e; }}
        QProgressBar {{ background:#13161c; border:1px solid #2a3039; border-radius:3px; color:#e6e8ec;
                        text-align:center; font-size:11px; }}
        QProgressBar::chunk {{ background:{ACCENT}; border-radius:2px; }}
        QTabWidget::pane {{ border:0; }}
        QTabBar::tab {{ background:#181c23; color:#b9bcc4; padding:6px 14px; border:0; margin-right:2px; }}
        QTabBar::tab:selected {{ background:#24405e; color:#ffffff; }}
        QLabel#crumb {{ color:#c9d3e0; }}
        QSplitter::handle {{ background:#1f242c; }}
        QMenu::item:selected {{ background:#24405e; }}
    """)


def install_error_log():
    d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP)
    os.makedirs(d, exist_ok=True)
    log = os.path.join(d, "error.log")

    def hook(t, v, tb):
        with open(log, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "".join(traceback.format_exception(t, v, tb)) + "\n")
        sys.__excepthook__(t, v, tb)
    sys.excepthook = hook


def main():
    install_error_log()
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    apply_theme(app)
    app.setWindowIcon(foot_icon()[0])
    w = Window(sys.argv[1] if len(sys.argv) > 1 else None)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
