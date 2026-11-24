#!/usr/bin/env python3
"""
tex-annotator: an Overleaf-style local LaTeX editor for a project folder.
Edit the source next to the compiled PDF, chat with Claude, and annotate the
PDF (pencil + comments); Claude Code edits the source and the PDF recompiles.

Usage:
    tex-annotator                       # the LaTeX project in the current folder (or an empty folder)
    tex-annotator path/to/project       # main .tex file detected automatically
    tex-annotator path/to/main.tex      # project = that file's folder
    tex-annotator path/to/file.pdf      # rebuild a PDF as LaTeX, in a new folder next to it
    tex-annotator --check               # check that everything it needs is installed

Requires: latexmk, synctex (TeX Live), and the `claude` CLI (signed in). Importing PDFs also needs Ghostscript.
Only the Python standard library is used. Server log: ~/.cache/tex-annotator/server.log
"""

import argparse
import atexit
import base64
import difflib
import hashlib
import json
import mimetypes
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from . import __version__
    from . import pdf_import
except ImportError:  # run as a plain script
    __version__ = "dev"
    import pdf_import  # type: ignore[no-redef]

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
PROMPTS = HERE / "prompts"
CACHE = Path.home() / ".cache" / "tex-annotator"
LOG_PATH = CACHE / "server.log"
TRASH = CACHE / "trash"

# Files the editor (and Claude's undo tracking) treat as editable text.
TEXT_EXTS = {".tex", ".bib", ".sty", ".cls", ".bst", ".cfg", ".txt", ".md", ".dtx", ".ins", ".csv", ".dat"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
# LaTeX build output hidden from the file tree.
BUILD_SUFFIXES = (".aux", ".log", ".out", ".toc", ".lof", ".lot", ".fls", ".fdb_latexmk", ".synctex.gz",
                  ".synctex", ".bbl", ".blg", ".bcf", ".run.xml", ".nav", ".snm", ".vrb", ".xdv", ".dvi",
                  ".idx", ".ilg", ".ind", ".glo", ".gls", ".glg", ".ist", ".acn", ".acr", ".alg", ".thm")
SKIP_DIRS = {"node_modules", "__pycache__", "_minted"}
MAX_FILES = 3000
MAX_UPLOAD = 30 * 1024 * 1024
MAX_IMPORT = 200 * 1024 * 1024     # PDFs to import
MAX_JSON = 120 * 1024 * 1024       # requests with JSON bodies (an imported PDF's text layer can be large)


# --------------------------------------------------------------------------- #
# Logging (so a crash or kill leaves evidence)
# --------------------------------------------------------------------------- #

def slog(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pid {os.getpid()}] {msg}"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def install_crash_logging():
    def on_signal(signum, _frame):
        name = signal.Signals(signum).name
        slog(f"received {name}; shutting down")
        print(f"\nReceived {name}; shutting down.")
        os._exit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)

    def excepthook(exc_type, exc, tb):
        slog("uncaught exception:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    def thread_excepthook(args):
        slog(f"uncaught exception in thread {args.thread.name if args.thread else '?'}:\n"
             + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    atexit.register(lambda: slog("process exiting"))


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

class State:
    def __init__(self, root: Path, tex: Path, engine: str, model: str | None):
        self.root = root            # project folder (what the file tree shows, where Claude works)
        self.tex = tex              # main document (what gets compiled)
        self.engine = engine
        self.model = model
        self.lock = threading.Lock()
        self.compile_lock = threading.Lock()
        self.last_compile = {"ok": None, "errors": "", "time": 0}
        self.job = {"state": "idle", "changes": {}}
        self.transcript: list[dict] = []   # chat history shown in the UI
        self.partial = ""                  # Claude's reply while it is still streaming
        self.session_id: str | None = None # Claude conversation to resume
        self.context_tokens = 0            # size of the conversation Claude saw on its last request
        self.chat_cost = 0.0               # usage cost of this conversation so far (USD equivalent)
        self.session_total = 0.0           # Claude Code's running cost total for the resumed session
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.undo: dict[str, str | None] | None = None  # abs path -> content before Claude's last edit (None = created)
        self.progress = ""                 # what a long job (PDF import) is doing right now, shown while it runs

    @property
    def pdf(self) -> Path:
        return self.tex.with_suffix(".pdf")

    @property
    def import_dir(self) -> Path:
        return self.root / "import"


S: State  # set in main()


def rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(S.root).as_posix()
    except ValueError:
        return str(p)


def resolve_in_root(relpath: str) -> Path:
    if not relpath or relpath.startswith("/") or "\\" in relpath:
        raise ValueError(f"Invalid path: {relpath!r}")
    p = (S.root / relpath).resolve()
    if p == S.root or not p.is_relative_to(S.root) or any(part.startswith(".") for part in p.relative_to(S.root).parts):
        raise ValueError(f"Path is outside the project: {relpath}")
    return p


def resolve_text(relpath: str) -> Path:
    p = resolve_in_root(relpath)
    if p.suffix.lower() not in TEXT_EXTS:
        raise ValueError(f"Not an editable text file: {relpath}")
    return p


def file_kind(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in TEXT_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    return "other"


def is_build_output(p: Path) -> bool:
    name = p.name.lower()
    if name.endswith(BUILD_SUFFIXES):
        return True
    return p.resolve() == S.pdf.resolve()


def walk_project() -> tuple[list[Path], list[Path]]:
    """(directories, files) in the project, skipping hidden folders and build output."""
    dirs, files = [], []
    for dirpath, dirnames, filenames in os.walk(S.root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        base = Path(dirpath)
        if base != S.root:
            dirs.append(base)
        for f in sorted(filenames):
            if f.startswith("."):
                continue
            p = base / f
            if not is_build_output(p):
                files.append(p)
            if len(files) >= MAX_FILES:
                return dirs, files
    return dirs, files


def text_files(files: list[Path] | None = None) -> list[Path]:
    main = S.tex.resolve()
    files = [p for p in (walk_project()[1] if files is None else files) if p.suffix.lower() in TEXT_EXTS]
    return sorted(files, key=lambda p: (p.resolve() != main, rel(p)))


def tree_signature(dirs: list[Path], files: list[Path]) -> str:
    names = "\n".join(rel(d) + "/" for d in dirs) + "\n" + "\n".join(rel(f) for f in files)
    return hashlib.md5(names.encode()).hexdigest()[:12]


def to_trash(p: Path) -> Path:
    dest = TRASH / time.strftime("%Y%m%d-%H%M%S") / rel(p)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), str(dest))
    return dest


def find_main(root: Path) -> Path | None:
    cands = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".tex"):
                p = Path(dirpath) / f
                try:
                    head = p.read_text(encoding="utf-8", errors="ignore")[:6000]
                except OSError:
                    continue
                if re.search(r"^[^%\n]*\\documentclass", head, re.M):
                    cands.append(p)
    if not cands:
        return None
    return sorted(cands, key=lambda p: (p.name != "main.tex", len(p.relative_to(root).parts), str(p)))[0]


# --------------------------------------------------------------------------- #
# LaTeX compilation and SyncTeX
# --------------------------------------------------------------------------- #

ENGINE_COMMENT = re.compile(r"^\s*%\s*!\s*TEX\s+(?:TS-)?program\s*=\s*(pdflatex|xelatex|lualatex)\b", re.I | re.M)


def engine() -> str:
    """The engine to compile with: a `% !TEX program = …` line at the top of the main file wins over --engine."""
    try:
        head = "\n".join(S.tex.read_text(encoding="utf-8", errors="ignore").splitlines()[:30])
    except OSError:
        return S.engine
    m = ENGINE_COMMENT.search(head)
    return {"pdflatex": "pdf", "xelatex": "xe", "lualatex": "lua"}[m.group(1).lower()] if m else S.engine


def compile_tex() -> dict:
    if not S.tex.exists():                 # empty project: nothing to compile yet
        S.last_compile = {"ok": None, "errors": "", "time": time.time()}
        return S.last_compile
    engine_flag = {"pdf": "-pdf", "xe": "-xelatex", "lua": "-lualatex"}[engine()]
    cmd = [
        "latexmk", engine_flag, "-synctex=1", "-interaction=nonstopmode",
        "-halt-on-error", "-file-line-error", S.tex.name,
    ]
    with S.compile_lock:
        proc = subprocess.run(cmd, cwd=S.tex.parent, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL)
    ok = proc.returncode == 0
    errors = "" if ok else extract_errors(proc.stdout + "\n" + proc.stderr)
    S.last_compile = {"ok": ok, "errors": errors, "time": time.time()}
    slog(f"compile {rel(S.tex)} {'ok' if ok else 'FAILED'}")
    return S.last_compile


def extract_errors(output: str) -> str:
    lines = output.splitlines()
    picked = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\S+\.tex):(\d+):", line)
        if m or line.startswith("!"):
            chunk = lines[i:i + 4]
            if m:  # make the path project-relative so the UI can link it
                chunk[0] = rel(S.tex.parent / m.group(1)) + line[len(m.group(1)):]
            picked.extend(chunk)
    text = "\n".join(picked) if picked else "\n".join(lines[-40:])
    return text[-4000:]


def synctex_lookup(page: int, x: float, y: float) -> tuple[str, int] | None:
    """PDF point (page 1-based, x/y in big points from top-left) -> (file, line)."""
    try:
        out = subprocess.run(
            ["synctex", "edit", "-o", f"{page}:{x:.1f}:{y:.1f}:{S.pdf}", "-d", str(S.tex.parent)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        ).stdout
    except Exception:
        return None
    inp = re.search(r"^Input:(.*)$", out, re.M)
    line = re.search(r"^Line:(-?\d+)$", out, re.M)
    if not inp or not line or int(line.group(1)) <= 0:
        return None
    p = Path(inp.group(1))
    if not p.is_absolute():
        p = S.tex.parent / p
    return rel(p), int(line.group(1))


def synctex_view(relpath: str, line: int) -> dict | None:
    """Source line -> {page, x, y, w, h} box in PDF points (top-left origin)."""
    src = resolve_text(relpath)
    try:
        out = subprocess.run(
            ["synctex", "view", "-i", f"{line}:0:{src}", "-o", str(S.pdf), "-d", str(S.tex.parent)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        ).stdout
    except Exception:
        return None
    boxes, cur = [], {}
    for ln in out.splitlines():
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        if k == "Page":
            if cur:
                boxes.append(cur)
            cur = {"page": int(v)}
        elif k in ("h", "v", "W", "H") and cur:
            cur[k] = float(v)
    if cur:
        boxes.append(cur)
    boxes = [b for b in boxes if {"h", "v", "W", "H"} <= b.keys()]
    if not boxes:
        return None
    page = boxes[0]["page"]
    same = [b for b in boxes if b["page"] == page]
    x0 = min(b["h"] for b in same)
    x1 = max(b["h"] + b["W"] for b in same)
    y0 = min(b["v"] - b["H"] for b in same)
    y1 = max(b["v"] for b in same)
    return {"page": page, "x": x0, "y": y0, "w": max(x1 - x0, 20), "h": max(y1 - y0, 10)}


def locate(page: int, pts: list[tuple[float, float]]) -> dict[str, tuple[int, int]]:
    """SyncTeX each PDF point; return {file: (first_line, last_line)}."""
    hits: dict[str, list[int]] = {}
    for x, y in pts:
        r = synctex_lookup(page, x, y)
        if r:
            hits.setdefault(r[0], []).append(r[1])
    return {f: (min(ls), max(ls)) for f, ls in hits.items()}


def rect_points(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    xs, ys = (x0, (x0 + x1) / 2, x1), (y0, (y0 + y1) / 2, y1)
    return [(x, y) for y in ys for x in xs]


def source_excerpt(relpath: str, lo: int, hi: int, pad: int = 1, cap: int = 14) -> str:
    """Line-numbered excerpt of a source file, so Claude can edit without reading the whole file."""
    try:
        lines = resolve_text(relpath).read_text(encoding="utf-8").splitlines()
    except (ValueError, OSError, UnicodeDecodeError):
        return ""
    a, b = max(1, lo - pad), min(len(lines), hi + pad)
    b = min(b, a + cap - 1)
    return "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(a, b + 1))


def describe_location(hits: dict[str, tuple[int, int]]) -> list[str]:
    if not hits:
        return ["Source: not found by SyncTeX (search for the text instead)."]
    out = []
    for f, (lo, hi) in hits.items():
        out.append(f"Source: {f}:{lo}" + (f"-{hi}" if hi != lo else ""))
        ex = source_excerpt(f, lo, hi)
        if ex:
            out += ["```latex", ex, "```"]
    return out


# --------------------------------------------------------------------------- #
# Snapshots and change tracking for Claude's edits
# --------------------------------------------------------------------------- #

def take_snapshot() -> dict[str, str]:
    snap = {}
    for p in text_files():
        try:
            snap[str(p)] = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            pass
    return snap


def diff_since(snap: dict[str, str]) -> tuple[dict[str, list[int]], dict[str, str | None]]:
    """Changed lines per file (1-based, new side) and the undo record for those files."""
    changes: dict[str, list[int]] = {}
    undo: dict[str, str | None] = {}
    current = {str(p): p for p in text_files()}
    for path, p in current.items():
        try:
            new_text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        old_text = snap.get(path)
        if old_text == new_text:
            continue
        old, new = (old_text or "").splitlines(), new_text.splitlines()
        lines: set[int] = set()
        for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag in ("replace", "insert"):
                lines.update(range(j1 + 1, j2 + 1))
            elif tag == "delete":
                lines.add(min(j1 + 1, max(len(new), 1)))
        changes[rel(p)] = sorted(lines) or [1]
        undo[path] = old_text
    for path, old_text in snap.items():   # files Claude deleted
        if path not in current and not Path(path).exists():
            undo[path] = old_text
    return changes, undo


def apply_undo(record: dict[str, str | None]) -> list[str]:
    done = []
    for path, old in record.items():
        p = Path(path)
        if old is None:
            if p.exists():
                to_trash(p)
                done.append(f"{rel(p)} (removed)")
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(old, encoding="utf-8")
            done.append(rel(p))
    return done


# --------------------------------------------------------------------------- #
# Claude chat
# --------------------------------------------------------------------------- #

def log(msg: str, kind: str = "info", **extra):
    with S.lock:
        S.transcript.append({"t": time.time(), "kind": kind, "msg": msg, **extra})


SYSTEM_PROMPT = """You are working inside tex-annotator, a local Overleaf-style LaTeX editor. The current directory \
is a LaTeX project; its main document is `{main}`. The user sees the LaTeX source side by side with the compiled PDF \
and talks to you through a chat panel. They can also draw on the PDF and pin comments to it. Annotations arrive with \
the PDF text they cover, SyncTeX source line numbers and a line-numbered source excerpt; pencil marks also come with a \
cropped image of the marked area.

- Be economical: when you are given line numbers, Read just that range (Read with offset/limit) instead of whole \
files, and only search the project when the location is unknown.

- When the user asks for a change, edit the project's source files directly with your edit tools. Keep edits focused \
and preserve the document's existing style, macros and structure. You may create new files (e.g. a new chapter) and \
\\input them where appropriate.
- Do not compile or run commands. The app recompiles after your turn and tells you if compilation fails.
- When the user only asks a question, answer it without editing anything.
- Reply concisely in Markdown. Refer to source locations as path/file.tex:LINE relative to the project folder \
(e.g. chapters/intro.tex:42); the user can click them."""

IMPORTED_NOTE = """This project was rebuilt from a PDF by the app's PDF import. import/original.pdf is the original; \
import/pages/page-NN.png show its pages; import/extraction.md has its text and measured layout; \
import/conversion-notes.md records how it was rebuilt. Figures are in two layers: figures/fig-NN-K.tex puts the \
figure's labels (TikZ nodes, real text) over figures/fig-NN-K.png (the image with its text removed); \
figures/original-graphics.pdf is the original without its text; layers/page-NN.tex is a positioned copy of a page. \
When the user compares with the original, look at the original page image."""


def system_prompt(extra: str = "") -> str:
    text = SYSTEM_PROMPT.format(main=rel(S.tex))
    if (S.import_dir / "extraction.md").exists():
        text += "\n\n" + IMPORTED_NOTE
    return text + ("\n\n" + extra if extra else "")


def build_prompt(payload: dict, images: dict[int, Path]) -> str:
    out: list[str] = []
    ann = payload.get("annotations") or {}
    comments = ann.get("comments", [])
    areas = ann.get("areas", [])

    if comments or areas:
        out.append("# Annotations on the PDF")
        for a in areas:
            x0, y0, x1, y1 = a["inner"]
            out += ["", f"## Marked area {a['n']} (page {a['page']})"]
            if a["n"] in images:
                out.append(f"Image of the area with the user's pencil marks in red: {images[a['n']]}")
            if a.get("pins"):
                out.append("Comment pins inside this area: " + ", ".join(str(n) for n in a["pins"]))
            if (a.get("text") or "").strip():
                out.append(f'PDF text under the marks (approximate): "{a["text"].strip()[:400]}"')
            if (a.get("line") or "").strip() and a.get("line") != a.get("text"):
                out.append(f'On the PDF line(s): "{a["line"].strip()[:400]}"')
            out += describe_location(locate(a["page"], rect_points(x0, y0, x1, y1)))
        for c in comments:
            out += ["", f"## Comment {c['n']} (page {c['page']})"]
            if (c.get("text_at") or "").strip():
                out.append(f'PDF text at the pin: "{c["text_at"].strip()[:300]}"')
            out += describe_location(locate(c["page"], [(c["x"], c["y"])]))
            out.append(f"Comment: {c['text'].strip()}")
        out += [
            "",
            "How to read pencil marks: strike-through/scribble = delete; circled or underlined = the nearby comment "
            "applies to it; handwriting or a caret = insert that text; arrows = move content. If a mark is ambiguous, "
            "make the most reasonable edit and say what you assumed. Finish with one line per area/comment saying what you did.",
            "",
        ]

    ctx = payload.get("context") or {}
    if ctx.get("file"):
        line = f"The user has `{ctx['file']}` open in the editor"
        if ctx.get("line"):
            line += f" with the cursor on line {ctx['line']}"
        out.append(line + ".")
        sel = (ctx.get("selection") or "").strip("\n")
        if sel:
            out += [f"They have selected lines {ctx.get('from')}-{ctx.get('to')}:", "```latex", sel[:6000], "```"]
        out.append("")

    message = (payload.get("message") or "").strip()
    out += ["# Message from the user", message or "Please address the annotations above."]
    return "\n".join(out)


MODELS = {"opus", "sonnet", "haiku"}
EFFORTS = {"low", "medium", "high"}
APP_TOOLS = "Read,Edit,Write,Glob,Grep"


def claude_command() -> list[str] | None:
    """How to start Claude Code on this computer, or None if it isn't installed."""
    exe = shutil.which("claude")
    if not exe:   # the official installer's location, in case PATH hasn't been updated yet
        home_bin = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
        exe = str(home_bin) if home_bin.exists() else None
    if not exe:
        return None
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        # npm's Windows shim runs `node …\cli.js`. Run that directly: arguments given to a .cmd pass through
        # cmd.exe, which mangles them.
        try:
            m = re.search(r'%~?dp0%?\\([^"%]+?\.js)"', Path(exe).read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            m = None
        if m:
            script = Path(exe).parent / m.group(1)
            node = Path(exe).parent / "node.exe"
            node = str(node) if node.exists() else shutil.which("node")
            if script.exists() and node:
                return [node, str(script)]
    return [exe]


def kill_tree(proc: subprocess.Popen):
    """Stop a process and everything it started."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
    except (ProcessLookupError, OSError, subprocess.SubprocessError):
        try:
            proc.terminate()
        except OSError:
            pass


def run_claude(prompt: str, extra_dirs: list[str], resume: str | None = None,
               model: str | None = None, effort: str | None = None,
               system: str | None = None) -> tuple[bool, str, str | None]:
    claude = claude_command()
    if not claude:
        return False, "Claude Code isn't installed (the `claude` command wasn't found). Run `tex-annotator --check`.", None
    # The prompt goes in on stdin and the system prompt in a file, so neither passes through the command line
    # (length limits, and quoting on Windows).
    fd, sys_path = tempfile.mkstemp(prefix="texann-system-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(system or system_prompt())
    cmd = [
        *claude, "-p",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--permission-mode", "acceptEdits",
        # Load only what this app needs. Without these, every request also carries the
        # user's MCP servers, plugins and skills (~30k tokens of tool descriptions).
        "--tools", APP_TOOLS, "--allowedTools", *APP_TOOLS.split(","),
        "--strict-mcp-config", "--disable-slash-commands",
        "--restricted",   # no command-running tools, ignore user/project settings, file tools stay in the project
        "--append-system-prompt-file", sys_path,
    ]
    for d in extra_dirs:
        cmd += ["--add-dir", d]
    model = model or S.model
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if resume:
        cmd += ["--resume", resume]

    slog(f"starting claude -p (resume={resume or 'no'})")
    # start_new_session: detach Claude from this terminal and process group so
    # nothing it does to its own group or the TTY can affect the server.
    detach = ({"start_new_session": True} if os.name == "posix"
              else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)})
    try:
        proc = subprocess.Popen(cmd, cwd=S.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, encoding="utf-8", errors="replace", **detach)
    except OSError as e:
        Path(sys_path).unlink(missing_ok=True)
        return False, f"Couldn't start Claude Code: {e}", None
    S.proc = proc
    stderr_buf: list[str] = []
    threading.Thread(target=lambda: stderr_buf.append(proc.stderr.read()), daemon=True).start()

    def feed():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass
    threading.Thread(target=feed, daemon=True).start()

    result_text, ok, session_id, used_model = "", False, None, model or "default"
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = ev.get("session_id", session_id)
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init" and ev.get("model"):
                used_model = ev["model"].replace("claude-", "")
            elif t == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                    with S.lock:
                        S.partial += e["delta"].get("text", "")
            elif t == "assistant":
                u = ev.get("message", {}).get("usage") or {}
                ctx = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                if ctx:
                    S.context_tokens = ctx
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        with S.lock:
                            S.partial = ""
                        log(block["text"].strip(), "claude")
                    elif block.get("type") == "tool_use":
                        inp = block.get("input", {})
                        target = inp.get("file_path") or inp.get("pattern") or ""
                        if target and Path(target).is_absolute():
                            target = rel(Path(target))
                        log(f'{block.get("name")} {target}', "tool")
            elif t == "result":
                ok = not ev.get("is_error") and ev.get("subtype") == "success"
                result_text = ev.get("result", "") or ""
                total = ev.get("total_cost_usd")
                if total is not None:
                    # A resumed session reports its running total, so this turn's cost is the difference.
                    cost = max(0.0, total - (S.session_total if resume else 0.0))
                    S.session_total = total
                    S.chat_cost += cost
                    log(f"{used_model} · ${cost:.3f} · context {S.context_tokens / 1000:.1f}k tokens", "cost")
    finally:
        proc.wait()
        S.proc = None
        Path(sys_path).unlink(missing_ok=True)
        with S.lock:
            S.partial = ""
    slog(f"claude exited rc={proc.returncode}")
    if S.cancelled:
        return False, "Stopped.", session_id
    if proc.returncode != 0 and not result_text:
        err = "".join(stderr_buf)[-2000:]
        return False, f"claude exited with code {proc.returncode}\n{err}".strip(), session_id
    return ok, result_text, session_id


def chat_worker(payload: dict):
    imgdir = Path(tempfile.mkdtemp(prefix="tex-annotator-"))
    try:
        images: dict[int, Path] = {}
        for a in (payload.get("annotations") or {}).get("areas", []):
            if a.get("png"):
                path = imgdir / f"area-{a['n']}-page-{a['page']}.png"
                path.write_bytes(base64.b64decode(a["png"].split(",", 1)[-1]))
                images[a["n"]] = path
        model = payload.get("model") if payload.get("model") in MODELS else None
        effort = payload.get("effort") if payload.get("effort") in EFFORTS else None

        prompt = build_prompt(payload, images)
        try:  # keep the last prompt for troubleshooting ("what exactly did Claude get?")
            (CACHE / "last-prompt.md").write_text(prompt, encoding="utf-8")
        except OSError:
            pass
        snap = take_snapshot()
        ok, result, session = run_claude(prompt, [str(imgdir)], resume=S.session_id, model=model, effort=effort)
        if not ok and S.session_id and not S.cancelled and not session:
            log("Couldn't continue the previous conversation, so starting a new one.", "info")
            ok, result, session = run_claude(prompt, [str(imgdir)], model=model, effort=effort)
        if session:
            S.session_id = session
        if not ok:
            log(result or "Claude run failed", "info" if S.cancelled else "error")

        changes, undo = diff_since(snap)
        comp_ok = True
        if undo:
            S.undo = undo
            log("Recompiling…", "status")
            comp = compile_tex()
            if not comp["ok"] and not S.cancelled:
                log("Compilation failed. Asking Claude to fix it…", "error")
                fix = ("The document no longer compiles after your edits. Fix the errors below "
                       "without undoing the requested changes, then summarise the fix.\n\n" + comp["errors"])
                _, _, session2 = run_claude(fix, [str(imgdir)], resume=S.session_id, model=model, effort=effort)
                if session2:
                    S.session_id = session2
                comp = compile_tex()
                changes, S.undo = diff_since(snap)
            comp_ok = comp["ok"]
            if comp_ok:
                log("PDF updated.", "ok")
            else:
                log("Still failing to compile:\n" + comp["errors"], "error")
        with S.lock:
            S.job["changes"] = changes
            S.job["state"] = "done" if ok and comp_ok else "error"
    except Exception as e:  # noqa: BLE001
        slog("chat turn failed:\n" + traceback.format_exc())
        log(str(e), "error")
        with S.lock:
            S.job["state"] = "error"
    finally:
        shutil.rmtree(imgdir, ignore_errors=True)


def describe_attachments(payload: dict) -> str:
    ann = payload.get("annotations") or {}
    n_marks = len(ann.get("areas", []))
    n_comments = len(ann.get("comments", []))
    parts = []
    if n_marks:
        parts.append(f"{n_marks} marked area{'s' * (n_marks != 1)}")
    if n_comments:
        parts.append(f"{n_comments} comment{'s' * (n_comments != 1)}")
    ctx = payload.get("context") or {}
    if ctx.get("selection"):
        parts.append(f"selection {ctx.get('file')}:{ctx.get('from')}-{ctx.get('to')}")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# PDF import: take the PDF apart (pdf_import.py), then Claude rebuilds it as LaTeX, a few pages at a time
# --------------------------------------------------------------------------- #

PART_WORDS = int(os.environ.get("TEX_ANNOTATOR_PART_WORDS", 2600))   # a part is about this many words…
PART_PAGES = int(os.environ.get("TEX_ANNOTATOR_PART_PAGES", 8))       # …and at most this many pages
CHECK_PAGES = 12    # most compiled pages shown to Claude when it checks a part against the original


def import_source() -> dict | None:
    """The PDF waiting in import/ (uploaded, or given on the command line), if any."""
    if not (S.import_dir / "original.pdf").exists():
        return None
    try:
        return json.loads((S.import_dir / "source.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"name": "original.pdf"}


def plan_parts(pages: list[dict]) -> list[list[int]]:
    parts, cur, weight = [], [], 0
    for p in pages:
        w = max(p["words"], 80) + 150 * len(p["figures"]) + (600 if p["scan"] else 0)
        if cur and (weight + w > PART_WORDS or len(cur) >= PART_PAGES):
            parts.append(cur)
            cur, weight = [], 0
        cur.append(p["n"])
        weight += w
    return parts + [cur] if cur else parts


def page_range(ns: list[int]) -> str:
    return f"page {ns[0]}" if len(ns) == 1 else f"pages {ns[0]}–{ns[-1]}"


def part_prompt(k: int, parts: list[list[int]], analysis: dict) -> str:
    """What Claude is asked to do for part k (the process itself is in prompts/pdf-import.md)."""
    ns, K, total, name = parts[k], len(parts), len(analysis["pages"]), analysis["source"]
    tag = lambda n: pdf_import.page_tag(n, total)  # noqa: E731
    a, b = analysis["pages"][ns[0] - 1]["lines"][0], analysis["pages"][ns[-1] - 1]["lines"][1]
    s0, s1 = analysis["summary_lines"]
    images = (", ".join(f"import/pages/page-{tag(n)}.png" for n in ns) if len(ns) <= 4
              else f"import/pages/page-{tag(ns[0])}.png to page-{tag(ns[-1])}.png")
    if K == 1:
        head = [f"# Rebuild {name} as LaTeX",
                f"The user imported `{name}` ({total} page{'s' * (total != 1)}) and wants it as an editable LaTeX "
                "project. Everything is prepared: follow the PDF-to-LaTeX process in your instructions."]
        steps = [f"Read import/extraction.md lines {s0}–{s1} (the summary), then lines {a}–{b} (the pages).",
                 f"Look at the page images: {images}.",
                 "Write the whole document into main.tex."]
    elif k == 0:
        head = [f"# Rebuild {name} as LaTeX: part 1 of {K} ({page_range(ns)})",
                f"The user imported `{name}` ({total} pages) and wants it as an editable LaTeX project. Everything is "
                "prepared: follow the PDF-to-LaTeX process in your instructions. The document is long, so it is "
                f"converted in {K} parts, each in its own conversation. This is the first."]
        steps = [f"Read import/extraction.md lines {s0}–{s1} (the summary of the whole document), then lines {a}–{b} "
                 f"({page_range(ns)}).",
                 f"Look at the page images of this part ({images}), and at a few from later in the document, so the "
                 "preamble suits all of it.",
                 "Write main.tex: the complete preamble for the whole document, \\begin{document}, then "
                 + ", ".join(f"\\input{{content/part-{i + 1}}}" for i in range(K))
                 + " (one per line, in this order), then \\end{document}. The part files exist and are empty for now.",
                 f"Write {page_range(ns)} into content/part-1.tex."]
    else:
        head = [f"# Rebuild {name} as LaTeX: part {k + 1} of {K} ({page_range(ns)})",
                "The earlier parts were converted in other conversations, following the PDF-to-LaTeX process in your "
                "instructions. Continue seamlessly in the same style."]
        steps = [f"Read import/conversion-notes.md, the preamble of main.tex, and the last 40 lines of content/part-{k}.tex.",
                 f"Read import/extraction.md lines {a}–{b} ({page_range(ns)}); the summary is in lines {s0}–{s1} if "
                 f"you need it. Look at the page images: {images}.",
                 f"Write {page_range(ns)} into content/part-{k + 1}.tex. Change the preamble only if this part needs "
                 "something new, and keep it right for the earlier parts."]
    scans = [n for n in ns if analysis["pages"][n - 1]["scan"]]
    if scans:
        steps.append(f"{page_range(scans).capitalize()} ha{'s' if len(scans) == 1 else 've'} no usable text layer: "
                     "transcribe from the -hires page images, as the extraction explains.")
    steps.append("Write import/conversion-notes.md" + (" (create it)" if k == 0 else " (update it)") + ": the decisions "
                 "that later parts and later edits must follow (fonts and packages, how each S-style is set, macros and "
                 "environments you defined, list and table conventions)"
                 + (", and where this part ends (for example in the middle of a paragraph or a list)" if K > 1 else "")
                 + ". Keep it short.")
    steps.append("Finish with a short summary for the user: how you rebuilt each page (flowing or positioned) and anything "
                 "you could not reproduce faithfully. The app compiles the document right after your turn, so don't "
                 "mention compiling.")
    return "\n".join(head + [""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)])


def check_prompt(orig: list[int], compiled: list[int], total: int) -> str:
    o = ", ".join(f"import/pages/page-{pdf_import.page_tag(n, total)}.png" for n in orig)
    c = ", ".join(f"import/check/page-{n:02d}.png" for n in compiled)
    return ("It compiles. Now check your work against the original: compare the original pages "
            f"({o}) with the compiled pages ({c}). The compiled document reflows, so line and page breaks won't match "
            "exactly, and that's fine. Fix real differences: missing, extra or out-of-order text; wrong heading levels "
            "or numbering; lists, tables or maths that don't match; figures that are missing, misplaced, or have wrong "
            "or doubled labels; clearly wrong fonts, sizes, colours or spacing. If it all matches, change nothing. "
            "Reply with one short line per fix (the app recompiles after your turn, so don't mention compiling).")


def compiled_pages(file_rel: str) -> list[int]:
    """Pages of the compiled PDF that show a .tex file's content (via SyncTeX)."""
    try:
        lines = (S.root / file_rel).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    body = [i + 1 for i, ln in enumerate(lines) if ln.strip() and not ln.lstrip().startswith("%")]
    pages = set()
    for ln in body[:6] + body[len(body) // 2:len(body) // 2 + 2] + body[-6:]:
        box = synctex_view(file_rel, ln)
        if box:
            pages.add(box["page"])
    return list(range(min(pages), max(pages) + 1)) if pages else []


def render_check_pages(file_rel: str | None) -> list[int]:
    """Render the compiled pages for a part (or all of them) to import/check/page-NN.png."""
    if not S.pdf.exists():
        return []
    out = S.import_dir / "check"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    if file_rel:
        pages = compiled_pages(file_rel)
        if not pages:
            return []
        first, last = pages[0], pages[-1]
    else:
        first, last = 1, CHECK_PAGES
    last = min(last, first + CHECK_PAGES - 1)
    pdf_import.run_gs(["-sDEVICE=png16m", "-r100", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
                       f"-dFirstPage={first}", f"-dLastPage={last}", "-o", str(out / "tmp-%03d.png"), str(S.pdf)])
    done = []
    for i, f in enumerate(sorted(out.glob("tmp-*.png"))):
        f.rename(out / f"page-{first + i:02d}.png")
        done.append(first + i)
    return done


def compile_and_fix(session: str | None, run) -> tuple[dict, str | None]:
    """Compile; if it fails, let Claude (same conversation) fix it, twice at most."""
    S.progress = "Compiling…"
    comp = compile_tex()
    for _ in range(2):
        if comp["ok"] or S.cancelled or not session:
            break
        log("It doesn't compile yet. Asking Claude to fix it…", "status")
        S.progress = "Claude is fixing the compile errors…"
        _, _, session2 = run("The document doesn't compile. Fix these errors, keeping all the content (the app "
                             "recompiles after your turn, so don't mention compiling):\n\n" + comp["errors"], session)
        session = session2 or session
        S.progress = "Compiling…"
        comp = compile_tex()
    return comp, session


GENERATED = re.compile(r"^(figures/fig-\d+-\d+\.(png|tex)|layers/page-\d+\.tex|figures/original-graphics\.pdf)$")


def tidy_import() -> list[str]:
    """Move generated files the LaTeX doesn't use (figures, positioned pages) to import/unused/."""
    gen = defaultdict(list)                                 # "figures/fig-02-1" -> its .png and .tex
    for d in ("figures", "layers"):
        for p in (S.root / d).glob("*"):
            r = rel(p)
            if GENERATED.match(r):
                gen[r.rsplit(".", 1)[0]].append(p)
    own = [p for p in text_files() if p.suffix == ".tex" and not rel(p).startswith(("figures/", "layers/", "import/"))]
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in own)
    used, grew = set(), True
    while grew:                                             # snippets that are used can use other files
        grew = False
        for key, files in gen.items():
            if key not in used and re.search(re.escape(key) + r"(?![\w-])", text):
                used.add(key)
                grew = True
                for f in files:
                    if f.suffix == ".tex":
                        text += "\n" + f.read_text(encoding="utf-8", errors="ignore")
    moved = []
    for key, files in gen.items():
        if key in used:
            continue
        for f in files:
            dest = S.import_dir / "unused" / rel(f)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest))
            moved.append(rel(f))
    for d in ("layers", "figures"):
        try:
            (S.root / d).rmdir()                            # only if now empty
        except OSError:
            pass
    return moved


def restore_unused(moved: list[str]):
    for r in moved:
        src = S.import_dir / "unused" / r
        if src.exists():
            (S.root / r).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(S.root / r))


def import_worker(payload: dict):
    """The whole PDF import, as one job: analyse, convert part by part, compile, check, tidy."""
    model = payload.get("model") if payload.get("model") in MODELS else None
    effort = payload.get("effort") if payload.get("effort") in EFFORTS else None
    check = payload.get("check", True)
    ok_all = False
    try:
        src = import_source() or {}
        name = src.get("name") or "original.pdf"
        S.progress = f"Taking {name} apart…"
        log(f"Taking {name} apart: text, layout, fonts, figures…", "status")

        def progress(msg):
            S.progress = msg
        analysis = pdf_import.analyse(S.import_dir / "original.pdf", payload.get("text") or {}, S.root,
                                      progress=progress, source_name=name)
        pages = analysis["pages"]
        notes = []
        if analysis["scanned_pages"]:
            notes.append(f"no text layer on {page_range(analysis['scanned_pages'])} (Claude will read those from the images)")
        if analysis["designed_pages"]:
            notes.append(f"{page_range(analysis['designed_pages'])} look{'s' * (len(analysis['designed_pages']) == 1)} designed")
        log(f"{name}: {len(pages)} page{'s' * (len(pages) != 1)}, {analysis['words']:,} words, {analysis['figures']} "
            f"figure{'s' * (analysis['figures'] != 1)}" + ("; " + "; ".join(notes) if notes else "")
            + f". Page images, figures and the extraction are in import/, figures/ and layers/.", "info")
        if S.cancelled:
            log("Stopped.", "info")
            return
        parts = plan_parts(pages)
        S.tex = S.root / "main.tex"
        if len(parts) > 1:
            (S.root / "content").mkdir(exist_ok=True)
            for i, ns in enumerate(parts, 1):
                f = S.root / "content" / f"part-{i}.tex"
                if not f.exists():
                    f.write_text(f"% Part {i}: {page_range(ns)} of the original (not converted yet)\n", encoding="utf-8")
            log(f"Converting it in {len(parts)} parts of a few pages each, so every page gets Claude's full attention.", "info")
        system = system_prompt((PROMPTS / "pdf-import.md").read_text(encoding="utf-8"))

        def run(prompt, resume=None):
            try:   # keep the last prompt for troubleshooting, as for chat messages
                (CACHE / "last-prompt.md").write_text(prompt, encoding="utf-8")
            except OSError:
                pass
            return run_claude(prompt, [], resume=resume, model=model, effort=effort, system=system)

        for k, ns in enumerate(parts):
            if S.cancelled:
                break
            label = f" (part {k + 1} of {len(parts)})" if len(parts) > 1 else ""
            S.progress = f"Claude is rebuilding {page_range(ns)}{label}…"
            log(f"Rebuilding {page_range(ns)}{label}…", "status")
            ok, result, session = run(part_prompt(k, parts, analysis))
            if not ok:
                log(result or "Claude couldn't finish this part.", "info" if S.cancelled else "error")
                break
            comp, session = compile_and_fix(session, run)
            if check and comp["ok"] and not S.cancelled:
                S.progress = f"Claude is comparing {page_range(ns)} with the original…"
                shown = render_check_pages(None if len(parts) == 1 else f"content/part-{k + 1}.tex")
                if shown:
                    log(f"Checking {page_range(ns)} against the original…", "status")
                    _, _, session2 = run(check_prompt(ns, shown, len(pages)), session)
                    comp, _ = compile_and_fix(session2 or session, run)
            if not comp["ok"] and not S.cancelled:
                log("Still not compiling:\n" + comp["errors"], "error")
        else:
            ok_all = True
        if not S.cancelled and S.tex.exists():
            S.progress = "Tidying up…"
            moved = tidy_import()
            comp = compile_tex()
            if moved and not comp["ok"]:
                restore_unused(moved)
                comp = compile_tex()
                moved = []
            if moved:
                log(f"Moved {len(moved)} generated file{'s' * (len(moved) != 1)} the document doesn't use to import/unused/.", "info")
            if comp["ok"]:
                log(f"Done: {name} is now main.tex. Use the Original button to compare it with the PDF you imported, "
                    "and mark up anything that's off.", "ok")
            ok_all = ok_all and bool(comp["ok"])
    except pdf_import.PdfImportError as e:
        log(str(e), "error")
    except Exception as e:  # noqa: BLE001
        slog("import failed:\n" + traceback.format_exc())
        log(f"The import failed: {e}", "error")
    finally:
        S.progress = ""
        S.session_id = None        # the next chat message starts a fresh conversation about the result
        S.undo = None
        with S.lock:
            S.job["changes"] = {}
            S.job["reload"] = True
            S.job["state"] = "done" if ok_all else "error"


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, ctype: str | None = None):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if path.suffix.lower() == ".svg":
            self.send_header("Content-Security-Policy", "script-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_JSON:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            slog("request failed:\n" + traceback.format_exc())
            raise

    def from_this_app(self) -> bool:
        """Reject requests from other websites open in the same browser (CSRF) and
        DNS-rebinding attempts: the Host must be this machine, and any Origin must be too."""
        local = ("127.0.0.1", "localhost")
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in local:
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null" and urlparse(origin).hostname not in local:
            return False
        return True

    def busy(self) -> bool:
        if S.job["state"] == "running":
            self.send_json({"error": "Claude is working; wait for it to finish."}, 409)
            return True
        return False

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        if not self.from_this_app():
            return self.send_json({"error": "Forbidden"}, 403)
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        if path in ("/", "/index.html"):
            return self.send_file(STATIC / "index.html", "text/html; charset=utf-8")
        if path == "/pdf-extract.js":
            return self.send_file(STATIC / "pdf-extract.js", "text/javascript; charset=utf-8")
        if path == "/pdf":
            return self.send_file(S.pdf, "application/pdf")
        if path.startswith("/vendor/"):
            f = (STATIC / "vendor" / path[len("/vendor/"):]).resolve()
            if f.parent != (STATIC / "vendor").resolve():
                return self.send_error(404)
            return self.send_file(f)
        if path == "/api/status":
            dirs, files = walk_project()
            src = import_source()
            return self.send_json({
                "project": S.root.name,
                "main": rel(S.tex),
                "mainExists": S.tex.exists(),
                "importSource": src.get("name") if src else None,
                "imported": (S.import_dir / "extraction.md").exists(),
                "gs": bool(pdf_import.gs_path()),
                "cacheDir": str(CACHE),
                "pdfExists": S.pdf.exists(),
                "pdfMtime": S.pdf.stat().st_mtime if S.pdf.exists() else 0,
                "compile": S.last_compile,
                "canUndo": bool(S.undo),
                "job": {"state": S.job["state"]},
                "files": {rel(p): p.stat().st_mtime for p in text_files(files)},
                "treeSig": tree_signature(dirs, files),
            })
        if path == "/api/tree":
            dirs, files = walk_project()
            return self.send_json({
                "project": S.root.name, "main": rel(S.tex),
                "dirs": [rel(d) for d in dirs],
                "files": [{"path": rel(f), "kind": file_kind(f), "size": f.stat().st_size} for f in files],
            })
        if path == "/api/file":
            try:
                p = resolve_text(q.get("path", rel(S.tex)))
                return self.send_json({"path": rel(p), "content": p.read_text(encoding="utf-8"),
                                       "mtime": p.stat().st_mtime})
            except (ValueError, OSError, UnicodeDecodeError) as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/raw":
            try:
                return self.send_file(resolve_in_root(q.get("path", "")))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if path == "/api/sync/view":
            try:
                box = synctex_view(q["file"], int(q["line"]))
            except (KeyError, ValueError) as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json(box or {"error": "No PDF location for that line (is it part of the document, and compiled?)"},
                                  200 if box else 404)
        if path == "/api/sync/edit":
            try:
                r = synctex_lookup(int(q["page"]), float(q["x"]), float(q["y"]))
            except (KeyError, ValueError) as e:
                return self.send_json({"error": str(e)}, 400)
            return self.send_json({"file": r[0], "line": r[1]} if r else {"error": "No source location here"},
                                  200 if r else 404)
        if path == "/api/chat":
            since = int(q.get("since", 0))
            with S.lock:
                return self.send_json({
                    "state": S.job["state"], "changes": S.job["changes"],
                    "items": S.transcript[since:], "total": len(S.transcript),
                    "partial": S.partial, "hasSession": S.session_id is not None,
                    "contextTokens": S.context_tokens, "chatCost": round(S.chat_cost, 4),
                    "defaultModel": S.model, "progress": S.progress,
                    "kind": S.job.get("kind", "chat"), "reload": S.job.get("reload", False),
                })
        self.send_error(404)

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        # A JSON (or PDF) content type forces browsers to preflight cross-site requests, which this server never approves.
        ctype = self.headers.get("Content-Type") or ""
        allowed = ctype.startswith("application/json") or (self.path == "/api/import/upload" and ctype.startswith("application/pdf"))
        if not self.from_this_app() or not allowed:
            return self.send_json({"error": "Forbidden"}, 403)
        try:
            return self.route_post()
        except (ValueError, KeyError, OSError) as e:
            return self.send_json({"error": str(e)}, 400)

    def route_post(self):
        if self.path == "/api/compile":
            return self.send_json(compile_tex())

        if self.path == "/api/file":
            if self.busy():
                return
            body = self.read_json()
            p = resolve_text(body["path"])
            base = body.get("base_mtime")
            if p.exists() and base is not None and abs(p.stat().st_mtime - base) > 1e-6 and not body.get("force"):
                return self.send_json({"error": "conflict", "message": f"{rel(p)} changed on disk since you opened it."}, 409)
            p.write_text(body["content"], encoding="utf-8")
            slog(f"saved {rel(p)}")
            return self.send_json({"ok": True, "mtime": p.stat().st_mtime})

        if self.path == "/api/files/create":
            if self.busy():
                return
            body = self.read_json()
            if body.get("folder"):
                p = resolve_in_root(body["path"].rstrip("/"))
                p.mkdir(parents=True, exist_ok=True)
                return self.send_json({"ok": True, "path": rel(p)})
            p = resolve_text(body["path"])
            if p.exists():
                return self.send_json({"error": f"{rel(p)} already exists"}, 409)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body.get("content", ""), encoding="utf-8")
            slog(f"created {rel(p)}")
            return self.send_json({"ok": True, "path": rel(p)})

        if self.path == "/api/files/upload":
            if self.busy():
                return
            body = self.read_json()
            p = resolve_in_root(body["path"])
            data = base64.b64decode(body["data"].split(",", 1)[-1])
            if len(data) > MAX_UPLOAD:
                return self.send_json({"error": "File is larger than 30 MB"}, 400)
            if p.exists() and not body.get("overwrite"):
                return self.send_json({"error": "exists", "message": f"{rel(p)} already exists"}, 409)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            slog(f"uploaded {rel(p)} ({len(data)} bytes)")
            return self.send_json({"ok": True, "path": rel(p)})

        if self.path == "/api/files/rename":
            if self.busy():
                return
            body = self.read_json()
            src, dst = resolve_in_root(body["from"]), resolve_in_root(body["to"])
            if not src.exists():
                return self.send_json({"error": f"{body['from']} does not exist"}, 404)
            if dst.exists():
                return self.send_json({"error": f"{rel(dst)} already exists"}, 409)
            was_main = S.tex.resolve() == src or (src.is_dir() and S.tex.resolve().is_relative_to(src))
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            if was_main:
                S.tex = dst / S.tex.resolve().relative_to(src) if src.is_dir() or dst.is_dir() else dst
            slog(f"renamed {body['from']} -> {rel(dst)}")
            return self.send_json({"ok": True, "path": rel(dst), "main": rel(S.tex)})

        if self.path == "/api/files/delete":
            if self.busy():
                return
            body = self.read_json()
            p = resolve_in_root(body["path"])
            if not p.exists():
                return self.send_json({"error": f"{body['path']} does not exist"}, 404)
            if p == S.tex.resolve() or (p.is_dir() and S.tex.resolve().is_relative_to(p)):
                return self.send_json({"error": "That contains the main document. Set another file as main first."}, 400)
            dest = to_trash(p)
            slog(f"deleted {body['path']} -> {dest}")
            return self.send_json({"ok": True, "trash": str(dest)})

        if self.path == "/api/main":
            if self.busy():
                return
            body = self.read_json()
            p = resolve_in_root(body["path"])
            if p.suffix.lower() != ".tex" or not p.is_file():
                return self.send_json({"error": "The main document must be a .tex file"}, 400)
            S.tex = p
            slog(f"main document -> {rel(p)}")
            return self.send_json({"ok": True, "main": rel(p), "compile": compile_tex()})

        if self.path == "/api/chat":
            payload = self.read_json()
            with S.lock:
                if S.job["state"] == "running":
                    return self.send_json({"error": "Claude is still working on the last message."}, 409)
                S.job = {"state": "running", "changes": {}}
                S.cancelled = False
            log((payload.get("message") or "").strip() or "(annotations only)", "user",
                attach=describe_attachments(payload))
            slog("chat message received")
            threading.Thread(target=chat_worker, args=(payload,), daemon=True, name="claude-chat").start()
            return self.send_json({"ok": True})

        if self.path == "/api/import/upload":
            # The raw PDF, saved as import/original.pdf; converting it is a separate step (/api/import/start).
            if self.busy():
                return
            if S.tex.exists():
                return self.send_json({"error": "This project already has a LaTeX document. To import a PDF, start "
                                                 "tex-annotator in an empty folder, or run: tex-annotator path/to/file.pdf"}, 409)
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_IMPORT:
                return self.send_json({"error": f"The PDF is larger than {MAX_IMPORT // 2**20} MB."}, 400)
            data = self.rfile.read(n)
            if b"%PDF" not in data[:1024]:
                return self.send_json({"error": "That file isn't a PDF."}, 400)
            name = Path(unquote(self.headers.get("X-Filename") or "document.pdf")).name or "document.pdf"
            S.import_dir.mkdir(exist_ok=True)
            (S.import_dir / "original.pdf").write_bytes(data)
            (S.import_dir / "source.json").write_text(json.dumps({"name": name, "bytes": len(data)}), encoding="utf-8")
            slog(f"import: received {name} ({len(data)} bytes)")
            return self.send_json({"ok": True, "name": name})

        if self.path == "/api/import/start":
            payload = self.read_json()
            src = import_source()
            if not src:
                return self.send_json({"error": "Upload a PDF first."}, 400)
            if S.tex.exists():
                return self.send_json({"error": "This project already has a LaTeX document."}, 409)
            if not pdf_import.gs_path():
                return self.send_json({"error": pdf_import.GS_MISSING}, 400)
            with S.lock:
                if S.job["state"] == "running":
                    return self.send_json({"error": "Claude is still working on the last message."}, 409)
                S.job = {"state": "running", "changes": {}, "kind": "import"}
                S.cancelled = False
            pages = len((payload.get("text") or {}).get("pages") or [])
            log(f"Rebuild {src.get('name')} as LaTeX", "user",
                attach=f"{pages} page{'s' * (pages != 1)}" + (", check against the original" if payload.get("check", True) else ""))
            slog("import started")
            threading.Thread(target=import_worker, args=(payload,), daemon=True, name="pdf-import").start()
            return self.send_json({"ok": True})

        if self.path == "/api/chat/new":
            if S.job["state"] == "running":
                return self.send_json({"error": "Stop Claude first."}, 409)
            with S.lock:
                S.transcript = []
                S.session_id = None
                S.context_tokens = 0
                S.chat_cost = 0.0
                S.session_total = 0.0
                S.job = {"state": "idle", "changes": {}}
            return self.send_json({"ok": True})

        if self.path == "/api/cancel":
            proc = S.proc
            if S.job["state"] != "running":
                return self.send_json({"ok": True})
            S.cancelled = True
            if proc and proc.poll() is None:
                kill_tree(proc)
            slog("cancel requested")
            return self.send_json({"ok": True})

        if self.path == "/api/undo":
            if self.busy():
                return
            if not S.undo:
                return self.send_json({"error": "Nothing to undo."}, 400)
            changed = apply_undo(S.undo)
            S.undo = None
            with S.lock:
                S.job["changes"] = {}
            log("Undid Claude's last edit (" + (", ".join(changed) or "no changes") + ").", "info")
            return self.send_json({"restored": changed, "compile": compile_tex()})

        self.send_error(404)


def in_wsl() -> bool:
    return sys.platform.startswith("linux") and "microsoft" in platform.uname().release.lower()


def open_browser(url: str):
    if in_wsl():   # a Linux program inside Windows: open the Windows browser
        for cmd in (["wslview", url], ["explorer.exe", url]):
            if shutil.which(cmd[0]):
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
    try:
        if webbrowser.open(url):
            return
    except Exception:  # noqa: BLE001
        pass
    for cmd in (["xdg-open", url], ["code", "--open-url", url]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
    print(f"(Couldn't open a browser automatically; open {url} yourself.)")


INSTALLER = ("install.ps1 (Windows)" if os.name == "nt" else "install.sh")
HINTS = {  # how to get each missing piece, per system
    "tex": {"win": "install MiKTeX (winget install MiKTeX.MiKTeX) and Strawberry Perl (winget install StrawberryPerl.StrawberryPerl)",
            "mac": "install MacTeX: brew install --cask mactex-no-gui (or download it from tug.org/mactex)",
            "linux": "sudo apt install texlive-latex-extra texlive-fonts-recommended latexmk"},
    "perl": {"win": "latexmk needs Perl: winget install StrawberryPerl.StrawberryPerl, then open a new terminal"},
    "gs": {"win": "download it from https://ghostscript.com/releases/gsdnld.html (or run install.ps1 again)",
           "mac": "brew install ghostscript (MacTeX usually includes it)",
           "linux": "sudo apt install ghostscript"},
    "claude": {"win": "irm https://claude.ai/install.ps1 | iex   (in PowerShell)",
               "mac": "curl -fsSL https://claude.ai/install.sh | bash",
               "linux": "curl -fsSL https://claude.ai/install.sh | bash"},
}


def hint(what: str) -> str:
    key = "win" if os.name == "nt" else "mac" if sys.platform == "darwin" else "linux"
    return HINTS[what].get(key, "")


def check_requirements() -> bool:
    """Print what is installed and what is missing. Returns True if everything needed is there."""
    ok = True

    def line(good, name: str, detail: str):
        mark = "OK " if good else "?  " if good is None else "MISSING"
        print(f"  {mark:<7}  {name:<10} {detail}")

    def run(cmd: list[str], timeout=30) -> tuple[int, str]:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=timeout, stdin=subprocess.DEVNULL)
            return out.returncode, (out.stdout or "") + (out.stderr or "")
        except Exception as e:  # noqa: BLE001
            return -1, str(e)

    def first_line(text: str) -> str:
        return (text.strip().splitlines() or [""])[0][:70]

    print(f"tex-annotator {__version__} on {platform.system()} {platform.release()}" + (" (WSL)" if in_wsl() else "") + "\n")
    py_ok = sys.version_info >= (3, 10)
    line(py_ok, "python", f"{sys.version.split()[0]}" + ("" if py_ok else "  (3.10 or newer is required)"))
    ok &= py_ok

    latexmk = shutil.which("latexmk")
    if not latexmk:
        line(False, "latexmk", "a TeX distribution is needed: " + hint("tex"))
        ok = False
    else:
        rc, out = run([latexmk, "--version"])
        works = "latexmk" in out.lower() and rc == 0
        line(works, "latexmk", first_line(out) if works else (hint("perl") or first_line(out)))
        ok &= works
    synctex = shutil.which("synctex")
    line(bool(synctex), "synctex", first_line(run([synctex, "--version"])[1]) if synctex else "comes with TeX: " + hint("tex"))
    ok &= bool(synctex)

    gs = pdf_import.gs_path()
    print(f"  {'OK ' if gs else 'missing':<7}  {'gs':<10} " +
          (first_line(run([gs, "--version"])[1]) + "  (for importing PDFs)" if gs else
           "only needed to import PDFs: " + hint("gs")))

    claude = claude_command()
    if not claude:
        line(False, "claude", "Claude Code is needed: " + hint("claude") + ", then run `claude` once to sign in")
        print(f"\nInstall the missing pieces above (or run {INSTALLER} again), then run `tex-annotator --check`.")
        return False
    line(True, "claude", first_line(run([*claude, "--version"])[1]))
    rc, out = run([*claude, "auth", "status"])
    try:
        signed_in = bool(json.loads(out[out.index("{"):]).get("loggedIn"))
    except ValueError:
        signed_in = True if rc == 0 else False if rc == 1 else None
    line(signed_in, "sign-in", "signed in to Claude" if signed_in else
         "run `claude` once in a terminal and sign in" if signed_in is False else
         "couldn't check; run `claude` once in a terminal to make sure you're signed in")
    ok &= signed_in is not False
    print("\nAll set." if ok else f"\nInstall the missing pieces above (or run {INSTALLER} again), then run `tex-annotator --check`.")
    return ok


def project_for_pdf(pdf: Path) -> Path:
    """`tex-annotator report.pdf` works in report-latex/ next to the PDF, with the PDF waiting in import/."""
    for i in range(1, 100):
        root = pdf.parent / (f"{pdf.stem}-latex" if i == 1 else f"{pdf.stem}-latex-{i}")
        if (root / "import" / "original.pdf").exists() or (root.is_dir() and find_main(root)):
            return root                                      # an earlier import of it: carry on there
        if root.exists() and any(root.iterdir()):
            continue                                         # someone else's folder: don't touch it
        (root / "import").mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, root / "import" / "original.pdf")
        (root / "import" / "source.json").write_text(json.dumps({"name": pdf.name, "bytes": pdf.stat().st_size}),
                                                    encoding="utf-8")
        print(f"Made a project folder for {pdf.name}: {root}")
        return root
    sys.exit(f"Couldn't find a free folder name next to {pdf}.")


def main():
    global S
    # Print progress immediately even when stdout isn't a terminal.
    sys.stdout.reconfigure(line_buffering=True, errors="replace")
    sys.stderr.reconfigure(line_buffering=True, errors="replace")
    ap = argparse.ArgumentParser(prog="tex-annotator", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?", default=".", help="Project folder or main .tex file (default: current folder)")
    ap.add_argument("--port", type=int, default=8765, help="Port for the local web page (default 8765; the next free one is used if taken)")
    ap.add_argument("--engine", choices=["pdf", "xe", "lua"], default="pdf", help="LaTeX engine: pdflatex (default), xelatex or lualatex")
    ap.add_argument("--model", default=None, help="Claude model to use (default: your Claude Code default)")
    ap.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically")
    ap.add_argument("--check", action="store_true", help="Check that the required tools are installed, then exit")
    ap.add_argument("--version", action="version", version=f"tex-annotator {__version__}")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check_requirements() else 1)

    target = Path(args.project).expanduser().resolve()
    if target.is_file() and target.suffix.lower() == ".pdf":
        root = project_for_pdf(target)
        tex = find_main(root) or root / "main.tex"
    elif target.is_dir():
        root = target
        if root == Path.home() or root == Path(root.anchor):
            sys.exit(f"{root} is your whole home/root folder. Run tex-annotator inside a LaTeX project folder instead.")
        tex = find_main(root) or root / "main.tex"   # no document yet: the page offers to import a PDF or start one
    elif target.is_file() and target.suffix == ".tex":
        root, tex = target.parent, target
    else:
        sys.exit(f"Not a folder, .tex or .pdf file: {target}")
    missing = [t for t in ("latexmk", "synctex", "claude") if not shutil.which(t)]
    if missing:
        print(f"warning: not found: {', '.join(missing)}. Run `tex-annotator --check` for help.", file=sys.stderr)

    install_crash_logging()
    S = State(root, tex, args.engine, args.model)
    slog(f"starting for project {root} (main {rel(tex)})")
    print(f"Project: {root}")
    if tex.exists():
        print(f"Main document: {rel(tex)}\nCompiling (first build can take a little while)…")
        res = compile_tex()
        print("  ok" if res["ok"] else "  compile failed:\n" + res["errors"])
    elif import_source():
        print(f"Ready to rebuild {import_source().get('name')} as LaTeX: press Convert on the page.")
    else:
        print("No LaTeX document here yet: the page lets you import a PDF or start a blank document.")

    server, port = None, args.port
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            print(f"  port {port} is in use, trying {port + 1}…")
    if server is None:
        sys.exit("Could not find a free port.")
    server.daemon_threads = True

    url = f"http://127.0.0.1:{port}/"
    slog(f"serving at {url}")
    print("\n" + "=" * 60)
    print(f"  tex-annotator is running. Open this in your browser:\n\n      {url}\n")
    print("  Leave this terminal open. Press Ctrl+C to stop.")
    print(f"  Log: {LOG_PATH}")
    print("=" * 60 + "\n")
    if not args.no_browser:
        threading.Timer(0.5, open_browser, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        slog("stopped with Ctrl+C")
        print("\nStopped.")


if __name__ == "__main__":
    main()
