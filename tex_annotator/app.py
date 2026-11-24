#!/usr/bin/env python3
"""
tex-annotator: an Overleaf-style local LaTeX editor for a project folder.
Edit the source next to the compiled PDF, chat with Claude, and annotate the
PDF (pencil + comments); Claude Code edits the source and the PDF recompiles.

Usage:
    tex-annotator                       # the LaTeX project in the current folder
    tex-annotator path/to/project       # main .tex file detected automatically
    tex-annotator path/to/main.tex      # project = that file's folder
    tex-annotator --check               # check that everything it needs is installed

Requires: latexmk, synctex (TeX Live), and the `claude` CLI (signed in).
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from . import __version__
except ImportError:  # run as a plain script
    __version__ = "dev"

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
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

    @property
    def pdf(self) -> Path:
        return self.tex.with_suffix(".pdf")


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

def compile_tex() -> dict:
    engine_flag = {"pdf": "-pdf", "xe": "-xelatex", "lua": "-lualatex"}[S.engine]
    cmd = [
        "latexmk", engine_flag, "-synctex=1", "-interaction=nonstopmode",
        "-halt-on-error", "-file-line-error", S.tex.name,
    ]
    with S.compile_lock:
        proc = subprocess.run(cmd, cwd=S.tex.parent, capture_output=True, text=True, stdin=subprocess.DEVNULL)
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
            capture_output=True, text=True, timeout=10,
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
            capture_output=True, text=True, timeout=10,
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


def run_claude(prompt: str, extra_dirs: list[str], resume: str | None = None,
               model: str | None = None, effort: str | None = None) -> tuple[bool, str, str | None]:
    claude = shutil.which("claude")
    if not claude:
        return False, "The `claude` CLI was not found on PATH.", None
    cmd = [
        claude, "-p", prompt,
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--permission-mode", "acceptEdits",
        # Load only what this app needs. Without these, every request also carries the
        # user's MCP servers, plugins and skills (~30k tokens of tool descriptions).
        "--tools", APP_TOOLS, "--allowedTools", *APP_TOOLS.split(","),
        "--strict-mcp-config", "--disable-slash-commands",
        "--restricted",   # no command-running tools, ignore user/project settings, file tools stay in the project
        "--append-system-prompt", SYSTEM_PROMPT.format(main=rel(S.tex)),
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
    proc = subprocess.Popen(cmd, cwd=S.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1, encoding="utf-8", **detach)
    S.proc = proc
    stderr_buf: list[str] = []
    threading.Thread(target=lambda: stderr_buf.append(proc.stderr.read()), daemon=True).start()

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
        if n > MAX_UPLOAD * 1.4:
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
        if path == "/pdf":
            return self.send_file(S.pdf, "application/pdf")
        if path.startswith("/vendor/"):
            f = (STATIC / "vendor" / path[len("/vendor/"):]).resolve()
            if f.parent != (STATIC / "vendor").resolve():
                return self.send_error(404)
            return self.send_file(f)
        if path == "/api/status":
            dirs, files = walk_project()
            return self.send_json({
                "project": S.root.name,
                "main": rel(S.tex),
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
                    "defaultModel": S.model,
                })
        self.send_error(404)

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        # A JSON content type forces browsers to preflight cross-site requests, which this server never approves.
        if not self.from_this_app() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
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
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGTERM)
                    else:
                        proc.terminate()
                except (ProcessLookupError, OSError):
                    pass
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


def open_browser(url: str):
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


def check_requirements() -> bool:
    """Print what is installed and what is missing. Returns True if everything needed is there."""
    ok = True

    def line(good: bool, name: str, detail: str):
        print(f"  {'OK ' if good else 'MISSING'}  {name:<10} {detail}")

    def version(cmd: list[str]) -> str:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return (out.stdout or out.stderr).strip().splitlines()[0][:70]
        except Exception:  # noqa: BLE001
            return ""

    print(f"tex-annotator {__version__}\n")
    py_ok = sys.version_info >= (3, 10)
    line(py_ok, "python", f"{sys.version.split()[0]}" + ("" if py_ok else "  (3.10 or newer is required)"))
    ok &= py_ok
    for tool, hint in (("latexmk", "Install TeX Live. Ubuntu/Debian: sudo apt install texlive-latex-extra latexmk · macOS: install MacTeX"),
                       ("synctex", "Comes with TeX Live / MacTeX (texlive-binaries on Ubuntu)")):
        path = shutil.which(tool)
        line(bool(path), tool, version([tool, "--version"]) if path else hint)
        ok &= bool(path)
    claude = shutil.which("claude")
    if not claude:
        line(False, "claude", "Install Claude Code (https://claude.com/claude-code), then run `claude` once to sign in")
        return False
    line(True, "claude", version([claude, "--version"]))
    try:
        st = json.loads(subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=30).stdout)
        signed_in = bool(st.get("loggedIn"))
    except Exception:  # noqa: BLE001
        signed_in = None
    if signed_in is None:
        print("  ?        sign-in    couldn't check; run `claude` once in a terminal to make sure you're signed in")
    else:
        line(signed_in, "sign-in", "signed in to Claude" if signed_in else "run `claude` once in a terminal and sign in")
        ok &= signed_in
    print("\nAll set." if ok else "\nInstall the missing pieces above, then run `tex-annotator --check` again.")
    return ok


def main():
    global S
    # Print progress immediately even when stdout isn't a terminal.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
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
    if target.is_dir():
        root = target
        if root == Path.home() or root == Path(root.anchor):
            sys.exit(f"{root} is your whole home/root folder. Run tex-annotator inside a LaTeX project folder instead.")
        tex = find_main(root)
        if not tex:
            sys.exit(f"No .tex file with \\documentclass found in {root}.\n"
                     f"Run tex-annotator inside a LaTeX project folder, or pass the main .tex file: tex-annotator path/to/main.tex")
    elif target.is_file() and target.suffix == ".tex":
        root, tex = target.parent, target
    else:
        sys.exit(f"Not a folder or .tex file: {target}")
    missing = [t for t in ("latexmk", "synctex", "claude") if not shutil.which(t)]
    if missing:
        print(f"warning: not found: {', '.join(missing)}. Run `tex-annotator --check` for help.", file=sys.stderr)

    install_crash_logging()
    S = State(root, tex, args.engine, args.model)
    slog(f"starting for project {root} (main {rel(tex)})")
    print(f"Project: {root}\nMain document: {rel(tex)}")
    print(f"Compiling (first build can take a little while)…")
    res = compile_tex()
    print("  ok" if res["ok"] else "  compile failed:\n" + res["errors"])

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
