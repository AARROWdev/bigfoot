"""Black-box tests against the INSTALLED Bigfoot.exe, driven through Windows UI Automation like a user would.
Runs inside Windows Sandbox only (tests/sandbox/inside.ps1).

    blackbox.py wizard <setup.exe>        click through the setup wizard, ticking the desktop shortcut
    blackbox.py title <Bigfoot.exe> <t>   start it, check the window title, close it
    blackbox.py all <Bigfoot.exe>         settings remembered, two copies at once, bad %LOCALAPPDATA%,
                                          no error.log, the mutex (copy + paste into Notepad: tests/app test 42)
    blackbox.py portable <Bigfoot.exe>    the unzipped portable copy starts and scans
"""
import ctypes
import os
import subprocess
import sys
import time
import traceback

import types

import win32con
import win32gui
import win32process

try:
    import win32ui  # noqa: F401  (needs the MFC runtime, which Windows Sandbox doesn't have)
except ImportError:
    sys.modules["win32ui"] = types.ModuleType("win32ui")  # pywinauto only uses it for screenshots
from pywinauto import Application, Desktop  # noqa: E402

OUT = r"C:\out"
RESULTS = os.path.join(OUT, "blackbox-results.txt")
if os.environ.get("USERNAME", "").upper() != "WDAGUTILITYACCOUNT":
    sys.exit("black-box tests run only inside Windows Sandbox")


LOG = open(os.path.join(OUT, "blackbox.log"), "a", encoding="utf-8")
sys.stdout = sys.stderr = LOG  # everything, tracebacks included, goes to the log (UTF-8)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def wait(cond, timeout=60, what="condition", step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    raise TimeoutError(what)


def windows_of(pid, title_prefix="Bigfoot"):
    found = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd).startswith(title_prefix):
            if win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
                found.append(hwnd)
    win32gui.EnumWindows(cb, None)
    return found


class Bigfoot:
    def __init__(self, exe, env=None):
        self.proc = subprocess.Popen([exe], env=env)
        self.hwnd = wait(lambda: (windows_of(self.proc.pid) or [None])[0], 60, "Bigfoot window")
        self.app = Application(backend="uia").connect(process=self.proc.pid)
        self.win = self.app.window(handle=self.hwnd)
        time.sleep(1)

    def texts(self):
        return [c.window_text() for c in self.win.descendants(control_type="Text")]

    def status(self):
        for t in self.texts():
            if t.startswith(("Scanned", "Stopped", "Scanning", "Pick a drive", "Not a folder", "Copied", "Sent")):
                return t
        return ""

    def scan(self, path, timeout=120):
        edit = self.win.child_window(control_type="Edit", found_index=0)
        edit.set_edit_text(path)  # through UI Automation: works for any text and without keyboard focus
        self.win.child_window(title="Scan", control_type="Button").invoke()
        return wait(lambda: self.status().startswith((f"Scanned {path}", f"Stopped {path}")) and self.status(),
                    timeout, f"scan {path}")

    def path_text(self):
        return self.win.child_window(control_type="Edit", found_index=0).get_value()

    def close(self, timeout=30):
        win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
        self.proc.wait(timeout)
        return self.proc.returncode


def make_tree(root, n_dirs=3, n_files=5, name="f"):
    for d in range(n_dirs):
        p = os.path.join(root, f"d{d}")
        os.makedirs(p, exist_ok=True)
        for f in range(n_files):
            with open(os.path.join(p, f"{name}{f}.bin"), "wb") as fh:
                fh.write(b"x" * (100 * (f + 1)))
    return root


results = []


def check(name, ok, detail=""):
    results.append(f"{'PASS' if ok else 'FAIL'}  {name}{('  (' + detail + ')') if detail and not ok else ''}")
    log(results[-1])


def run_check(name, fn):
    try:
        fn()
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:  # a failed check must not leave a Bigfoot open (the uninstall test would be refused)
        subprocess.run(["taskkill", "/f", "/im", "Bigfoot.exe"], capture_output=True)
        subprocess.run(["taskkill", "/f", "/im", "notepad.exe"], capture_output=True)
        time.sleep(1)


# ------------------------------------------------------------------ modes
def msaa_checked(it):
    """The MSAA "checked" state of a UIA element, or None if it can't be read."""
    try:
        from pywinauto.uia_defines import get_elem_interface
        return bool(get_elem_interface(it.element_info.element, "LegacyIAccessible").CurrentState & 0x10)
    except Exception:  # noqa: BLE001
        return None


def tick_desktop_task():
    """Ticks "Create a desktop shortcut" through UI Automation (no mouse or keyboard needed). True if done."""
    try:
        w = Desktop(backend="uia").window(title_re="Setup - Bigfoot.*")
        for it in w.descendants():
            if "desktop" in it.window_text().lower():
                for how in ("toggle", "legacy", "click"):  # Toggle pattern, MSAA default action, or a click
                    try:
                        if how == "toggle":
                            it.toggle()
                        elif how == "legacy":
                            from pywinauto.uia_defines import get_elem_interface
                            get_elem_interface(it.element_info.element, "LegacyIAccessible").DoDefaultAction()
                        else:  # a posted mouse click on the item itself (no SendInput)
                            wiz = Desktop(backend="win32").window(title_re="Setup - Bigfoot.*")
                            lb = wiz.child_window(class_name="TNewCheckListBox").wrapper_object()
                            r, lr = it.rectangle(), lb.rectangle()
                            lb.click(coords=(r.left - lr.left + 8, (r.top + r.bottom) // 2 - lr.top))
                        time.sleep(0.5)
                        checked = msaa_checked(it)
                        log("tried", how, "-> checked:", checked)
                        if checked is not False:  # True, or unknown (the shortcut check decides)
                            return True
                    except Exception as e:  # noqa: BLE001
                        log("no", how, "on", it.element_info.control_type, e)
    except Exception as e:  # noqa: BLE001
        log("UIA tick failed", e)
    return False


def wizard(setup):
    proc = subprocess.Popen([setup, "/LOG=" + os.path.join(OUT, "wizard.log")])
    d = Desktop(backend="win32")
    ticked = False
    for _ in range(25):
        try:
            w = wait(lambda: d.window(title_re="Setup - Bigfoot.*").wrapper_object(), 20, "setup wizard")
        except TimeoutError:
            break
        shown = [c for c in w.descendants() if c.is_visible()]
        log("page:", [(c.class_name(), c.window_text()[:40]) for c in shown if c.window_text()
                      or "Check" in c.class_name()])
        tasks = [c for c in shown if "CheckListBox" in c.class_name()]
        if tasks and not ticked:  # "Select Additional Tasks": tick "Create a desktop shortcut" (the only task)
            r = tasks[0].rectangle()
            tick_desktop_task()  # (no keyboard fallback: nothing here may use focus or key input)
            ticked = True
            log("ticked the desktop shortcut task", r)
            time.sleep(0.5)
        btns = {c.window_text().replace("&", "").rstrip(" >"): c for c in shown
                if "Button" in c.class_name() and c.is_enabled()}
        for cap in ("Install", "Next", "Finish"):
            if cap in btns:
                log("click", cap)
                btns[cap].click()
                break
        else:
            time.sleep(1)
            continue
        if cap == "Finish":
            break
        time.sleep(1.5)
    proc.wait(120)
    # Finish launches Bigfoot (the "Launch Bigfoot" box is ticked): close it so it saves its settings
    pid = wait(lambda: next((p for p in _pids("Bigfoot.exe")), None), 30, "Bigfoot launched by Finish")
    hwnd = wait(lambda: (windows_of(pid) or [None])[0], 60, "its window")
    time.sleep(2)
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    wait(lambda: pid not in _pids("Bigfoot.exe"), 30, "Bigfoot to close")
    return proc.returncode


def _pids(name):
    out = subprocess.run(["tasklist", "/fi", f"imagename eq {name}", "/fo", "csv", "/nh"], capture_output=True,
                         text=True).stdout
    return [int(l.split('","')[1]) for l in out.splitlines() if l.startswith('"')]


def title(exe, want):
    b = Bigfoot(exe)
    t = win32gui.GetWindowText(b.hwnd)
    b.close()
    log("title", repr(t))
    return 0 if t == want else 1


def portable(exe):
    root = make_tree(r"C:\bbtest\portable")
    b = Bigfoot(exe)
    s = b.scan(root)
    b.close()
    log(s)
    return 0 if s.startswith(f"Scanned {root}") and "15 files" in s else 1


def all_tests(exe):
    lad = os.path.join(os.environ["LOCALAPPDATA"], "Bigfoot", "error.log")

    def remembered():
        root = make_tree(r"C:\bbtest\remember")
        b = Bigfoot(exe)
        s = b.scan(root)
        check("BB scan through the UI", s.startswith(f"Scanned {root}") and "15 files" in s, s)
        win32gui.ShowWindow(b.hwnd, win32con.SW_SHOWNOACTIVATE)  # never activate anything
        win32gui.SetWindowPos(b.hwnd, 0, 77, 66, 1111, 701, win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER)
        time.sleep(0.5)
        r1 = win32gui.GetWindowRect(b.hwnd)
        check("BB closes cleanly", b.close() == 0)
        b = Bigfoot(exe)
        r2 = win32gui.GetWindowRect(b.hwnd)
        check("BB window size and position remembered", r1 == r2, f"{r1} vs {r2}")
        check("BB last folder remembered", b.path_text() == root, b.path_text())
        b.close()

    def two_copies():
        big = r"C:\bbtest\big"
        for d in range(300):
            make_tree(os.path.join(big, f"g{d}"), 10, 20)
        small = make_tree(r"C:\bbtest\small")
        b1, b2 = Bigfoot(exe), Bigfoot(exe)
        b1.win.child_window(control_type="Edit", found_index=0).set_edit_text(big)
        b1.win.child_window(title="Scan", control_type="Button").invoke()
        time.sleep(0.3)
        s2 = b2.scan(small)
        rc = b1.close()  # mid-scan (or right after)
        check("BB two copies at once: second one scans", s2.startswith(f"Scanned {small}"), s2)
        check("BB closing a copy mid-scan exits cleanly", rc == 0, f"exit {rc}")
        s2 = b2.scan(big, 300)
        check("BB the other copy keeps working", s2.startswith(f"Scanned {big}") and "60,000 files" in s2, s2)
        b2.close()

    def bad_localappdata():
        for case, val in (("a file", r"C:\bbtest\notafolder.txt"), ("a missing drive", r"Q:\nope")):
            open(r"C:\bbtest\notafolder.txt", "w").close()
            env = dict(os.environ, LOCALAPPDATA=val)
            try:
                b = Bigfoot(exe, env)
                alive = b.proc.poll() is None
                b.close()
                check(f"BB starts when %LOCALAPPDATA% is {case}", alive)
            except TimeoutError:
                p = subprocess.run(["tasklist"], capture_output=True, text=True).stdout
                check(f"BB starts when %LOCALAPPDATA% is {case}", False, "no window within 60 s")
                subprocess.run(["taskkill", "/f", "/im", "Bigfoot.exe"], capture_output=True)

    def mutex():
        b = Bigfoot(exe)
        k32 = ctypes.windll.kernel32
        k32.OpenMutexW.restype = ctypes.c_void_p
        h = k32.OpenMutexW(0x00100000, False, "BigfootAppMutex")
        check("BB app mutex held while running (uninstaller relies on it)", bool(h))
        if h:
            k32.CloseHandle(ctypes.c_void_p(h))
        b.close()

    for name, fn in (("remembered", remembered), ("two copies", two_copies), ("bad LOCALAPPDATA", bad_localappdata),
                     ("mutex", mutex)):
        log("---", name)
        run_check(f"BB {name}", fn)
    check("BB no error.log after normal use", not os.path.exists(lad) or os.path.getsize(lad) == 0,
          open(lad, encoding="utf-8", errors="replace").read()[-800:] if os.path.exists(lad) else "")
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write("\n".join(results) + "\n")
    return 0 if all(r.startswith("PASS") for r in results) else 1


if __name__ == "__main__":
    mode = sys.argv[1]
    try:
        rc = {"wizard": lambda: wizard(sys.argv[2]), "title": lambda: title(sys.argv[2], sys.argv[3]),
              "all": lambda: all_tests(sys.argv[2]), "portable": lambda: portable(sys.argv[2])}[mode]()
    except Exception:
        traceback.print_exc()
        rc = 3
    log("exit", rc)
    sys.exit(rc)
