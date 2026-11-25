"""
Starting Latex-Copilot like any other app: from Applications (macOS), the Start menu or desktop (Windows), or
the app menu (Linux), with no terminal. `latex-copilot --launch` asks for the project folder with the system's
own folder dialog, where a new folder can also be made, then opens the app in the browser. A folder that is
already open is just shown again. With no terminal to press Ctrl+C in, the app stops by itself a couple of
minutes after its browser tab is closed (see app.idle_watchdog).

Only the Python standard library is used. The dialogs come from the system: AppleScript on macOS, Tk (which
uses Windows' own dialog) or PowerShell on Windows, and zenity, kdialog or Tk on Linux.
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

TITLE = "Latex-Copilot"
PROMPT = "Choose the folder for your LaTeX project. To start a new project, make a new folder."
CACHE = Path.home() / ".cache" / "latex-copilot"
STATE = CACHE / "launcher.json"        # the last folder opened, so the dialog starts there
RUNNING = CACHE / "running.json"       # which folders are open, on which port


def _key(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict):
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------- without a terminal
def console_fallback() -> bool:
    """pythonw (the Windows shortcut) has no console, so print() has nowhere to go: send it to a log file.
    Returns True when there was no console."""
    if sys.stdout is not None and sys.stderr is not None:
        return False
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        log = open(CACHE / "launcher.log", "a", encoding="utf-8", errors="replace")
    except OSError:
        log = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log
    return True


def prepare(no_console: bool):
    """Started from an icon rather than a terminal: find the tools a terminal would, and on Windows keep the
    programs the app runs (latexmk, TeX, Ghostscript, Claude Code) from each opening a console window."""
    if os.name == "nt":
        if no_console:
            flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            original = subprocess.Popen.__init__

            def no_window(self, *args, **kwargs):
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | flag
                original(self, *args, **kwargs)
            subprocess.Popen.__init__ = no_window
        return
    # Apps started from the Dock or an app menu don't get the PATH a terminal has, so take it from a login
    # shell, plus the usual places for TeX, Homebrew and Claude Code.
    dirs = []
    shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    try:
        out = subprocess.run([shell, "-lc", 'printf "\\n%s" "$PATH"'], capture_output=True, text=True,
                             timeout=15, stdin=subprocess.DEVNULL).stdout
        dirs += out.strip().splitlines()[-1].split(":") if out.strip() else []
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    dirs += [str(Path.home() / ".local" / "bin"), "/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin"]
    path = os.environ.get("PATH", "").split(os.pathsep)
    for d in dirs:
        if d and d not in path and os.path.isdir(d):
            path.append(d)
    os.environ["PATH"] = os.pathsep.join(p for p in path if p)


# ------------------------------------------------------------------- dialogs
def _run(cmd: list[str], timeout: int = 3600) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return -1, ""


def _applescript_text(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tk_folder(start: str | None) -> str | None:
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)     # in front of the browser and the desktop
        chosen = filedialog.askdirectory(parent=root, title=PROMPT, initialdir=start or str(Path.home()),
                                         mustexist=False)
        root.destroy()
    except Exception:  # noqa: BLE001 (no display, Tk not set up…)
        return None
    return chosen or ""


def choose_folder(start: str | None = None) -> str | None:
    """The system's folder dialog. Returns the folder, "" if the user cancelled, None if no dialog is available."""
    start = start if start and os.path.isdir(start) else None
    if sys.platform == "darwin":
        where = f" default location (POSIX file {_applescript_text(start)})" if start else ""
        script = (f"activate\ntry\nset f to choose folder with prompt {_applescript_text(PROMPT)}{where}\n"
                  "on error number -128\nreturn \"\"\nend try\nreturn POSIX path of f")
        code, out = _run(["osascript", "-e", script])
        return out if code == 0 else None
    if os.name == "nt":
        chosen = _tk_folder(start)
        if chosen is not None:
            return chosen
        ps = ("Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
              f"$d.Description = '{PROMPT}'; $d.ShowNewFolderButton = $true; "
              + (f"$d.SelectedPath = '{start.replace(chr(39), chr(39) * 2)}'; " if start else "")
              + "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }")
        code, out = _run(["powershell", "-NoProfile", "-STA", "-Command", ps])
        return out if code == 0 else None
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", f"--title={PROMPT}"]
        if start:
            cmd.append(f"--filename={start.rstrip('/')}/")
        code, out = _run(cmd)
        return out if code == 0 else ""
    if shutil.which("kdialog"):
        code, out = _run(["kdialog", "--getexistingdirectory", start or str(Path.home()), "--title", PROMPT])
        return out if code == 0 else ""
    return _tk_folder(start)


def message(text: str):
    """Tell the user something, in a window (there's no terminal to print to)."""
    print(text, file=sys.stderr)
    if sys.platform == "darwin":
        _run(["osascript", "-e", f"activate\ndisplay dialog {_applescript_text(text)} buttons {{\"OK\"}} "
                                 f"default button 1 with title {_applescript_text(TITLE)} with icon caution"])
    elif os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, TITLE, 0x30 | 0x40000)   # warning icon, on top
        except (OSError, AttributeError):
            pass
    elif shutil.which("zenity"):
        _run(["zenity", "--warning", f"--title={TITLE}", f"--text={text}", "--no-markup"])
    elif shutil.which("kdialog"):
        _run(["kdialog", "--title", TITLE, "--sorry", text])
    elif shutil.which("notify-send"):
        _run(["notify-send", TITLE, text])


# ------------------------------------------------- the last folder; open folders
def last_folder() -> str | None:
    folder = _load(STATE).get("last")
    return folder if folder and os.path.isdir(folder) else None


def remember(folder: Path):
    data = _load(STATE)
    data["last"] = str(folder)
    _save(STATE, data)


def register(root: Path, port: int):
    data = _load(RUNNING)
    data[_key(root)] = {"port": port, "pid": os.getpid()}
    _save(RUNNING, data)


def unregister(root: Path):
    data = _load(RUNNING)
    if data.get(_key(root), {}).get("pid") == os.getpid():
        del data[_key(root)]
        _save(RUNNING, data)


def find_running(root: Path, token: str) -> int | None:
    """The port of a Latex-Copilot already open on this folder, if there is one."""
    entry = _load(RUNNING).get(_key(root))
    if not isinstance(entry, dict) or not isinstance(entry.get("port"), int):
        return None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{entry['port']}/api/status",
                                     headers={"X-Latex-Copilot-Token": token})
        with urllib.request.urlopen(req, timeout=3) as r:
            status = json.loads(r.read())
        return entry["port"] if _key(status.get("root", "")) == _key(root) else None
    except (OSError, ValueError):
        return None
