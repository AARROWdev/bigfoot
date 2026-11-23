"""Takes the README screenshots (docs\\map.png, types.png, top.png) of a folder made by tools\\make_demo.py.

No window appears on screen (Qt lays it out and paints it without showing it) and the mouse and keyboard aren't
used. Bigfoot's settings go to a temporary .ini file, never the registry.

    python tools\\screenshots.py D:\\BigfootDemo
"""
import os
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import bigfoot as bf  # noqa: E402


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    if len(sys.argv) != 2 or not os.path.isdir(sys.argv[1]):
        raise SystemExit(__doc__)
    demo = os.path.abspath(sys.argv[1])
    ini = os.path.join(tempfile.mkdtemp(prefix="bigfoot-shots-"), "bigfoot.ini")
    bf.QSettings = lambda *a, **k: QSettings(ini, QSettings.IniFormat)
    app = QApplication([])
    app.setApplicationName(bf.APP)
    bf.apply_theme(app)
    w = bf.Window()
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.resize(1440, 860)
    w.show()
    w.path.setText(demo)
    w.scan()
    for _ in range(600):
        pump(app, 0.1)
        if w.scanner.t1 and w.status.text().startswith("Scanned"):
            break
    else:
        raise SystemExit("the scan didn't finish")
    pump(app, 0.5)
    out = os.path.join(ROOT, "docs")
    for tab, name in ((0, "map"), (1, "types"), (2, "top")):
        w.tabs.setCurrentIndex(tab)
        pump(app, 0.4)
        w.grab().save(os.path.join(out, f"{name}.png"))
        print("wrote", os.path.join(out, f"{name}.png"))
    print(w.status.text())


if __name__ == "__main__":
    main()
