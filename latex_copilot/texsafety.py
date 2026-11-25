#!/usr/bin/env python3
"""
Which files a LaTeX document used outside its project, and keeping biber offline when it did.

TeX can read any file you can: TeX Live's `openin_any=p` stops that only in older TeX Live, and MiKTeX has no
such setting. So after every compile Latex-Copilot checks the files TeX recorded in <jobname>.fls (latexmk
always runs TeX with -recorder). If the document read or wrote files outside its project, apart from TeX's own
files and fonts, the app removes the PDF until the user allows those files for that project. The allowed
files are kept in ~/.cache/latex-copilot/trust.json, outside every project, where Claude can't reach them.

Run as a script, this file stands in for biber, which latexmk runs between TeX passes and which can download
from the internet. It won't run biber if the document has read files outside its project, so their contents
can't be sent anywhere:

    texsafety.py biber [biber options] main.bcf

Only the Python standard library is used.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Folders that belong to a TeX installation or hold the system's fonts, wherever they are.
TEX_DIR = re.compile(r"^(texmf.*|texlive|miktex|\.?tinytex|\.?fonts|fontconfig)$", re.I)
FONT_EXTS = {".otf", ".ttf", ".ttc", ".otc", ".dfont", ".pfb", ".pfa", ".pfm", ".afm", ".tfm", ".ofm", ".vf",
             ".ovf", ".enc", ".map", ".woff", ".woff2"}
# When older TeX Live's openin_any=p stops a read, some versions say so; LaTeX then reports the file as missing.
REFUSED = re.compile(r"Not reading from (.+?) \(openin_any = p\)|LaTeX Error: File `(.+?)' not found")
TRIED_EXTS = ("", ".tex", ".pdf", ".png", ".jpg", ".jpeg", ".eps")   # what \input and \includegraphics add
_ROOTS: list[str] | None = None


def real(path: str) -> str:
    return os.path.realpath(path)


def key(path: str) -> str:
    """For comparing paths (Windows paths ignore case)."""
    return os.path.normcase(os.path.realpath(path))


def within(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([key(path), key(folder)]) == key(folder)
    except ValueError:                       # different drives
        return False


def tex_roots() -> list[str]:
    """TeX Live's own folders, as kpsewhich reports them. (MiKTeX is recognised by its folder names.)"""
    global _ROOTS
    if _ROOTS is None:
        found = []
        for arg in ("-var-value=TEXMFROOT", "-expand-path=$TEXMF", "-expand-path=$TEXMFCNF"):
            try:
                out = subprocess.run(["kpsewhich", arg], capture_output=True, text=True, encoding="utf-8",
                                     errors="replace", timeout=30, stdin=subprocess.DEVNULL).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            found += re.split(r"[;\r\n]" if os.name == "nt" else r"[:\r\n]", out)
        home = str(Path.home())
        roots = []
        for d in (x.strip().lstrip("!") for x in found):
            # Never a folder that holds the home folder, and nothing as broad as /usr or C:\
            if d and os.path.isabs(d) and len(Path(d).parts) > 2 and not within(home, d):
                roots.append(real(d))
        _ROOTS = roots
    return _ROOTS


def tex_owned(path: str) -> bool:
    p = Path(path)
    return (p.suffix.lower() in FONT_EXTS
            or any(TEX_DIR.match(part) for part in p.parts[:-1])
            or any(within(path, r) for r in tex_roots()))


def recorded(fls: Path) -> list[tuple[str, str]]:
    """("read" | "write", path) for every file TeX opened, from its .fls recorder file."""
    try:
        lines = fls.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    pwd, out, seen = str(fls.parent), [], set()
    for line in lines:
        kind, _, name = line.partition(" ")
        if kind == "PWD" and name:
            pwd = name
        elif kind in ("INPUT", "OUTPUT") and name:
            path = real(name if os.path.isabs(name) else os.path.join(pwd, name))
            item = ("read" if kind == "INPUT" else "write", path)
            if (item[0], key(path)) not in seen:
                seen.add((item[0], key(path)))
                out.append(item)
    return out


def refused(output: str, cwd: Path) -> list[tuple[str, str]]:
    """Reads that older TeX Live refused (openin_any=p), from TeX's output: a file reported missing that does
    exist. Worth listing all the same, so the user can allow it."""
    out = []
    for m in REFUSED.finditer(output):
        name = m.group(1) or m.group(2)
        for ext in TRIED_EXTS:
            path = real(os.path.join(cwd, name + ext))
            if os.path.isfile(path):
                if ("read", path) not in out:
                    out.append(("read", path))
                break
    return out


def outside(root: Path, fls: Path, output: str = "", allowed=()) -> list[tuple[str, str]]:
    """Files outside the project, other than TeX's own and fonts, that the document read or wrote (or tried to read)."""
    ok = {key(a) for a in allowed}
    found = []
    for kind, path in recorded(fls) + refused(output, fls.parent):
        if within(path, str(root)) or key(path) in ok or tex_owned(path):
            continue
        if all(key(path) != key(p) for _, p in found):
            found.append((kind, path))
    return found


def load_trust(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def project_trust(path, root) -> dict:
    """What the user has allowed for this project: {"files": [...], "lualatex": bool}."""
    entry = load_trust(path).get("projects", {}).get(key(str(root)), {})
    return entry if isinstance(entry, dict) else {}


def guard(argv: list[str]) -> int:
    """Run a tool that can reach the internet (biber) only if the document read nothing outside its project."""
    if not argv:
        return 2
    tool, args = argv[0], argv[1:]
    root = os.environ.get("LATEX_COPILOT_ROOT")
    if root and args:
        name = Path(args[-1].strip('"')).name
        stem = name[:-4] if name.lower().endswith(".bcf") else name
        trust = project_trust(os.environ.get("LATEX_COPILOT_TRUST", ""), root)
        found = outside(Path(root), Path.cwd() / f"{stem}.fls", allowed=trust.get("files", []))
        if found:
            files = ", ".join(p for _, p in found[:3]) + (" …" if len(found) > 3 else "")
            print(f"Latex-Copilot didn't run {tool}, which can download from the internet, because the document "
                  f"read files outside its project folder: {files}", file=sys.stderr)
            return 3
    exe = shutil.which(tool)
    if not exe:
        print(f"{tool} isn't installed", file=sys.stderr)
        return 127
    return subprocess.call([exe, *args])


if __name__ == "__main__":
    sys.exit(guard(sys.argv[1:]))
