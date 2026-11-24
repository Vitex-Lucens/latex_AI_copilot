#!/usr/bin/env python3
"""
PDF import, step 1: take a PDF apart into the pieces Claude needs to rebuild it as LaTeX.

The browser reads the PDF's text layer with pdf.js (static/pdf-extract.js): every text run with its exact
position, size, direction and font. This module adds everything that needs rendering, using Ghostscript,
and writes into the project folder:

  import/pages/page-NN.png       each original page as it looks (100 dpi), so Claude can see the layout
  import/extraction.md           measured layout, fonts and styles, then every page's text in reading order
  import/analysis.json           per-page numbers the app uses to plan the conversion
  figures/fig-NN-K.png           figure K of page NN, rendered with its text removed
  figures/fig-NN-K.tex           that figure as two layers: the image, with its labels on top as real text
  figures/original-graphics.pdf  the whole PDF with all text removed (vector): page backgrounds, re-crops
  layers/page-NN.tex             a positioned copy of page NN: its graphics with every text line placed exactly

Coordinates are PDF points (bp, 1/72 inch). Internally y points UP from the bottom of the page, as in PDF
and TikZ. Only the Python standard library is used, plus the `gs` (Ghostscript) command.

For testing:  python3 -m tex_annotator.pdf_import original.pdf text.json out-folder
(text.json is what static/pdf-extract.js returns.)
"""

import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import zlib
from array import array
from collections import Counter, defaultdict
from pathlib import Path

PAGE_DPI = 100      # page images for Claude, and the render pair used to sample text colours
REGION_DPI = 36     # text-free render used to find figures (1 pixel = 2 pt)
FIGURE_DPI = 300    # figure images
PHOTO_DPI = 200     # pages whose figures are all photographs (PNG files of photos get big)
HIRES_DPI = 150     # page images of scanned pages, for reading the text
BATCH = 8           # pages per Ghostscript run, so the temporary renders stay small


class PdfImportError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Ghostscript and images
# --------------------------------------------------------------------------- #

def gs_path() -> str | None:
    """Ghostscript: `gs` on macOS/Linux, `gswin64c` on Windows (whose installer doesn't add it to PATH)."""
    found = shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c")
    if found or sys.platform != "win32":
        return found
    for base in {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")} - {""}:
        hits = sorted(Path(base).glob("gs/gs*/bin/gswin64c.exe")) + sorted(Path(base).glob("gs/gs*/bin/gswin32c.exe"))
        if hits:
            return str(hits[-1])                            # the newest version
    return shutil.which("rungs")                            # TeX Live's own Ghostscript on Windows


GS_MISSING = ("Importing a PDF needs Ghostscript. Windows: https://ghostscript.com/releases/gsdnld.html (or run install.ps1 again) · "
              "Ubuntu/Debian: sudo apt install ghostscript · macOS: brew install ghostscript (MacTeX already includes it).")


def run_gs(args: list[str], timeout: int = 1200):
    exe = gs_path()
    if not exe:
        raise PdfImportError(GS_MISSING)
    p = subprocess.run([exe, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dUseCropBox", *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise PdfImportError("Ghostscript couldn't process the PDF:\n" + (p.stderr or p.stdout).strip()[-1500:])


def render_ppm(pdf: Path, out: Path, tag: str, dpi: int, first: int, last: int, filter_text=False) -> list[Path]:
    """Render pages first..last (1-based) to PPM files; returns one path per page.
    filter_text uses Ghostscript's render-time text filter. It misses text on pages with transparency,
    so text-free renders normally come from the text-free PDF instead (see make_graphics_pdf)."""
    args = [f"-r{dpi}", "-sDEVICE=ppmraw", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
            f"-dFirstPage={first}", f"-dLastPage={last}", "-o", str(out / f"{tag}-%04d.ppm")]
    if filter_text:
        args.append("-dFILTERTEXT")
    run_gs(args + [str(pdf)])
    return [out / f"{tag}-{i:04d}.ppm" for i in range(1, last - first + 2)]


def make_graphics_pdf(pdf: Path, out: Path) -> bool:
    """The PDF with all its text removed, as vector graphics. Returns False if Ghostscript can't write it."""
    try:
        run_gs(["-sDEVICE=pdfwrite", "-dFILTERTEXT", "-dAutoRotatePages=/None", "-dPassThroughJPEGImages=true",
                "-dPassThroughJPXImages=true", "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false",
                "-dDownsampleMonoImages=false", "-dAutoFilterColorImages=false", "-dAutoFilterGrayImages=false",
                "-dColorImageFilter=/FlateEncode", "-dGrayImageFilter=/FlateEncode", "-o", str(out), str(pdf)])
        return out.exists() and out.stat().st_size > 0
    except PdfImportError:
        out.unlink(missing_ok=True)
        return False


PPM_HEADER = re.compile(rb"P6\s+(?:#[^\n]*\n\s*)*(\d+)\s+(\d+)\s+\d+\s")


class Raster:
    """A PPM render in memory, with conversion from PDF points (y up) to pixels (y down)."""

    def __init__(self, path: Path, page_w: float, page_h: float):
        data = Path(path).read_bytes()
        m = PPM_HEADER.match(data)
        if not m:
            raise PdfImportError(f"Unexpected render format: {path}")
        self.w, self.h, self.off, self.data = int(m.group(1)), int(m.group(2)), m.end(), data
        self.kx, self.ky, self.page_h = self.w / page_w, self.h / page_h, page_h

    def box(self, x0, y0, x1, y1):
        """PDF-point box -> pixel box (x0, y0, x1, y1), y down, clamped to the image."""
        return (max(0, int(x0 * self.kx)), max(0, int((self.page_h - y1) * self.ky)),
                min(self.w, int(math.ceil(x1 * self.kx))), min(self.h, int(math.ceil((self.page_h - y0) * self.ky))))

    def rgb(self, x, y):
        i = self.off + (y * self.w + x) * 3
        d = self.data
        return d[i], d[i + 1], d[i + 2]

    def row(self, y, x0=0, x1=None):
        x1 = self.w if x1 is None else x1
        i = self.off + (y * self.w + x0) * 3
        return self.data[i:i + (x1 - x0) * 3]


def _up_filter(row: bytes, prev: bytes, mask: int) -> bytes:
    """PNG 'Up' filter, byte-wise (row - prev) mod 256, done with big-integer lanes (fast in pure Python)."""
    n = len(row)
    a = bytearray(2 * n)
    a[1::2] = row
    b = bytearray(2 * n)
    b[1::2] = prev
    return ((int.from_bytes(a, "big") | mask) - int.from_bytes(b, "big")).to_bytes(2 * n, "big")[1::2]


def write_png(path: Path, width: int, height: int, rows, dpi: float):
    """Write RGB rows (bytes of 3*width each) as a PNG with its resolution recorded."""
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    comp, parts, prev = zlib.compressobj(6), [], None
    mask = int.from_bytes(b"\x01\x00" * (width * 3), "big")
    for row in rows:
        parts.append(comp.compress(b"\x00" + row if prev is None else b"\x02" + _up_filter(row, prev, mask)))
        prev = row
    parts.append(comp.flush())
    per_m = int(round(dpi / 0.0254))
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n"
                           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                           + chunk(b"pHYs", struct.pack(">IIB", per_m, per_m, 1))
                           + chunk(b"IDAT", b"".join(parts)) + chunk(b"IEND", b""))


def png_from_raster(r: Raster, path: Path, dpi: float, box=None):
    x0, y0, x1, y1 = box or (0, 0, r.w, r.h)
    write_png(path, x1 - x0, y1 - y0, (r.row(y, x0, x1) for y in range(y0, y1)), dpi)


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #

SUBSET = re.compile(r"^[A-Z]{6}\+")
MATH_FONT = re.compile(r"cmmi|cmsy|cmex|cmbsy|msam|msbm|lmmath|math|eufm|eusm|rsfs|stmary|esint|wasy|txsy|txex|"
                       r"pxsy|pxex|mtsy|mtex|stixgeneral|stixsize|xits", re.I)
SYMBOL_FONT = re.compile(r"^symbol|wingding|webding|dingbat|zapf|marlett|mtextra|fontawesome|segoeuisymbol", re.I)
MONO_FONT = re.compile(r"mono|courier|consol|menlo|monaco|cmtt|sftt|lmtt|inconsolata|sourcecode|firacode|"
                       r"lucidaconsole|andale|typewriter|cursor", re.I)
SANS_FONT = re.compile(r"sans|arial|helvet|calibri|verdana|tahoma|segoe|roboto|opensans|lato|montserrat|gill|futura|"
                       r"frutiger|myriad|univers|avenir|franklin|trebuchet|centurygothic|cmss|lmss|sfss|nimbussan|"
                       r"heros|arimo|carlito|candara|corbel|aptos|inter\b|ubuntu|poppins|raleway|nunito|cabin|"
                       r"barlow|worksans|bahnschrift|adventor", re.I)
SERIF_FONT = re.compile(r"times|roman|serif|georgia|garamond|cambria|palatino|antiqua|minion|baskerville|caslon|"
                        r"charter|cmr|cmbx|cmti|cmsl|cmcsc|cmb\d|lmroman|sfrm|sfbx|sfti|sfsl|sfcc|termes|pagella|"
                        r"schola|bonum|libertin|tinos|caladea|century|constantia|bookman|didot|bodoni|utopia|"
                        r"charis|crimson|merriweather|playfair|sabon|perpetua|goudy|cochin|hoefler|literata|lora", re.I)
BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi|medi(?:um)?\b|-bd\b|bd$|cmbx|cmb\d|sfbx|cmssbx", re.I)
ITALIC_FONT = re.compile(r"italic|oblique|ital\b|-it\b|it$|cmti|cmsl|cmbxti|sfti|sfsl|slant|kursiv", re.I)
SMALLCAPS_FONT = re.compile(r"cmcsc|sfcc|smallcap|caps\b|-sc\b", re.I)


class Font:
    __slots__ = ("name", "kind", "bold", "italic", "smallcaps")

    def __init__(self, raw: str):
        name = SUBSET.sub("", raw or "").strip() or "unknown"
        squash = re.sub(r"[\s_]", "", name)
        self.name = name
        if SYMBOL_FONT.search(squash):
            self.kind = "symbol"
        elif MATH_FONT.search(squash):
            self.kind = "math"
        elif MONO_FONT.search(squash):
            self.kind = "mono"
        elif re.search(r"sansserif", squash, re.I) or (SANS_FONT.search(squash) and not re.search(r"serif", squash, re.I)):
            self.kind = "sans"
        elif SERIF_FONT.search(squash):
            self.kind = "serif"
        else:
            self.kind = "?"
        self.bold = bool(BOLD_FONT.search(squash))
        self.italic = bool(ITALIC_FONT.search(squash)) and self.kind != "math"
        self.smallcaps = bool(SMALLCAPS_FONT.search(squash))


# Closest freely available LaTeX fonts, tried in order; only installed packages are suggested.
FONT_MATCHES = [
    (r"times|tinos|termes|nimbusrom|liberationserif|stixgeneral", "Times", ["tgtermes", "newtxtext", "mathptmx"]),
    (r"arial|helvet|arimo|liberationsans|nimbussan|heros", "Helvetica/Arial", ["tgheros", "helvet"]),
    (r"calibri|carlito", "Calibri", ["carlito", "tgheros"]),
    (r"cambria|caladea", "Cambria", ["caladea", "tgtermes"]),
    (r"georgia", "Georgia", ["tgschola", "tgtermes"]),
    (r"palatino|pagella|bookantiqua", "Palatino", ["tgpagella", "mathpazo"]),
    (r"garamond", "Garamond", ["ebgaramond", "tgpagella"]),
    (r"bookman|bonum", "Bookman", ["tgbonum"]),
    (r"century|schola", "Century Schoolbook", ["tgschola"]),
    (r"courier|cursor", "Courier", ["tgcursor", "courier"]),
    (r"lmroman|lmsans|lmmono|lmmath|^cm[a-z]|^sf[a-z]{2}\d", "Computer/Latin Modern (LaTeX's own font)", ["lmodern"]),
    (r"dejavusans(?!mono)", "DejaVu Sans", ["dejavu", "tgheros"]),
    (r"dejavuserif", "DejaVu Serif", ["dejavu", "tgtermes"]),
    (r"roboto", "Roboto", ["roboto", "tgheros"]),
    (r"opensans", "Open Sans", ["opensans", "tgheros"]),
    (r"lato", "Lato", ["lato", "tgheros"]),
    (r"sourcesans", "Source Sans", ["sourcesanspro", "tgheros"]),
    (r"libertin", "Linux Libertine", ["libertine", "tgtermes"]),
    (r"consol|menlo|monaco|inconsolata|sourcecode|dejavusansmono", "a monospace font", ["inconsolata", "sourcecodepro", "lmodern"]),
    (r"verdana|tahoma|segoe|aptos|inter|ubuntu|montserrat|frutiger|myriad|avenir|gill|futura|franklin|trebuchet|"
     r"poppins|raleway|nunito|noto", "a modern sans-serif", ["tgheros", "helvet"]),
]

FONT_PACKAGES = ["lmodern", "tgtermes", "tgheros", "tgpagella", "tgschola", "tgbonum", "tgcursor", "tgadventor",
                 "mathptmx", "helvet", "courier", "newtxtext", "newtxmath", "mathpazo", "carlito", "caladea",
                 "ebgaramond", "libertine", "dejavu", "roboto", "opensans", "lato", "sourcesanspro", "inconsolata",
                 "sourcecodepro", "fontawesome5", "fontspec"]
OTHER_PACKAGES = ["geometry", "tikz", "pgfplots", "tcolorbox", "fancyhdr", "titlesec", "enumitem", "tabularx",
                  "booktabs", "multirow", "colortbl", "makecell", "longtable", "xcolor", "setspace", "parskip",
                  "multicol", "caption", "float", "wrapfig", "hyperref", "microtype", "textpos", "eso-pic", "pdfpages",
                  "siunitx", "mathtools", "amssymb", "ragged2e", "lastpage", "csquotes", "babel"]


def installed_packages() -> set[str] | None:
    """LaTeX packages installed here (from the list above). None for MiKTeX, which installs packages on first use."""
    kpse = shutil.which("kpsewhich")
    if not kpse:
        return set()
    try:
        ver = subprocess.run([kpse, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=30).stdout
        if "miktex" in ver.lower():
            return None
        out = subprocess.run([kpse, *[p + ".sty" for p in FONT_PACKAGES + OTHER_PACKAGES]], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=60).stdout
    except Exception:  # noqa: BLE001
        return set()
    return {Path(line.strip()).stem for line in out.splitlines() if line.strip()}


def font_suggestion(name: str, have: set[str] | None) -> str:
    squash = re.sub(r"[\s_-]", "", name)
    for pat, label, pkgs in FONT_MATCHES:
        if re.search(pat, squash, re.I):
            ok = [p for p in pkgs if p in have] if have else pkgs
            return f"{label} → {ok[0]}" if ok else f"{label}: none of {', '.join(pkgs)} is installed"
    return ""


# --------------------------------------------------------------------------- #
# Text runs and segments
# --------------------------------------------------------------------------- #

PUA_BULLETS = {"\uf0b7": "•", "\uf0a7": "▪", "\uf0d8": "➢", "\uf076": "❖", "\uf0fc": "✓", "\uf0a8": "□",
               "\uf06e": "■", "\uf0e8": "➔", "\uf0d7": "▸"}
LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}


def clean_text(s: str) -> str:
    for table in (LIGATURES, PUA_BULLETS):
        for k, v in table.items():
            s = s.replace(k, v)
    return "".join(c for c in s if c >= " " or c == "\t").replace("\t", " ")


class Run:
    """One pdf.js text item: a stretch of text in one font, along one direction."""
    __slots__ = ("text", "x", "y", "length", "size", "ux", "uy", "font", "box", "horizontal", "x0", "x1",
                 "color", "seen", "script", "link", "page")

    def __init__(self, item, page_h, fonts, page):
        text, x, y_down, length, size, dx, dy, fid = item
        self.text = clean_text(text)
        self.x, self.y = float(x), page_h - float(y_down)        # baseline origin, y up
        self.length, self.size = max(0.0, float(length)), float(size)
        self.ux, self.uy = float(dx), -float(dy)                 # text direction, y up
        self.font = fonts.get(fid) or Font("")
        self.horizontal = self.ux > 0.985
        vx, vy = -self.uy, self.ux                               # "up" for the glyphs
        asc, desc = 0.72 * self.size, 0.22 * self.size
        xs = [self.x + self.ux * t + vx * h for t in (0, self.length) for h in (-desc, asc)]
        ys = [self.y + self.uy * t + vy * h for t in (0, self.length) for h in (-desc, asc)]
        self.box = (min(xs), min(ys), max(xs), max(ys))
        self.x0, self.x1 = self.x, self.x + self.length
        self.color = None      # "#RRGGBB" sampled from the render; None = black
        self.seen = 1.0        # share of the glyph box that changes when the text is drawn (0 = invisible text)
        self.script = ""       # "sup" / "sub"
        self.link = None
        self.page = page

    @property
    def angle(self) -> float:
        return math.degrees(math.atan2(self.uy, self.ux))


def style_key(r: Run) -> tuple:
    f = r.font
    return (f.name, f.kind, f.bold, f.italic, round(r.size * 2) / 2, r.color)


def dominant(runs, key):
    c = Counter()
    for r in runs:
        c[key(r)] += max(1, len(r.text.strip()))
    return c.most_common(1)[0][0]


def join_runs(runs) -> str:
    out, prev = [], None
    for r in runs:
        t = r.text
        if prev is not None and out:
            gap = r.x0 - prev.x1 if r.horizontal else 0
            if gap > 0.12 * max(r.size, prev.size) and not out[-1].endswith(" ") and not t.startswith(" "):
                out.append(" ")
        out.append(t)
        prev = r
    return re.sub(r"\s+", " ", "".join(out)).strip()


class Seg:
    """Runs on one baseline without a wide gap: a phrase, a table cell, one column's line, a label."""
    __slots__ = ("runs", "x0", "x1", "y", "size", "rotated", "role", "owner", "col", "line_id", "text", "box", "shade",
                 "eq")

    def __init__(self, runs, rotated=False, line_id=0):
        self.runs = runs
        self.rotated = rotated
        self.shade = None    # fill colour of a band behind this line
        self.eq = False      # part of an equation laid out as text (e.g. a fraction with a rule)
        self.role = ""       # "label" (on a figure) / "running" (header, footer) / "box" (inside a box)
        self.owner = None
        self.col = 0
        self.line_id = line_id
        self.refresh()

    def refresh(self):
        rs = self.runs
        self.x0 = min(r.box[0] for r in rs) if self.rotated else min(r.x0 for r in rs)
        self.x1 = max(r.box[2] for r in rs) if self.rotated else max(r.x1 for r in rs)
        self.y = max(rs, key=lambda r: (not r.script, len(r.text.strip()))).y
        self.size = dominant(rs, lambda r: round(r.size * 2) / 2)
        self.text = join_runs(rs)
        self.box = (min(r.box[0] for r in rs), min(r.box[1] for r in rs),
                    max(r.box[2] for r in rs), max(r.box[3] for r in rs))

    @property
    def words(self):
        return len(re.findall(r"\w+", self.text))

    @property
    def center(self):
        b = self.box
        return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


class Line:
    __slots__ = ("runs", "y", "size", "segs", "id")

    def __init__(self, run):
        self.runs, self.y, self.size, self.segs, self.id = [run], run.y, run.size, [], 0


def build_segments(runs: list[Run]) -> tuple[list[Line], list[Seg]]:
    horiz = sorted((r for r in runs if r.horizontal), key=lambda r: (-r.y, r.x))
    lines: list[Line] = []
    for r in horiz:
        host = None
        for ln in reversed(lines[-30:]):
            if abs(ln.y - r.y) <= 0.3 * min(ln.size, r.size) + 0.3:
                host = ln
                break
        if host:
            host.runs.append(r)
            host.size = max(host.size, r.size)
        else:
            lines.append(Line(r))
    for ln in lines:
        ln.size = dominant(ln.runs, lambda r: round(r.size * 2) / 2)

    # Superscripts and subscripts: smaller text just above/below a line, touching one of its runs.
    for small in lines:
        if len(small.runs) > 4:
            continue
        for r in list(small.runs):
            best = None
            for ln in lines:
                if ln is small or r.size > 0.86 * ln.size:
                    continue
                dy = r.y - ln.y
                if not (-0.45 * ln.size <= dy <= 0.7 * ln.size) or abs(dy) < 0.1 * ln.size:
                    continue
                if any(-2.5 <= r.x0 - q.x1 <= 1.5 or -1.5 <= q.x0 - r.x1 <= 2.5 for q in ln.runs):
                    if best is None or abs(dy) < abs(best[1]):
                        best = (ln, dy)
            if best:
                small.runs.remove(r)
                r.script = "sup" if best[1] > 0 else "sub"
                best[0].runs.append(r)
    lines = [ln for ln in lines if ln.runs]

    segs: list[Seg] = []
    for i, ln in enumerate(lines):
        ln.id = i + 1
        ln.runs.sort(key=lambda r: r.x0)
        groups, cur = [], [ln.runs[0]]
        for r in ln.runs[1:]:
            if r.x0 - max(q.x1 for q in cur[-3:]) > max(1.0 * ln.size, 6.0):
                groups.append(cur)
                cur = [r]
            else:
                cur.append(r)
        groups.append(cur)
        for g in groups:
            s = Seg(g, line_id=ln.id)
            ln.segs.append(s)
            segs.append(s)

    # Rotated text: merge runs that continue each other along the same direction.
    rot = sorted((r for r in runs if not r.horizontal), key=lambda r: (round(r.angle), r.x, r.y))
    used = set()
    for i, r in enumerate(rot):
        if i in used:
            continue
        group = [r]
        used.add(i)
        for j in range(i + 1, len(rot)):
            q = rot[j]
            if j in used or abs(q.angle - r.angle) > 2:
                continue
            last = group[-1]
            ex, ey = last.x + last.ux * last.length, last.y + last.uy * last.length
            along = (q.x - ex) * last.ux + (q.y - ey) * last.uy
            perp = abs(-(q.x - ex) * last.uy + (q.y - ey) * last.ux)
            if -0.5 * q.size <= along <= 0.6 * q.size and perp < 0.3 * q.size:
                group.append(q)
                used.add(j)
        segs.append(Seg(group, rotated=True))
    return lines, segs


# --------------------------------------------------------------------------- #
# Rendering-based analysis: colours, visible text, figure regions
# --------------------------------------------------------------------------- #

def sample_runs(runs: list[Run], rw: Raster, rn: Raster):
    """Colour and visibility of each run, from the render with text vs. the one without."""
    dw, dn = rw.data, rn.data
    for r in runs:
        x0, y0, x1, y1 = rw.box(*r.box)
        if x1 <= x0 or y1 <= y0:
            r.seen = 0.0
            continue
        step = 1 if (x1 - x0) * (y1 - y0) < 900 else 2
        total = changed = 0
        hits = []
        for py in range(y0, y1, step):
            bw, bn = rw.off + py * rw.w * 3, rn.off + py * rn.w * 3
            for px in range(x0, x1, step):
                i, j = bw + px * 3, bn + px * 3
                a0, a1, a2 = dw[i], dw[i + 1], dw[i + 2]
                d = max(abs(a0 - dn[j]), abs(a1 - dn[j + 1]), abs(a2 - dn[j + 2]))
                total += 1
                if d > 48:
                    changed += 1
                    hits.append((d, a0, a1, a2))
        r.seen = changed / total if total else 0.0
        if len(hits) >= 6:      # tiny glyphs (dots, commas) are too blurred to tell their colour
            hits.sort(reverse=True)
            top = hits[:max(3, len(hits) // 10)]
            c = tuple(round(sum(h[k] for h in top) / len(top)) for k in (1, 2, 3))
            r.color = None if max(c) - min(c) < 40 and max(c) < 120 else "#%02X%02X%02X" % c  # near-black → black


def snap_colors(runs: list[Run]):
    """Merge sampled colours that are really one colour (sampling noise), weighted by text length."""
    weight = Counter()
    for r in runs:
        if r.color:
            weight[r.color] += max(1, len(r.text))
    groups: list[tuple] = []
    for col, _ in weight.most_common():
        rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
        for centre, members in groups:
            if sum((a - b) ** 2 for a, b in zip(rgb, centre)) < 45 ** 2:
                members[col] = rgb
                break
        else:
            groups.append((rgb, {col: rgb}))
    remap = {}
    for _, members in groups:
        # the most saturated sample is the least affected by anti-aliasing, so closest to the true colour
        best = max(members, key=lambda k: weight[k] * (1 + (max(members[k]) - min(members[k])) / 64))
        for k in members:
            remap[k] = best
    for r in runs:
        if r.color:
            r.color = remap.get(r.color, r.color)


class Region:
    __slots__ = ("box", "kind", "ink", "fill", "fill_share", "colors", "segs", "name", "file", "words",
                 "photo", "border", "blocks")

    def __init__(self, box, kind):
        self.box, self.kind = box, kind
        self.ink = self.fill_share = 0.0
        self.fill = self.border = None
        self.colors = 0
        self.segs: list[Seg] = []
        self.blocks = []
        self.name = self.file = ""
        self.words = 0
        self.photo = False

    @property
    def w(self):
        return self.box[2] - self.box[0]

    @property
    def h(self):
        return self.box[3] - self.box[1]


def _split_component(mask, lab, w, cid, box, count, depth=0) -> list[tuple]:
    """Split a connected area at a clear empty band (rows or columns) that only the dilation bridged, e.g. two
    boxes stacked a few points apart. Only big pieces are split off, so a logo's letters stay together."""
    x0, y0, x1, y1 = box
    if depth > 3 or (x1 - x0 < 24 and y1 - y0 < 24):
        return [(x0, y0, x1, y1, count)]
    rows = bytearray(y1 - y0 + 1)
    cols = bytearray(x1 - x0 + 1)
    for y in range(y0, y1 + 1):
        base = y * w
        for x in range(x0, x1 + 1):
            i = base + x
            if mask[i] and lab[i] == cid:
                rows[y - y0] = 1
                cols[x - x0] = 1

    def cut(occ, lo):
        """Pieces along one direction, separated by empty gaps of 2+ px, each piece 12+ px long."""
        runs, k = [], 0
        while k < len(occ):
            if not occ[k]:
                k += 1
                continue
            a = k
            while k < len(occ) and (occ[k] or (k + 1 < len(occ) and occ[k + 1])):
                k += 1                                      # one-pixel gaps don't count
            runs.append((lo + a, lo + k - 1))
        merged = []
        for a, b in runs:                                   # pieces too small to stand alone join their neighbour
            if merged and (b - a < 11 or merged[-1][1] - merged[-1][0] < 11):
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        return merged

    for along_rows in (True, False):
        pieces = cut(rows, y0) if along_rows else cut(cols, x0)
        if len(pieces) < 2:
            continue
        tight = []
        for a, b in pieces:
            sub_box = (x0, a, x1, b) if along_rows else (a, y0, b, y1)
            # tighten to this piece's own pixels
            mnx = mny = 1 << 30
            mxx = mxy = -1
            n = 0
            for y in range(sub_box[1], sub_box[3] + 1):
                base = y * w
                for x in range(sub_box[0], sub_box[2] + 1):
                    i = base + x
                    if mask[i] and lab[i] == cid:
                        n += 1
                        mnx, mxx, mny, mxy = min(mnx, x), max(mxx, x), min(mny, y), max(mxy, y)
            if n:
                tight.append((mnx, mny, mxx, mxy, n))
        # Only solid panels are split apart (filled boxes, photos). A chart or a diagram has white space of its
        # own between its parts, and must stay one figure.
        if any(n < 0.6 * (c - a + 1) * (d - b + 1) for a, b, c, d, n in tight):
            return [(x0, y0, x1, y1, count)]
        out = []
        for a, b, c, d, n in tight:
            out += _split_component(mask, lab, w, cid, (a, b, c, d), n, depth + 1)
        return out
    return [(x0, y0, x1, y1, count)]


def find_regions(r: Raster, page_w: float, page_h: float) -> list[Region]:
    """Connected areas of ink on the text-free render: figures, boxes, rules, small marks."""
    w, h, d, off = r.w, r.h, r.data, r.off
    border = Counter()
    for x in range(0, w, 2):
        border[r.rgb(x, 0)] += 1
        border[r.rgb(x, h - 1)] += 1
    for y in range(0, h, 2):
        border[r.rgb(0, y)] += 1
        border[r.rgb(w - 1, y)] += 1
    bg, n = border.most_common(1)[0]
    if n < 0.3 * sum(border.values()):
        bg = (255, 255, 255)
    br, bgg, bb = bg
    thr = 12
    mask = bytearray(w * h)
    for y in range(h):
        base = off + y * w * 3
        row = d[base:base + w * 3]
        mrow = y * w
        for x in range(w):
            i = 3 * x
            if abs(row[i] - br) > thr or abs(row[i + 1] - bgg) > thr or abs(row[i + 2] - bb) > thr:
                mask[mrow + x] = 1
    # Dilate by 3 px (6 pt) so the parts of one figure join up, then label the connected areas.
    R = 3
    tmp = bytearray(w * h)
    for y in range(h):
        row = mask[y * w:(y + 1) * w]
        x = row.find(1)
        while x != -1:
            a, b = max(0, x - R), min(w, x + R + 1)
            tmp[y * w + a:y * w + b] = b"\x01" * (b - a)
            x = row.find(1, x + 1)
    dil = bytearray(w * h)
    for x in range(w):
        col = tmp[x::w]
        y = col.find(1)
        while y != -1:
            a, b = max(0, y - R), min(h, y + R + 1)
            dil[x + a * w:x + b * w:w] = b"\x01" * (b - a)
            y = col.find(1, y + 1)
    lab = array("i", bytes(4 * w * h))                       # component id of each pixel
    comps = []
    start = dil.find(1)
    while start != -1:
        if not lab[start]:
            cid = len(comps) + 1
            lab[start] = cid
            stack = [start]
            mnx = mny = 1 << 30
            mxx = mxy = -1
            count = 0
            while stack:
                i = stack.pop()
                y, x = divmod(i, w)
                if mask[i]:
                    count += 1
                    if x < mnx: mnx = x
                    if x > mxx: mxx = x
                    if y < mny: mny = y
                    if y > mxy: mxy = y
                for j in ((i - 1) if x > 0 else -1, (i + 1) if x < w - 1 else -1,
                          (i - w) if y > 0 else -1, (i + w) if y < h - 1 else -1):
                    if j >= 0 and dil[j] and not lab[j]:
                        lab[j] = cid
                        stack.append(j)
            comps += _split_component(mask, lab, w, cid, (mnx, mny, mxx, mxy), count)
        start = dil.find(1, start + 1)

    sx, sy = page_w / w, page_h / h
    bgq = (br >> 4, bgg >> 4, bb >> 4)
    regions = []
    for mnx, mny, mxx, mxy, count in comps:
        if count == 0:
            continue
        box = (mnx * sx, page_h - (mxy + 1) * sy, (mxx + 1) * sx, page_h - mny * sy)
        bw, bh = box[2] - box[0], box[3] - box[1]
        if bw * bh < 16 or (bw < 3 and bh < 3):
            continue
        thin, long_ = min(bw, bh), max(bw, bh)
        if thin <= 5 and long_ >= 12 and long_ >= 4 * thin:
            kind = "hrule" if bw > bh else "vrule"
        elif bw <= 18 and bh <= 18:
            kind = "mark"
        else:
            kind = "region"
        reg = Region(box, kind)
        if kind == "region":
            cols = Counter()
            for y in range(mny, mxy + 1):
                for x in range(mnx, mxx + 1):
                    c = r.rgb(x, y)
                    cols[(c[0] >> 4, c[1] >> 4, c[2] >> 4)] += 1
            total = sum(cols.values())
            q, fill_n = cols.most_common(1)[0]
            reg.ink = count / max(1, (mxx - mnx + 1) * (mxy - mny + 1))
            reg.fill_share = fill_n / max(1, total)
            reg.colors = len(cols)
            reg.photo = reg.colors > 350
            if q != bgq:
                acc = [0, 0, 0, 0]
                for y in range(mny, mxy + 1):
                    for x in range(mnx, mxx + 1):
                        c = r.rgb(x, y)
                        if (c[0] >> 4, c[1] >> 4, c[2] >> 4) == q:
                            acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 1
                reg.fill = "#%02X%02X%02X" % tuple(round(acc[k] / acc[3]) for k in range(3))
                ring = Counter()
                for x in range(mnx, mxx + 1):
                    ring[r.rgb(x, mny)] += 1
                    ring[r.rgb(x, mxy)] += 1
                for y in range(mny, mxy + 1):
                    ring[r.rgb(mnx, y)] += 1
                    ring[r.rgb(mxx, y)] += 1
                # The border is thinner than a pixel here, so it shows up blended: the ring pixels furthest from
                # the fill colour are the closest to the real border colour.
                fr, fg, fb = (int(reg.fill[k:k + 2], 16) for k in (1, 3, 5))
                far = sorted(ring.elements(), key=lambda c: -max(abs(c[0] - fr), abs(c[1] - fg), abs(c[2] - fb)))
                top = far[:max(1, len(far) // 10)]
                rc = tuple(round(sum(c[k] for c in top) / len(top)) for k in range(3))
                if max(abs(rc[0] - fr), abs(rc[1] - fg), abs(rc[2] - fb)) > 24:
                    reg.border = "#%02X%02X%02X" % rc
        regions.append(reg)
    return regions


# --------------------------------------------------------------------------- #
# Page structure
# --------------------------------------------------------------------------- #

CAPTION = re.compile(r"^\s*(fig\.?|figure|table|tab\.|chart|graph|image|photo|plate|exhibit|abbildung|tabelle|figura|"
                     r"tabla)\s*[\dIVXivx]+[\d.]*\s*[:.|–—-]", re.I)
MARKER_ONLY = re.compile(r"^(\(?[\dA-Za-z]{1,3}[.)]|\d{1,3}(\.\d{1,3})*\.?|[IVXLC]{1,5}\.?|[•◦▪▫‣⁃●○■□►▸▹➢➤➔✓✔❖∙·*–-])$")
MATH_CHARS = re.compile(r"[∫∑∏√∞≈≤≥≠±∂∇∈∉⊂⊆∪∩⇒⇔∀∃]")
BULLET = re.compile(r"^\s*([•◦▪▫‣⁃●○■□►▸▹➢➤➔✓✔❖∙·\-–—*])\s+")
NUMBERED = re.compile(r"^\s*(\(?(?:\d{1,3}|[a-zA-Z]|[ivxlcdm]{1,6}|[IVXLCDM]{1,6})[.)]|\d{1,3}(?:\.\d{1,3})+\.?)\s+\S")


class Page:
    def __init__(self, n, w, h):
        self.n, self.w, self.h = n, w, h
        self.runs: list[Run] = []
        self.lines: list[Line] = []
        self.segs: list[Seg] = []
        self.regions: list[Region] = []
        self.figures: list[Region] = []
        self.boxes: list[Region] = []
        self.rules: list[Region] = []
        self.marks: list[Region] = []
        self.background = None
        self.scan = ""           # "", "no-text", "invisible-text", "garbled"
        self.gutters: list[tuple] = []
        self.items = []          # reading order: Blocks and (kind, object) pairs
        self.words = 0
        self.links = []
        self.designed = []       # why this looks like a designed page


def inside(pt, box, pad=0.0):
    return box[0] - pad <= pt[0] <= box[2] + pad and box[1] - pad <= pt[1] <= box[3] + pad


def box_gap(a, b):
    return max(0.0, max(a[0], b[0]) - min(a[2], b[2]), max(a[1], b[1]) - min(a[3], b[3]))


def union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def overlaps(a, b, pad=0.0):
    return a[0] - pad < b[2] and b[0] - pad < a[2] and a[1] - pad < b[3] and b[1] - pad < a[3]


def classify_regions(pg: Page, body: float):
    """Which ink regions are figures, boxes behind text or the page background; give figures their labels."""
    area = pg.w * pg.h
    for reg in pg.regions:
        if reg.kind in ("hrule", "vrule"):
            pg.rules.append(reg)
            continue
        if reg.kind == "mark":
            pg.marks.append(reg)
            continue
        inner = [s for s in pg.segs if not s.role and inside(s.center, reg.box, 2)]
        reg.words = sum(s.words for s in inner)
        per_line = Counter(s.line_id for s in inner if not s.rotated)
        # A ruled table: several rows of cells, mostly words (a chart's labels are mostly numbers), thin lines.
        letters = sum(len(re.findall(r"[^\W\d_]{3,}", s.text)) for s in inner)
        tabular = (sum(1 for v in per_line.values() if v >= 2) >= 2 and letters >= 0.5 * max(1, reg.words)
                   and reg.ink <= 0.15)
        wordy = reg.words >= 12 and reg.words / max(1, len(inner)) >= 3
        if reg.w * reg.h >= 0.6 * area:
            reg.kind = "background"
            pg.background = reg
            continue
        lines_in = {s.line_id for s in inner if not s.rotated}
        if inner and reg.fill and len(lines_in) == 1 and reg.h <= 2.6 * max(s.size for s in inner):
            for s in inner:            # shading behind one line: a table's header row, a highlighted heading
                s.shade = reg.fill
            continue
        if inner and reg.fill and reg.fill_share >= 0.6:
            reg.kind = "box"
        elif inner and not reg.fill and reg.ink <= 0.25 and (wordy or tabular):
            reg.kind = "frame"
        else:
            reg.kind = "figure"
        reg.segs = inner
        if reg.kind == "figure":
            pg.figures.append(reg)
            for s in inner:
                s.role, s.owner = "label", reg
        else:
            pg.boxes.append(reg)
            for s in inner:
                s.role, s.owner = "box", reg

    def label_like(s: Seg) -> bool:
        if s.rotated:
            return True
        if len(s.text) > 40 or s.size > 1.25 * body or CAPTION.match(s.text):
            return False
        for o in pg.segs:      # the last line of a paragraph? (a long line of the same size right above/below)
            if o is s or o.role or o.rotated or abs(o.size - s.size) > 0.6 or len(o.text) <= 40:
                continue
            if 0 < abs(o.y - s.y) <= 1.6 * s.size and o.x0 < s.x1 and s.x0 < o.x1:
                return False
        return True

    # Labels just outside a figure (axis titles, tick labels) belong to it too.
    for fig in pg.figures:
        grew = True
        while grew:
            grew = False
            for s in pg.segs:
                if not s.role and box_gap(s.box, fig.box) <= max(4.0, 0.9 * s.size) and label_like(s):
                    s.role, s.owner = "label", fig
                    fig.segs.append(s)
                    fig.box = union(fig.box, s.box)
                    grew = True
    # Figures that now overlap become one.
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(pg.figures):
            for b in pg.figures[i + 1:]:
                if overlaps(a.box, b.box, 1):
                    a.box = union(a.box, b.box)
                    a.photo = a.photo or b.photo
                    for s in b.segs:
                        s.owner = a
                    a.segs += b.segs
                    pg.figures.remove(b)
                    merged = True
                    break
            if merged:
                break
    for fig in pg.figures:
        fig.words = sum(s.words for s in fig.segs)
        # a little margin, so hairlines at the edge (an axis frame) aren't clipped
        fig.box = (max(0.0, fig.box[0] - 3), max(0.0, fig.box[1] - 3), min(pg.w, fig.box[2] + 3), min(pg.h, fig.box[3] + 3))


def detect_running(pages: list[Page]):
    """Headers and footers: text in the top/bottom margin that repeats at the same place on many pages."""
    if len(pages) < 2:
        return []
    seen = defaultdict(list)
    for pg in pages:
        for s in pg.segs:
            if s.role or s.rotated:
                continue
            zone = "top" if s.y > 0.87 * pg.h else "bottom" if s.y < 0.11 * pg.h else ""
            if not zone:
                continue
            norm = re.sub(r"\d+", "#", s.text.lower()).strip()
            if re.fullmatch(r"[-–— ]*[ivxlcdm]+[-–— ]*", norm):
                norm = "#"
            xkey = round((s.x0 + s.x1) / 16) if "#" in norm else round(s.x0 / 8)
            seen[(zone, norm, xkey, round(s.y / 4))].append((pg, s))
    need = max(2, math.ceil(0.35 * len(pages)))
    found = []
    for (zone, norm, _, _), hits in seen.items():
        if len({pg.n for pg, _ in hits}) >= need:
            for _, s in hits:
                s.role = "running"
            found.append((zone, norm, hits))
    return found


class Span:
    """A run's horizontal extent, for finding columns."""
    __slots__ = ("x0", "x1", "y", "words")

    def __init__(self, r: Run):
        self.x0, self.x1, self.y = r.x0, r.x1, r.y
        self.words = len(re.findall(r"\w+", r.text))


def find_gutters(segs: list[Seg]) -> list[tuple]:
    """Vertical gaps between text columns (x-ranges with no text), ignoring lines that span columns.
    Works on runs rather than segments: lines of two columns separated by a narrow gap form one segment."""
    hz = [Span(r) for s in segs if not s.rotated for r in s.runs if r.text.strip()]
    if len(hz) < 8:
        return []
    lo, hi = min(s.x0 for s in hz), max(s.x1 for s in hz)
    width = hi - lo
    narrow = [s for s in hz if s.x1 - s.x0 < 0.55 * width]
    if len(narrow) < 8:
        return []
    n = int(width) + 2
    cover = [0] * n
    for s in narrow:
        for k in range(max(0, int(s.x0 - lo)), min(n, int(s.x1 - lo) + 1)):
            cover[k] += 1
    # A few items may cross a gutter: centred captions or headings that span the columns.
    allow = max(1, len(narrow) // 25)
    page_h = max(s.y for s in hz) - min(s.y for s in hz)
    gutters, x = [], 0
    while x < n:
        if cover[x] > allow:
            x += 1
            continue
        start = x
        while x < n and cover[x] <= allow:
            x += 1
        g0, g1 = lo + start, lo + x
        if g1 - g0 < 5 or g0 <= lo + 1 or g1 >= hi - 1:
            continue
        mid = (g0 + g1) / 2
        left = [s for s in narrow if s.x1 <= mid]
        right = [s for s in narrow if s.x0 >= mid]
        if len(left) < 4 or len(right) < 4:
            continue
        g0, g1 = max(s.x1 for s in left), min(s.x0 for s in right)   # the gap between the columns' text
        if g1 - g0 < 5:
            continue
        ll, rl = sum(s.x1 - s.x0 for s in left), sum(s.x1 - s.x0 for s in right)
        if min(ll, rl) < 0.15 * (ll + rl):
            continue
        ly = (min(s.y for s in left), max(s.y for s in left))
        ry = (min(s.y for s in right), max(s.y for s in right))
        ov = min(ly[1], ry[1]) - max(ly[0], ry[0])
        if ov < 25 or ov < 0.4 * min(ly[1] - ly[0], ry[1] - ry[0]):
            continue

        def line_words(side):              # words per line on that side (runs can be single words)
            per = Counter()
            for s in side:
                if max(ly[0], ry[0]) - 1 <= s.y <= min(ly[1], ry[1]) + 1:
                    per[round(s.y)] += s.words
            return sorted(per.values())
        wl, wr = line_words(left), line_words(right)
        if min(len(wl), len(wr)) < 3:
            continue
        # Text columns have long lines; table columns have short cells.
        if min(wl[len(wl) // 2], wr[len(wr) // 2]) < 3 and not (ov >= 0.5 * page_h and min(len(wl), len(wr)) >= 10):
            continue
        gutters.append((g0, g1))
    return gutters


class Block:
    __slots__ = ("kind", "rows", "style", "x0", "x1", "y0", "y1", "align", "indent", "marker", "keep_lines",
                 "math", "caption", "cells", "ncols", "id", "rules")

    def __init__(self, kind, rows):
        self.kind, self.rows = kind, rows
        self.style = None
        self.align = self.marker = self.id = ""
        self.indent = 0.0
        self.keep_lines = self.math = self.caption = False
        self.cells = []          # tables: rows of cells, each a list of segments
        self.ncols = 0
        self.rules = []          # tables: y of the horizontal rules drawn across them
        self.update()

    def update(self):
        segs = [s for row in self.rows for s in row]
        self.x0, self.x1 = min(s.x0 for s in segs), max(s.x1 for s in segs)
        self.y0, self.y1 = min(s.box[1] for s in segs), max(s.box[3] for s in segs)
        self.style = dominant(self.runs, style_key)

    @property
    def runs(self):
        return [r for row in self.rows for s in row for r in s.runs]


def _text_x(seg: Seg) -> float:
    """Where a list item's text starts (after its bullet or number)."""
    rs = seg.runs
    if len(rs) >= 2 and (BULLET.match(rs[0].text.strip() + " x") or re.fullmatch(r"\(?\w{1,4}[.)]", rs[0].text.strip())):
        return rs[1].x0
    m = BULLET.match(seg.text) or NUMBERED.match(seg.text)
    if m:   # bullet and text in one run: estimate from the character share
        return seg.x0 + (seg.x1 - seg.x0) * len(m.group(1)) / max(1, len(seg.text)) + 0.3 * seg.size
    return seg.x0


def _continues(b: Block, row: list[Seg], st: tuple, left: float, right: float) -> bool:
    """Does this row continue paragraph block b (rather than start a new block)?"""
    pst, last = b.style, b.rows[-1]
    if pst[1] != st[1] or abs(pst[4] - st[4]) > 0.6 or pst[5] != st[5]:
        return False
    if pst[2] != st[2] and style_key(last[-1].runs[-1]) != style_key(row[0].runs[0]):
        return False                    # bold changes for the whole line (a heading), not a bold phrase running on
    pitch = last[0].y - row[0].y
    if not 0 < pitch <= 1.6 * max(pst[4], st[4]):
        return False
    if len(b.rows) >= 2 and pitch > (b.rows[-2][0].y - last[0].y) * 1.3 + 1:
        return False
    width = max(1.0, right - left)
    x0, x1 = row[0].x0, row[-1].x1
    mid = (left + right) / 2
    if (abs((x0 + x1) / 2 - mid) < 4 and abs((b.x0 + b.x1) / 2 - mid) < 4 and x0 > left + 8
            and x1 - x0 < 0.85 * width and b.x1 - b.x0 < 0.85 * width):
        return True                                         # centred lines
    if abs(x1 - right) < 3 and abs(b.x1 - right) < 3 and x0 > left + 8 and b.x0 > left + 8:
        return True                                         # right-aligned lines
    if b.marker:                                            # continuation of a list item
        return abs(x0 - _text_x(b.rows[0][0])) <= 3 or abs(x0 - b.rows[0][0].x0) <= 3
    if len(b.rows) == 1:
        if b.rows[0][0].x0 - 4 * st[4] <= x0 < b.rows[0][0].x0 - 4:   # the first line was indented
            b.indent = b.rows[0][0].x0 - x0
            return True
        return abs(x0 - b.rows[0][0].x0) <= 4
    body_left = b.rows[1][0].x0
    if abs(x0 - body_left) <= 4:
        return True
    if x0 > body_left + 6 and last[-1].x1 < right - 0.1 * width:
        return False                                        # indented first line of the next paragraph
    return False


BIG_OPS = re.compile(r"[∫∑∏∮√]")


def _math_row(row: list[Seg]) -> bool:
    """A line of displayed maths: mostly maths-font characters, or a big operator in a short line."""
    if any(s.eq for s in row):                              # part of a fraction set as text
        return True
    runs = [r for s in row for r in s.runs]
    chars = sum(len(r.text.strip()) for r in runs)
    mathy = sum(len(r.text.strip()) for r in runs if r.font.kind == "math" or MATH_CHARS.search(r.text))
    return chars > 0 and (mathy >= 0.3 * chars or (chars <= 80 and any(BIG_OPS.search(r.text) for r in runs)))


def build_blocks(rows: list[list[Seg]], left: float, right: float, body: float) -> list[Block]:
    """Rows (the segments of one line within one column, left to right) -> table and paragraph blocks."""
    width = max(1.0, right - left)
    # Displayed equations: maths rows plus the small rows around them (limits, indices) make one block.
    kind = ["math" if _math_row(row) else "" for row in rows]
    changed = True
    while changed:
        changed = False
        for k, row in enumerate(rows):
            if kind[k] or not all(s.size <= 0.85 * body for s in row) or sum(len(s.text) for s in row) > 8:
                continue
            if any(0 <= j < len(rows) and kind[j] and abs(rows[j][0].y - row[0].y) <= 1.6 * body for j in (k - 1, k + 1)):
                kind[k] = "near"
                changed = True
    for k in range(len(rows)):
        if kind[k] or len(rows[k]) != 1 or len(rows[k][0].runs) < 2:
            continue
        seg = rows[k][0]
        for j in (k - 1, k + 1):
            if not (0 <= j < len(rows)) or len(rows[j]) < 3 or abs(rows[j][0].y - seg.y) > 2.5 * seg.size:
                continue
            anchors = [t.x0 for t in rows[j][1:]]
            cuts = [i for i in range(1, len(seg.runs)) if seg.runs[i].x0 - seg.runs[i - 1].x1 >= 1
                    and any(abs(seg.runs[i].x0 - a) <= 2 for a in anchors)]
            if len(cuts) >= max(1, len(anchors) // 2):
                bounds = [0] + cuts + [len(seg.runs)]
                rows[k] = [Seg(seg.runs[a:b], line_id=seg.line_id) for a, b in zip(bounds, bounds[1:])]
                break
    out: list[Block] = []
    i = 0
    while i < len(rows):
        if kind[i]:
            j = i
            while j + 1 < len(rows) and kind[j + 1] and rows[j][0].y - rows[j + 1][0].y <= 2.2 * body:
                j += 1
            if j > i or kind[i] == "math":
                b = Block("p", rows[i:j + 1])
                b.math = b.keep_lines = True
                out.append(b)
                i = j + 1
                continue
        # Tables: consecutive rows with 2+ cells that line up in columns.
        if len(rows[i]) >= 2:
            j = i
            while j + 1 < len(rows):
                nxt = rows[j + 1]
                if rows[j][0].y - nxt[0].y > 3.2 * max(s.size for s in rows[j]):
                    break
                wrapped = len(nxt) == 1 and j + 2 < len(rows) and len(rows[j + 2]) >= 2 and nxt[0].x0 > rows[i][0].x1
                if len(nxt) >= 2 or wrapped:
                    j += 1
                else:
                    break
            if j > i:
                cols: list[list[float]] = []
                for row in rows[i:j + 1]:
                    for s in row:
                        for c in cols:
                            if s.x0 < c[1] + 3 and c[0] - 3 < s.x1:
                                c[0], c[1] = min(c[0], s.x0), max(c[1], s.x1)
                                break
                        else:
                            cols.append([s.x0, s.x1])
                cols.sort()
                k = 0
                while k + 1 < len(cols):                    # columns that grew into each other are one
                    if cols[k + 1][0] < cols[k][1] + 3:
                        cols[k][1] = max(cols[k][1], cols.pop(k + 1)[1])
                    else:
                        k += 1
                first = [" ".join(s.text for s in row if cols[0][0] - 3 <= s.x0 <= cols[0][1]) for row in rows[i:j + 1]]
                if len(cols) == 2 and all(MARKER_ONLY.match(t.strip()) for t in first):
                    cols = []                               # a list with tab-separated numbers, not a table
                if len(cols) >= 2:
                    b = Block("table", rows[i:j + 1])
                    b.ncols = len(cols)
                    for row in rows[i:j + 1]:
                        cells = [[] for _ in cols]
                        for s in row:
                            ci = min(range(len(cols)), key=lambda c: 0 if cols[c][0] - 3 <= s.x0 <= cols[c][1] else abs(cols[c][0] - s.x0))
                            cells[ci].append(s)
                        b.cells.append(cells)
                    out.append(b)
                    i = j + 1
                    continue
        row = rows[i]
        st = dominant([r for s in row for r in s.runs], style_key)
        text = " ".join(s.text for s in row)
        m = BULLET.match(text) or (NUMBERED.match(text) if st[4] <= body * 1.08 and not st[2] else None)
        prev = out[-1] if out and out[-1].kind == "p" else None
        if prev is not None and not m and _continues(prev, row, st, left, right):
            prev.rows.append(row)
            prev.update()
        else:
            b = Block("p", [row])
            b.marker = m.group(1) if m else ""
            out.append(b)
        i += 1

    for b in out:
        if b.kind != "p" or b.math:
            continue
        n = len(b.rows)
        ends = [r[-1].x1 for r in b.rows]
        starts = [r[0].x0 for r in b.rows]
        mids = [(s + e) / 2 for s, e in zip(starts, ends)]
        full = sum(1 for e in ends[:-1] if e >= right - 0.03 * width - 2)
        if n >= 2 and full >= 0.7 * (n - 1):
            b.align = "justified" if all(abs(e - ends[0]) < 3 for e in ends[:-1]) else "left"
        elif (all(abs(m_ - (left + right) / 2) < 4 for m_ in mids) and starts[0] > left + 8
              and max(e - a for a, e in zip(starts, ends)) < 0.85 * width):
            b.align = "centred"
        elif all(abs(e - right) < 3 for e in ends) and starts[0] > left + 8:
            b.align = "right"
        else:
            b.align = "left"
        short = sum(1 for e in ends[:-1] if e < right - 0.2 * width)
        b.keep_lines = n >= 2 and short >= 0.5 * (n - 1) and b.align != "justified"
        b.math = sum(len(r.text.strip()) for r in b.runs if r.font.kind == "math") >= max(2, 0.3 * sum(len(r.text.strip()) for r in b.runs))
        b.caption = bool(CAPTION.match(b.rows[0][0].text))
    return out


def layout(pg: Page, body: float):
    """Columns and reading order for one page: fills pg.items with blocks, figures, boxes, rotated text and rules."""
    flow = [s for s in pg.segs if not s.role]
    pg.gutters = find_gutters(flow)

    def column_of(x0, x1, tol=0.5):
        for k, (g0, g1) in enumerate(pg.gutters):
            if x1 <= g0 + tol:
                return k
            if x0 < g1 - tol:
                return -1                                   # crosses a gutter: spans columns
        return len(pg.gutters)

    # A segment crossing a gutter at a gap between two of its runs is really two segments.
    for s in list(flow):
        if s.rotated or not pg.gutters or column_of(s.x0, s.x1) != -1:
            continue
        for g0, g1 in pg.gutters:
            cut = next((k for k in range(1, len(s.runs)) if s.runs[k - 1].x1 <= g0 + 2 and s.runs[k].x0 >= g1 - 2), None)
            if cut:
                a, b = Seg(s.runs[:cut], line_id=s.line_id), Seg(s.runs[cut:], line_id=s.line_id)
                for lst in (flow, pg.segs):
                    lst.remove(s)
                    lst += [a, b]
                break

    # Fractions set as text (a short rule with text centred above and below): the lines around them are maths.
    for rule in list(pg.rules):
        if rule.kind != "hrule":
            continue
        ry, rc, rw = (rule.box[1] + rule.box[3]) / 2, (rule.box[0] + rule.box[2]) / 2, rule.w
        above = [s for s in flow if not s.rotated and 1 < s.y - ry < 2.2 * s.size and abs((s.x0 + s.x1) / 2 - rc) < 0.25 * rw]
        below = [s for s in flow if not s.rotated and 1 < ry - s.box[3] + 0.2 * s.size < 1.6 * s.size
                 and abs((s.x0 + s.x1) / 2 - rc) < 0.25 * rw]
        if not above or not below or rw > 0.8 * pg.w:
            continue
        lo_y, hi_y = min(s.y for s in below) - 0.5 * body, max(s.y for s in above) + 0.5 * body
        for t in flow:
            if not t.rotated and lo_y <= t.y <= hi_y:
                t.eq = True
        pg.rules.remove(rule)

    hz = [s for s in flow if not s.rotated]
    left_edge = min((s.x0 for s in hz), default=0.0)
    right_edge = max((s.x1 for s in hz), default=pg.w)
    xs = [left_edge] + [x for g in pg.gutters for x in g] + [right_edge]
    bounds = [(xs[2 * k], xs[2 * k + 1]) for k in range(len(pg.gutters) + 1)]

    # Units in the reading order. Spanning units (across all columns) separate "bands"; inside a band,
    # columns are read one after the other, each top to bottom.
    units = []                                              # (top, column, kind, payload)
    for s in hz:
        s.col = column_of(s.x0, s.x1)
        units.append((s.box[3], s.col, "seg", s))
    for fig in pg.figures:
        units.append((fig.box[3], column_of(fig.box[0], fig.box[2], 6), "figure", fig))
    for box in pg.boxes:
        units.append((box.box[3], column_of(box.box[0], box.box[2], 6), "box", box))
    for s in flow:
        if s.rotated:
            units.append((s.box[3], column_of(s.box[0], s.box[2], 6), "rotated", s))
    span = (right_edge - left_edge) / (len(pg.gutters) + 1)
    for rule in pg.rules:
        if rule.kind == "hrule" and rule.w >= 0.25 * span and not any(
                overlaps(rule.box, o.box, 1) for o in pg.boxes + pg.figures):
            units.append((rule.box[3], column_of(rule.box[0], rule.box[2], 6), "rule", rule))
    if pg.gutters:
        # The multi-column zone: the longest stretch of height where at least two columns have content near
        # each other. Items entirely above or below it span the page (a heading before the columns, a closing
        # paragraph after them) even when they are narrow.
        reach = 1.25 * body
        y_lo = int(min(o.box[1] for _, _, _, o in units) - reach) - 1
        y_hi = int(max(o.box[3] for _, _, _, o in units) + reach) + 1
        ncols = len(pg.gutters) + 1
        cov = [bytearray(y_hi - y_lo + 1) for _ in range(ncols)]
        for _, c, _, o in units:
            if c >= 0:
                a, b = int(o.box[1] - reach) - y_lo, int(o.box[3] + reach) - y_lo
                cov[c][max(0, a):b + 1] = b"\x01" * (min(len(cov[c]), b + 1) - max(0, a))
        best, start = (0, -1), None
        for y in range(y_hi - y_lo + 2):
            multi = y <= y_hi - y_lo and sum(cov[c][y] for c in range(ncols)) >= 2
            if multi and start is None:
                start = y
            elif not multi and start is not None:
                if y - start > best[1] - best[0]:
                    best = (start, y)
                start = None
        if best[1] > best[0]:
            z_lo, z_hi = best[0] + y_lo + reach, best[1] + y_lo - reach
            outside = set()
            for c in range(ncols):
                mine = sorted((u for u in units if u[1] == c), key=lambda u: -u[0])
                inz = [k for k, u in enumerate(mine) if not (u[3].box[1] > z_hi + 1 or u[0] < z_lo - 1)]
                if not inz:
                    continue
                # Below the zone: stays in the column while it continues it (a longer column's last lines).
                edge, broken = mine[inz[-1]][3].box[1], False
                for u in mine[inz[-1] + 1:]:
                    if broken or u[0] < edge - reach:
                        broken = True
                        outside.add(id(u))
                    else:
                        edge = min(edge, u[3].box[1])
                # Above the zone: the same, going up.
                edge, broken = mine[inz[0]][0], False
                for u in reversed(mine[:inz[0]]):
                    if broken or u[3].box[1] > edge + reach:
                        broken = True
                        outside.add(id(u))
                    else:
                        edge = max(edge, u[0])
            moved = []
            for u in units:
                if id(u) in outside:
                    u = (u[0], -1, u[2], u[3])
                    if u[2] == "seg":
                        u[3].col = -1
                moved.append(u)
            units = moved
    units.sort(key=lambda u: -u[0])
    band, kind_prev = 0, None
    groups = defaultdict(list)                              # (band, column) -> units
    for u in units:
        spanning = u[1] == -1
        if kind_prev is not None and spanning != kind_prev:
            band += 1
        kind_prev = spanning
        groups[(band, u[1])].append(u)

    items = []
    for (b, col), us in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        lo, hi = (left_edge, right_edge) if col == -1 else bounds[col]
        # Lines of text between the other units: build blocks from consecutive rows.
        rows_run: list[list[Seg]] = []
        by_line = {}
        seq = []
        rules = [obj for _, _, kind, obj in us if kind == "rule"]
        for top, _, kind, obj in us:
            if kind == "rule":
                continue
            if kind == "seg":
                if obj.line_id in by_line:
                    by_line[obj.line_id].append(obj)
                else:
                    by_line[obj.line_id] = [obj]
                    seq.append(("row", by_line[obj.line_id]))
            else:
                seq.append((kind, obj))
        group_items = []
        for kind, obj in seq:
            if kind == "row":
                rows_run.append(sorted(obj, key=lambda s: s.x0))
                continue
            if rows_run:
                group_items += build_blocks(rows_run, lo, hi, body)
                rows_run = []
            group_items.append((kind, obj))
        if rows_run:
            group_items += build_blocks(rows_run, lo, hi, body)
        for rule in rules:
            y = (rule.box[1] + rule.box[3]) / 2
            table = next((b for b in group_items if isinstance(b, Block) and b.kind == "table" and b.y0 - 6 <= y <= b.y1 + 6
                          and rule.box[0] < b.x1 and b.x0 < rule.box[2]), None)
            if table:
                table.rules.append(y)
                continue
            top = lambda it: it.y1 if isinstance(it, Block) else it[1].box[3]  # noqa: E731
            at = next((k for k, it in enumerate(group_items) if top(it) < y), len(group_items))
            group_items.insert(at, ("rule", rule))
        items += group_items
    pg.items = items

    for box in pg.boxes:
        rows = defaultdict(list)
        for s in box.segs:
            if not s.rotated:
                rows[s.line_id].append(s)
        rr = sorted((sorted(v, key=lambda s: s.x0) for v in rows.values()), key=lambda row: -row[0].y)
        box.blocks = build_blocks(rr, box.box[0], box.box[2], body) if rr else []


# --------------------------------------------------------------------------- #
# Text output: inline markup for extraction.md, LaTeX for the layered snippets
# --------------------------------------------------------------------------- #

def esc_md(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_tags(r: Run, base: tuple, sid) -> tuple:
    """Inline tags for a run whose style differs from its block's: ((open, close), …)."""
    key = style_key(r)
    tags = []
    if r.link:
        tags.append((f'<a href="{esc_md(r.link)}">', "</a>"))
    if r.script:
        tags.append((f"<{r.script}>", f"</{r.script}>"))
    elif r.font.kind == "math" and base[1] != "math":
        tags.append(("<m>", "</m>"))
    else:
        if r.font.kind == "mono" and base[1] != "mono":
            tags.append(("<tt>", "</tt>"))
        elif key[1] != base[1] or abs(key[4] - base[4]) > 0.6:
            s = sid(key)
            tags.append((f"<{s}>", f"</{s}>"))
        if key[2] and not base[2]:
            tags.append(("<b>", "</b>"))
        if key[3] and not base[3]:
            tags.append(("<i>", "</i>"))
    if key[5] != base[5] and not r.script:
        tags.append((f"<c {key[5] or '#000000'}>", "</c>"))
    return tuple(tags)


def markup(runs: list[Run], base=None, sid=None) -> str:
    """Runs -> text with <b>, <i>, <sup>… for extraction.md, relative to the block's own style."""
    base = base or dominant(runs, style_key)
    sid = sid or (lambda k: "S?")
    out, open_tags, prev = [], (), None
    for r in runs:
        tags = run_tags(r, base, sid)
        space = ""
        if prev is not None:
            gap = r.x0 - prev.x1 if r.horizontal else 0
            if gap > 0.12 * max(r.size, prev.size) and not r.text.startswith(" ") and not prev.text.endswith(" "):
                space = " "
        if tags != open_tags:
            out += [c for _, c in reversed(open_tags)]
            out.append(space)
            out += [o for o, _ in tags]
            open_tags = tags
        else:
            out.append(space)
        out.append(esc_md(r.text))
        prev = r
    out += [c for _, c in reversed(open_tags)]
    return re.sub(r"[ \t]+", " ", "".join(out)).strip()


TEX_SPECIAL = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "#": r"\#", "%": r"\%",
               "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "<": r"\textless{}",
               ">": r"\textgreater{}", "|": r"\textbar{}", "\u00a0": "~", "\u00ad": "",
               # symbols pdfLaTeX can't typeset as text without extra set-up
               "\u2212": r"\ensuremath{-}", "\u2248": r"\ensuremath{\approx}", "\u2264": r"\ensuremath{\le}",
               "\u2265": r"\ensuremath{\ge}", "\u2260": r"\ensuremath{\ne}", "\u00b1": r"\ensuremath{\pm}",
               "\u00d7": r"\ensuremath{\times}", "\u00f7": r"\ensuremath{\div}", "\u221e": r"\ensuremath{\infty}",
               "\u2211": r"\ensuremath{\sum}", "\u222b": r"\ensuremath{\int}", "\u221a": r"\ensuremath{\surd}",
               "\u2202": r"\ensuremath{\partial}", "\u2206": r"\ensuremath{\Delta}", "\u2192": r"\ensuremath{\rightarrow}",
               "\u2190": r"\ensuremath{\leftarrow}", "\u2194": r"\ensuremath{\leftrightarrow}",
               "\u21d2": r"\ensuremath{\Rightarrow}", "\u2208": r"\ensuremath{\in}", "\u2022": r"\textbullet{}",
               "\u00b0": r"\textdegree{}", "\u00b5": r"\ensuremath{\mu}", "\u00b2": r"\textsuperscript{2}",
               "\u00b3": r"\textsuperscript{3}", "\u2026": r"\dots{}", "\u2713": r"\ensuremath{\checkmark}",
               "\u2714": r"\ensuremath{\checkmark}", "\u25aa": r"\textbullet{}", "\u25cf": r"\textbullet{}",
               "\u2023": r"\textbullet{}", "\u2043": "-", "\u2219": r"\textbullet{}",
               "\u27a2": r"\ensuremath{\triangleright}", "\u25ba": r"\ensuremath{\triangleright}",
               "\u25b8": r"\ensuremath{\triangleright}"}
GREEK = dict(zip("αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ",
                 (r"\alpha \beta \gamma \delta \varepsilon \zeta \eta \theta \iota \kappa \lambda \mu \nu \xi \pi \rho "
                  r"\sigma \tau \upsilon \varphi \chi \psi \omega \Gamma \Delta \Theta \Lambda \Xi \Pi \Sigma \Phi "
                  r"\Psi \Omega").split()))


def tex_escape(s: str) -> str:
    return "".join(TEX_SPECIAL.get(ch) or (r"\ensuremath{" + GREEK[ch] + "}" if ch in GREEK else ch) for ch in s)


def font_cmd(kind: str, size: float, bold: bool, italic: bool, smallcaps=False) -> str:
    pt = size * 72.27 / 72                                 # PDF points (bp) -> TeX points
    fam = {"sans": r"\sffamily", "mono": r"\ttfamily"}.get(kind, r"\rmfamily")
    return (f"{fam}\\fontsize{{{pt:.2f}}}{{{pt * 1.2:.2f}}}\\selectfont" + (r"\bfseries" if bold else "")
            + (r"\itshape" if italic else "") + (r"\scshape" if smallcaps else ""))


def latex_runs(runs: list[Run], base: tuple, colors: set) -> str:
    """Runs -> LaTeX, with \\textbf, \\textit, \\textsuperscript… where a run differs from the base style."""
    out, prev = [], None
    for r in runs:
        t = tex_escape(r.text)
        if prev is not None:
            gap = r.x0 - prev.x1 if r.horizontal else 0
            if gap > 0.12 * max(r.size, prev.size) and not r.text.startswith(" ") and not prev.text.endswith(" "):
                out.append(" ")
        key = style_key(r)
        if r.script:
            t = (r"\textsuperscript{" if r.script == "sup" else r"\textsubscript{") + t + "}"
        else:
            if key[2] and not base[2]:
                t = r"\textbf{" + t + "}"
            if key[3] and not base[3]:
                t = r"\textit{" + t + "}"
            if abs(key[4] - base[4]) > 0.6:
                pt = r.size * 72.27 / 72
                t = f"{{\\fontsize{{{pt:.2f}}}{{{pt * 1.2:.2f}}}\\selectfont {t}}}"
        if key[5] != base[5]:
            if key[5]:
                colors.add(key[5][1:])
            t = f"\\textcolor{{{'pdf' + key[5][1:] if key[5] else 'black'}}}{{{t}}}"
        out.append(t)
        prev = r
    return "".join(out).strip()


def tikz_nodes(segs: list[Seg], ox: float, oy: float, colors: set, stretch: bool) -> list[str]:
    """One TikZ node per segment, at its exact baseline position relative to (ox, oy)."""
    stacks = defaultdict(list)                             # right-aligned columns of short labels (y-axis ticks)
    for s in segs:
        if not s.rotated and len(s.text) <= 12:
            stacks[round(s.x1)].append(s)
    lines = []
    for s in segs:
        if not s.text.strip():
            continue
        base = dominant(s.runs, style_key)
        f = next(r.font for r in s.runs if style_key(r) == base)
        body = latex_runs(s.runs, base, colors)
        if s.rotated:
            first = s.runs[0]
            opts = ["anchor=base west", f"rotate={first.angle:.1f}"]
            x, y = first.x - ox, first.y - oy
        else:
            y = s.y - oy
            peers = stacks.get(round(s.x1), [])
            if len(s.text) <= 12 and len(peers) >= 2 and len({round(p.x0) for p in peers}) >= 2 and not stretch:
                opts, x = ["anchor=base east"], s.x1 - ox
            elif len(s.text) <= 4 and not stretch:
                opts, x = ["anchor=base"], (s.x0 + s.x1) / 2 - ox
            else:
                opts, x = ["anchor=base west"], s.x0 - ox
                if stretch and s.words >= 3 and " " in s.text:
                    body = f"\\makebox[{s.x1 - s.x0:.1f}bp][s]{{{body}}}"   # same line width as the original
        opts.append("font=" + font_cmd(f.kind, base[4], base[2], base[3], f.smallcaps))
        if base[5]:
            colors.add(base[5][1:])
            opts.append("text=pdf" + base[5][1:])
        lines.append(f"\\node[{', '.join(opts)}] at ({x:.2f},{y:.2f}) {{{body}}};")
    return lines


def color_defs(colors: set) -> list[str]:
    return [f"\\definecolor{{pdf{c}}}{{HTML}}{{{c}}}" for c in sorted(colors)]


# --------------------------------------------------------------------------- #
# The whole import
# --------------------------------------------------------------------------- #

def page_tag(n: int, total: int) -> str:
    return f"{n:0{max(2, len(str(total)))}d}"


def analyse(pdf: Path, text: dict, root: Path, progress=lambda msg: None, source_name: str = "") -> dict:
    """Run the analysis and write every output listed at the top of this file. Returns a summary."""
    t_start = time.time()
    if not gs_path():
        raise PdfImportError(GS_MISSING)
    pdf = Path(pdf)
    imp, figdir, laydir = root / "import", root / "figures", root / "layers"
    (imp / "pages").mkdir(parents=True, exist_ok=True)
    figdir.mkdir(exist_ok=True)
    laydir.mkdir(exist_ok=True)
    fonts = {fid: Font(name) for fid, name in (text.get("fonts") or {}).items()}
    raw_pages = text.get("pages") or []
    total = len(raw_pages)
    if not total:
        raise PdfImportError("The PDF has no pages.")
    pages = []
    for n, rp in enumerate(raw_pages, 1):
        pg = Page(n, float(rp["w"]), float(rp["h"]))
        for it in rp.get("items", []):
            try:
                r = Run(it, pg.h, fonts, n)
            except (ValueError, TypeError, IndexError):
                continue
            if r.text.strip():
                pg.runs.append(r)
        for x0, y0, x1, y1, url in rp.get("links", []):
            pg.links.append(((x0, pg.h - y1, x1, pg.h - y0), url))
        for r in pg.runs:
            for box, url in pg.links:
                if inside(((r.box[0] + r.box[2]) / 2, (r.box[1] + r.box[3]) / 2), box, 1):
                    r.link = url
        pages.append(pg)

    sizes = Counter()
    for pg in pages:
        for r in pg.runs:
            if r.horizontal and r.font.kind not in ("math", "symbol"):
                sizes[round(r.size * 2) / 2] += len(r.text)
    body = sizes.most_common(1)[0][0] if sizes else 10.0

    progress("Making a copy of the PDF without its text…")
    graphics = figdir / "original-graphics.pdf"
    if make_graphics_pdf(pdf, graphics):
        notext_src, filt = graphics, False
    else:   # fall back to Ghostscript's render-time filter (it misses text on pages with transparency)
        notext_src, filt = pdf, True

    with tempfile.TemporaryDirectory(prefix="texann-import-") as td:
        tmp = Path(td)
        # Render in batches: page images, text colours, whether text is visible, and the regions of ink.
        for first in range(1, total + 1, BATCH):
            last = min(total, first + BATCH - 1)
            progress(f"Rendering pages {first}–{last} of {total}…" if total > 1 else "Rendering the page…")
            with_t = render_ppm(pdf, tmp, "w", PAGE_DPI, first, last)
            no_t = render_ppm(notext_src, tmp, "n", PAGE_DPI, first, last, filter_text=filt)
            low = render_ppm(notext_src, tmp, "l", REGION_DPI, first, last, filter_text=filt)
            for k, n in enumerate(range(first, last + 1)):
                pg = pages[n - 1]
                rw, rn = Raster(with_t[k], pg.w, pg.h), Raster(no_t[k], pg.w, pg.h)
                png_from_raster(rw, imp / "pages" / f"page-{page_tag(n, total)}.png", PAGE_DPI)
                sample_runs(pg.runs, rw, rn)
                pg.regions = find_regions(Raster(low[k], pg.w, pg.h), pg.w, pg.h)
                ink = sum(r.w * r.h for r in pg.regions) / (pg.w * pg.h)
                chars = sum(len(r.text) for r in pg.runs)
                visible = sum(len(r.text) for r in pg.runs if r.seen >= 0.02)
                odd = sum(1 for r in pg.runs for c in r.text if "\ue000" <= c <= "\uf8ff" or c == "\ufffd")
                if not pg.runs and ink > 0.02:
                    pg.scan = "no-text"
                elif chars and visible < 0.2 * chars:
                    pg.scan = "invisible-text"
                elif chars and odd > 0.1 * chars:
                    pg.scan = "garbled"
                del rw, rn
                for f in (with_t[k], no_t[k], low[k]):
                    f.unlink(missing_ok=True)
        for pg in pages:
            if pg.scan:
                hi = render_ppm(pdf, tmp, "h", HIRES_DPI, pg.n, pg.n)[0]
                png_from_raster(Raster(hi, pg.w, pg.h), imp / "pages" / f"page-{page_tag(pg.n, total)}-hires.png", HIRES_DPI)
                hi.unlink(missing_ok=True)

        snap_colors([r for pg in pages for r in pg.runs])
        progress("Finding figures, columns and text blocks…")
        for pg in pages:
            pg.lines, pg.segs = build_segments(pg.runs)
        running = detect_running(pages)
        for pg in pages:
            if pg.scan == "no-text":
                continue
            if pg.scan:
                pg.rules = [r for r in pg.regions if r.kind in ("hrule", "vrule")]   # text is part of an image
            else:
                classify_regions(pg, body)
            layout(pg, body)
            pg.words = sum(s.words for s in pg.segs if s.role != "running")
            if pg.background:
                pg.designed.append("graphics cover the whole page")
            cover = sum(f.w * f.h for f in pg.figures) / (pg.w * pg.h)
            if cover > 0.35:
                pg.designed.append(f"figures cover {cover:.0%} of it")
            if any(r.size >= 2.2 * body for r in pg.runs) and pg.words < 250:
                pg.designed.append("large display text")

        # Figures: crop each from a high-resolution text-free render and write its layered snippet.
        n_fig = 0
        for pg in pages:
            if not pg.figures:
                continue
            pg.figures.sort(key=lambda f: (-f.box[3], f.box[0]))
            dpi = PHOTO_DPI if all(f.photo for f in pg.figures) else FIGURE_DPI
            progress(f"Cutting out the figure{'s' * (len(pg.figures) != 1)} on page {pg.n}…")
            hi = render_ppm(notext_src, tmp, "f", dpi, pg.n, pg.n, filter_text=filt)[0]
            rh = Raster(hi, pg.w, pg.h)
            for k, fig in enumerate(pg.figures, 1):
                fig.name = f"fig-{page_tag(pg.n, total)}-{k}"
                fig.file = f"figures/{fig.name}.png"
                pb = rh.box(*fig.box)
                if pb[2] - pb[0] < 2 or pb[3] - pb[1] < 2:
                    continue
                n_fig += 1
                png_from_raster(rh, root / fig.file, dpi, pb)
                bx0, bx1 = pb[0] / rh.kx, pb[2] / rh.kx            # exact box of the pixels kept, in points
                by1, by0 = pg.h - pb[1] / rh.ky, pg.h - pb[3] / rh.ky
                fig.box = (bx0, by0, bx1, by1)
                colors: set = set()
                nodes = tikz_nodes(fig.segs, bx0, by0, colors, stretch=False)
                w, h = bx1 - bx0, by1 - by0
                snippet = [
                    f"% Figure {k} on page {pg.n} of the original PDF, in two layers: the image ({fig.file}, rendered",
                    f"% with its text removed) and its {len(nodes)} text label{'s' * (len(nodes) != 1)} on top as real, editable text.",
                    f"% Natural size {w:.1f} x {h:.1f} bp. To resize it together with its labels:",
                    f"%   \\resizebox{{\\linewidth}}{{!}}{{\\input{{figures/{fig.name}}}}}",
                    *color_defs(colors),
                    r"\begin{tikzpicture}[x=1bp, y=1bp, every node/.style={inner sep=0pt, outer sep=0pt}]",
                    f"\\node[anchor=south west] at (0,0) {{\\includegraphics[width={w:.2f}bp,height={h:.2f}bp]{{{fig.file}}}}};",
                    f"\\useasboundingbox (0,0) rectangle ({w:.2f},{h:.2f});",
                    *nodes,
                    r"\end{tikzpicture}%",
                ]
                (figdir / f"{fig.name}.tex").write_text("\n".join(snippet) + "\n", encoding="utf-8")
            del rh
            hi.unlink(missing_ok=True)

    progress("Writing the positioned copy of each page…")
    for pg in pages:
        colors: set = set()
        tag = page_tag(pg.n, total)
        nodes = [] if pg.scan else tikz_nodes([s for s in pg.segs if s.text.strip()], 0.0, 0.0, colors, stretch=True)
        note = ("% This page's text is part of its image (scanned), so there are no text nodes yet."
                if pg.scan in ("no-text", "invisible-text") else
                "% This page's text layer is garbled, so there are no text nodes yet." if pg.scan else
                "% Every line of text is placed at its exact position; \\makebox[...][s] keeps each line's original width.")
        lay = [
            f"% Page {pg.n} of the original PDF as layers: the original's graphics with all text removed",
            f"% (figures/original-graphics.pdf, page {pg.n}) and the text on top.",
            note,
            f"% Use it on a page of its own, in a document whose paper size is {pg.w:.1f} x {pg.h:.1f} bp:",
            f"%   \\newpage\\thispagestyle{{empty}}\\input{{layers/page-{tag}}}\\newpage",
            "% Needs \\usepackage{tikz}. Coordinates are PDF points from the bottom-left corner of the page.",
            *color_defs(colors),
            r"\noindent\begin{tikzpicture}[remember picture, overlay]",
            r"\begin{scope}[shift={(current page.south west)}, x=1bp, y=1bp, every node/.style={inner sep=0pt, outer sep=0pt}]",
            (f"\\node[anchor=south west] at (0,0) {{\\includegraphics[page={pg.n}]{{figures/original-graphics.pdf}}}};"
             if not filt else "% (no text-free copy of the PDF could be made, so there is no background layer)"),
            *nodes,
            r"\end{scope}",
            r"\end{tikzpicture}%",
        ]
        (laydir / f"page-{tag}.tex").write_text("\n".join(lay) + "\n", encoding="utf-8")

    md, page_lines = write_extraction(pages, running, body, installed_packages(), text, source_name or pdf.name)
    (imp / "extraction.md").write_text(md, encoding="utf-8")
    summary = {
        "source": source_name or pdf.name,
        "pages": [{"n": pg.n, "w": round(pg.w, 2), "h": round(pg.h, 2), "words": pg.words,
                   "figures": [f.name for f in pg.figures if f.name], "boxes": len(pg.boxes), "scan": pg.scan,
                   "designed": pg.designed, "lines": page_lines.get(pg.n)} for pg in pages],
        "summary_lines": page_lines.get("summary"),
        "words": sum(pg.words for pg in pages),
        "figures": n_fig,
        "scanned_pages": [pg.n for pg in pages if pg.scan],
        "designed_pages": [pg.n for pg in pages if pg.designed],
        "seconds": round(time.time() - t_start, 1),
    }
    (imp / "analysis.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


# --------------------------------------------------------------------------- #
# extraction.md
# --------------------------------------------------------------------------- #

def mm(pt: float) -> str:
    return f"{pt * 25.4 / 72:.1f} mm"


def paper_name(w: float, h: float) -> str:
    for name, (a, b) in {"A4": (595.28, 841.89), "Letter": (612, 792), "A5": (419.53, 595.28), "A3": (841.89, 1190.55),
                         "Legal": (612, 1008), "B5": (498.9, 708.66)}.items():
        if abs(w - a) < 3 and abs(h - b) < 3:
            return f"{name} portrait"
        if abs(w - b) < 3 and abs(h - a) < 3:
            return f"{name} landscape"
    return "custom size"


def percentile(vals, q):
    vals = sorted(vals)
    if not vals:
        return 0.0
    k = (len(vals) - 1) * q
    lo = int(k)
    return vals[lo] + (vals[min(lo + 1, len(vals) - 1)] - vals[lo]) * (k - lo)


def wrap(text: str, width=110) -> list[str]:
    return textwrap.wrap(text, width, break_long_words=False, break_on_hyphens=False) or [""]


def fam_word(kind: str) -> str:
    return {"sans": "sans-serif", "serif": "serif", "mono": "monospace", "math": "maths", "symbol": "symbols"}.get(kind, "?")


def fmt_pages(ns) -> str:
    ns = sorted(set(ns))
    if not ns:
        return "-"
    parts, start, prev = [], ns[0], ns[0]
    for n in ns[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}–{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(parts)


def write_extraction(pages: list[Page], running, body: float, have: set, text: dict, name: str):
    # Styles, numbered by how much text uses them.
    use, example, pages_of = Counter(), {}, defaultdict(set)
    for pg in pages:
        if pg.scan == "invisible-text":
            continue
        for r in pg.runs:
            k = style_key(r)
            use[k] += len(r.text)
            pages_of[k].add(pg.n)
            if k not in example and len(r.text.strip()) >= 3:
                example[k] = r.text.strip()
    order = [k for k, _ in use.most_common()]
    ids = {k: f"S{i + 1}" for i, k in enumerate(order)}
    sid = lambda k: ids.get(k, "S?")  # noqa: E731
    total_chars = sum(use.values()) or 1
    body_key = next((k for k in order if k[1] not in ("math", "symbol")), None)

    # Measurements from the body text of ordinary pages.
    normal = [pg for pg in pages if not pg.scan and not pg.designed]
    body_segs, pitches, gaps, indents, aligns, col_counts = [], [], [], [], Counter(), Counter()
    for pg in normal:
        col_counts[len(pg.gutters) + 1] += 1
        prev = None
        for it in pg.items:
            if isinstance(it, Block) and it.kind == "p" and body_key and it.style[:5] == body_key[:5]:
                body_segs += [s for row in it.rows for s in row]
                ys = [row[0].y for row in it.rows]
                pitches += [a - b for a, b in zip(ys, ys[1:]) if 0 < a - b < 3 * body]
                if len(it.rows) >= 2:
                    aligns[it.align] += 1
                if it.indent:
                    indents.append(it.indent)
                if prev is not None:
                    g = prev.rows[-1][0].y - it.rows[0][0].y
                    if 0 < g < 4 * body:
                        gaps.append(g)
                prev = it
            else:
                prev = None

    out: list[str] = []
    W, H = pages[0].w, pages[0].h
    info = text.get("info") or {}
    made = " / ".join(x for x in (info.get("creator"), info.get("producer")) if x)
    n_fig = sum(1 for pg in pages for f in pg.figures if f.name)
    words = sum(pg.words for pg in pages)
    out += [f"# Extraction of {name}", "",
            f"{len(pages)} page{'s' * (len(pages) != 1)} · {words:,} words · {n_fig} figure{'s' * (n_fig != 1)}"
            + (f" · made with {made}" if made else "")]
    if info.get("title") or info.get("author"):
        out.append(" · ".join(x for x in (f"Title: {info.get('title')}" if info.get("title") else "",
                                           f"Author: {info.get('author')}" if info.get("author") else "") if x))
    scans = [pg.n for pg in pages if pg.scan]
    if scans:
        out.append(f"**No usable text layer on page{'s' * (len(scans) != 1)} {fmt_pages(scans)}** (scanned or garbled): "
                   "read the text from the page images there.")
    out += ["", "## How to read this file",
            "- Each page starts with a `## Page N` heading. Coordinates are PDF points (bp, 1/72 in) from the "
            "bottom-left of the page; the y of text is its baseline.",
            "- The text is the PDF's own text layer, so the wording is exact. Blocks are in reading order; the lines of "
            "a paragraph are joined.",
            "- `[b3 · S1 · x 72 · y 700 · 6 lines, justified]` heads each block. S-numbers are the styles listed below.",
            "- Inline: <b>…</b> bold, <i>…</i> italic, <sup>/<sub>, <tt> monospace, <m> set in a maths font, "
            "<c #RRGGBB> colour, <S4>…</S4> another style, <a href=…> link. &lt; &gt; &amp; stand for < > &.",
            "- `-|` is a hyphen at the end of a line: drop it in split words (exam-|ple → example), keep it in compounds "
            "(well-|known → well-known).",
            "- ` ⟶ ` is a wide gap inside a line (tab stop, right-aligned part). `lines kept`: the line breaks are real.",
            "- Tables found from aligned text are given as `| cell | cell |` rows; check them against the page image.",
            "- `[fig-02-1 …]` is a figure: figures/fig-02-1.png (text removed) and figures/fig-02-1.tex (the image with "
            "its labels on top as text). Its labels are listed there and are not repeated in the text."]

    out += ["", "## Page geometry (measured)"]
    sizes_seen = Counter((round(pg.w, 1), round(pg.h, 1)) for pg in pages)
    for (w, h), c in sizes_seen.most_common():
        where = "all pages" if c == len(pages) else "page" + "s" * (c != 1) + " " + fmt_pages(
            [pg.n for pg in pages if (round(pg.w, 1), round(pg.h, 1)) == (w, h)])
        out.append(f"- Paper: {w} × {h} pt ({mm(w)} × {mm(h)}), {paper_name(w, h)} — {where}")
    if body_segs:
        left = percentile([s.x0 for s in body_segs], 0.05)
        right = percentile([s.x1 for s in body_segs], 0.97)
        flow = [[s for s in pg.segs if s.role != "running" and not s.rotated] for pg in normal]
        tops = [max(s.box[3] for s in ss) for ss in flow if ss]
        bottoms = [min(s.box[1] for s in ss) for ss in flow if ss]
        top = percentile(tops, 0.8) if tops else H
        bottom = percentile(bottoms, 0.2) if bottoms else 0
        out.append(f"- Text area: x {left:.1f}–{right:.1f}, y {bottom:.1f}–{top:.1f} → margins left {left:.1f} pt ({mm(left)}), "
                   f"right {W - right:.1f} pt ({mm(W - right)}), top {H - top:.1f} pt ({mm(H - top)}), bottom {bottom:.1f} pt "
                   f"({mm(bottom)}). Measured from the body text; headers and footers sit outside it.")
    if col_counts:
        parts = []
        for k in sorted(col_counts):
            ps = [pg.n for pg in normal if len(pg.gutters) + 1 == k]
            gut = next((pg.gutters[0] for pg in normal if len(pg.gutters) + 1 == k and pg.gutters), None)
            parts.append(f"{k} on page{'s' * (len(ps) != 1)} {fmt_pages(ps)}"
                         + (f" (gap between columns x {gut[0]:.1f}–{gut[1]:.1f}, {gut[1] - gut[0]:.1f} pt)" if gut else ""))
        out.append("- Columns: " + "; ".join(parts) + ". A column region may be only part of a page: see the blocks' x.")
    for zone, norm, hits in sorted(running, key=lambda t: (t[0] != "top", t[1])):
        pg0, s0 = hits[0]
        c = (s0.x0 + s0.x1) / 2
        pos = "left" if c < pg0.w * 0.38 else "right" if c > pg0.w * 0.62 else "centred"
        varies = len({s.text for _, s in hits}) > 1
        what = "page number" if re.fullmatch(r"[#\s\-–—|]+", norm) else f'"{esc_md(s0.text)}"' + (
            " (with the page number)" if "#" in norm and varies else "")
        out.append(f"- Running {'header' if zone == 'top' else 'footer'}: {what}, {pos}, baseline y {s0.y:.1f}, "
                   f"{sid(dominant(s0.runs, style_key))} — pages {fmt_pages(sorted({pg.n for pg, _ in hits}))} "
                   "(left out of the pages below)")

    if body_key:
        f = body_key
        out += ["", "## Body text",
                f"- {sid(f)}: {f[0]} ({fam_word(f[1])}), {f[4]:g} pt, {('colour ' + f[5]) if f[5] else 'black'} — "
                f"{use[f] / total_chars:.0%} of the text"]
        pitch = percentile(pitches, 0.5) if pitches else 0
        para = []
        if pitch:
            para.append(f"line pitch {pitch:.1f} pt = {pitch / f[4]:.2f} × the font size (LaTeX's default is about 1.2)")
        para.append(f"first lines indented {percentile(indents, 0.5):.1f} pt" if len(indents) >= 2 else "no first-line indent")
        extra = percentile(gaps, 0.5) - pitch if gaps and pitch else 0
        if extra > 1.5:
            para.append(f"{extra:.1f} pt extra space between paragraphs")
        if aligns:
            para.append(f"{aligns.most_common(1)[0][0]} text")
        out.append("- " + " · ".join(para))

    out += ["", "## Styles", "| style | font | kind | size | colour | share | pages | example |", "|---|---|---|---|---|---|---|---|"]
    for k in order[:40]:
        extra = (" (bold)" if k[2] and not re.search("bold|black|heavy|demi|semi", k[0], re.I) else "") + \
                (" (italic)" if k[3] and not re.search("ital|oblique", k[0], re.I) else "")
        out.append(f"| {sid(k)} | {esc_md(k[0])}{extra} | {fam_word(k[1])} | {k[4]:g} pt | {k[5] or 'black'} | "
                   f"{use[k] / total_chars:.1%} | {fmt_pages(sorted(pages_of[k]))} | {esc_md(example.get(k, '')[:40])} |")
    if len(order) > 40:
        out.append(f"(+ {len(order) - 40} rarely used styles)")
    fams = Counter()
    for k in order:
        if k[1] not in ("math", "symbol"):
            fams[re.split(r"[-,]", k[0])[0]] += use[k]
    sugg = [f"{fam}: {s}" for fam, _ in fams.most_common(6) if (s := font_suggestion(fam, have))]
    if sugg:
        out += ["", "Closest installed LaTeX fonts (pdfLaTeX): " + " · ".join(sugg)]

    outline = text.get("outline") or []
    if outline:
        out += ["", "## Bookmarks (the PDF's own outline: section titles and their levels)"]
        for level, title, page in outline[:150]:
            out.append(f"{'  ' * (max(1, level) - 1)}- {esc_md(title)}" + (f" (p{page})" if page else ""))
        if len(outline) > 150:
            out.append(f"(+ {len(outline) - 150} more)")

    if have is None:
        out += ["", "## LaTeX packages",
                "This computer uses MiKTeX, which installs any missing package the first time a document uses it (this "
                "needs an internet connection), so the usual packages are all available. Prefer widely used ones."]
    elif have:
        out += ["", "## LaTeX packages installed on this computer",
                "Fonts: " + ", ".join(p for p in FONT_PACKAGES if p in have),
                "Other: " + ", ".join(p for p in OTHER_PACKAGES if p in have)]
        missing = [p for p in FONT_PACKAGES + OTHER_PACKAGES if p not in have]
        if missing:
            out.append("Not installed, so don't use: " + ", ".join(missing))

    page_lines = {"summary": [1, len(out)]}
    for pg in pages:
        out.append("")
        start = len(out) + 1
        write_page(pg, out, sid, len(pages))
        page_lines[pg.n] = [start, len(out)]
    return "\n".join(out) + "\n", page_lines


def row_markup(row: list[Seg], base, sid) -> str:
    parts = [markup(s.runs, base, sid) for s in row]
    if len(row) >= 2 and MARKER_ONLY.match(row[0].text.strip()):
        parts[:2] = [parts[0] + " " + parts[1]]           # "1 Introduction", "2. Retire the…" (a tab, not a gap)
    return " ⟶ ".join(parts)


def write_page(pg: Page, out: list, sid, total: int):
    tag = page_tag(pg.n, total)
    head = f"## Page {pg.n} · {pg.w:.0f} × {pg.h:.0f} pt"
    if not pg.scan:
        head += f" · {len(pg.gutters) + 1} column{'s' * bool(pg.gutters)} · {pg.words} words"
        named = [f for f in pg.figures if f.name]
        if named:
            head += f" · {len(named)} figure{'s' * (len(named) != 1)}"
    out.append(head)
    out.append(f"Page image: import/pages/page-{tag}.png · positioned copy: layers/page-{tag}.tex")
    if pg.designed:
        out.append("Looks like a designed page (" + "; ".join(pg.designed) + "): consider the positioned copy.")
    hires = f"import/pages/page-{tag}-hires.png"
    conv = f"x_pt = x_px × {72 / HIRES_DPI:.2f}, y_pt = {pg.h:.1f} − y_px × {72 / HIRES_DPI:.2f}"
    if pg.scan == "no-text":
        out.append(f"NO TEXT LAYER: this page is an image (scanned, or its text was turned into outlines). Transcribe it "
                   f"from {hires} ({HIRES_DPI} dpi; {conv}). Crop any pictures from figures/original-graphics.pdf "
                   f"(page {pg.n}) with viewport, using those coordinates.")
        return
    if pg.scan == "invisible-text":
        out.append(f"SCANNED, WITH AN INVISIBLE (OCR) TEXT LAYER: the text below is the OCR's reading and may contain "
                   f"errors, so check it against {hires} ({conv}). The visible text is part of the page image, so no "
                   f"figures were cut out: crop pictures from figures/original-graphics.pdf (page {pg.n}) with viewport.")
    elif pg.scan == "garbled":
        out.append(f"THE TEXT LAYER LOOKS GARBLED (fonts without proper character codes): read the text from {hires} "
                   f"instead; the positions and styles below are still right.")
    if pg.background:
        b = pg.background
        out.append(f"Background graphics cover the page (x {b.box[0]:.0f}–{b.box[2]:.0f}, y {b.box[1]:.0f}–{b.box[3]:.0f}"
                   + (f", mostly {b.fill}" if b.fill else "") + ").")
    counters = {"b": 0, "t": 0}

    def emit(b: Block, indent=""):
        if b.kind == "table":
            counters["t"] += 1
            b.id = f"t{counters['t']}"
            ruled = f" · {len(b.rules)} horizontal rule{'s' * (len(b.rules) != 1)}" if b.rules else ""
            shaded = [f"row {k + 1} shaded {next(t.shade for c in cells for t in c if t.shade)}"
                      for k, cells in enumerate(b.cells) if any(t.shade for c in cells for t in c)]
            ruled += "".join(" · " + x for x in shaded[:6])
            out.append(f"{indent}[{b.id} · table? {b.ncols} columns × {len(b.cells)} rows · {sid(b.style)} · "
                       f"x {b.x0:.0f}–{b.x1:.0f} · y {b.y0:.0f}–{b.y1:.0f}{ruled}]")
            for cells in b.cells:
                out.append(indent + "| " + " | ".join(" ".join(markup(s.runs, b.style, sid) for s in c).replace("|", "\\|")
                                                       for c in cells) + " |")
            return
        counters["b"] += 1
        b.id = f"b{counters['b']}"
        n = len(b.rows)
        desc = f"{n} line{'s' * (n != 1)}"
        if b.align and (n >= 2 or b.align in ("centred", "right")):
            desc += f", {b.align}"
        if b.indent:
            desc += f", first line indented {b.indent:.0f} pt"
        if b.keep_lines:
            desc += ", lines kept"
        shade = {s.shade for row in b.rows for s in row}
        if len(shade) == 1 and None not in shade:
            desc += f", on a band of {shade.pop()}"
        bits = [b.id, sid(b.style), f"x {b.x0:.0f}", f"y {b.rows[0][0].y:.0f}", desc]
        if b.marker:
            bits.append(f"list item '{esc_md(b.marker)}'")
        if b.math:
            bits.append("maths: rebuild it from the page image")
        if b.caption:
            bits.append("caption?")
        out.append(f"{indent}[{' · '.join(bits)}]")
        texts = [row_markup(row, b.style, sid) for row in b.rows]
        if b.keep_lines:
            for t in texts:
                out.extend(indent + x for x in wrap(t))
            return
        joined = texts[0]
        for t in texts[1:]:
            if re.search(r"[-‐]$", joined) and re.match(r"[a-zà-ÿ]", t):
                joined = joined[:-1] + "-|" + t
            elif joined.endswith("\u00ad"):
                joined = joined[:-1] + t
            else:
                joined += " " + t
        out.extend(indent + x for x in wrap(joined))

    for it in pg.items:
        if isinstance(it, Block):
            emit(it)
            continue
        kind, obj = it
        if kind == "figure" and obj.name:
            labels = [s.text for s in obj.segs]
            out.append(f"[{obj.name} · figure · x {obj.box[0]:.0f}–{obj.box[2]:.0f} · y {obj.box[1]:.0f}–{obj.box[3]:.0f} · "
                       f"{obj.w:.0f} × {obj.h:.0f} pt · {'photo' if obj.photo else 'graphics'}, {len(labels)} "
                       f"label{'s' * (len(labels) != 1)} → \\input{{figures/{obj.name}}}]")
            if labels:
                shown = " · ".join(esc_md(t) for t in labels[:24])
                out.extend(wrap("labels: " + shown + (f" · … (+{len(labels) - 24})" if len(labels) > 24 else "")))
        elif kind == "box":
            desc = "ruled table grid" if obj.kind == "frame" and any(b.kind == "table" for b in obj.blocks) else \
                   ("filled " + obj.fill if obj.fill else "frame") + (f", border {obj.border}" if obj.border else "")
            out.append(f"[box · {desc} · x {obj.box[0]:.0f}–{obj.box[2]:.0f} · y {obj.box[1]:.0f}–{obj.box[3]:.0f}]")
            for b in obj.blocks:
                emit(b, "  ")
            out.append("[/box]")
        elif kind == "rotated":
            r0 = obj.runs[0]
            out.append(f"[rotated {r0.angle:.0f}° · {sid(dominant(obj.runs, style_key))} · x {r0.x:.0f} · y {r0.y:.0f}] "
                       f"{markup(obj.runs, None, sid)}")
        elif kind == "rule":
            out.append(f"[horizontal rule · x {obj.box[0]:.0f}–{obj.box[2]:.0f} · y {obj.box[1]:.0f}]")
    marks = [m for m in pg.marks if not any(inside(((m.box[0] + m.box[2]) / 2, (m.box[1] + m.box[3]) / 2), f.box)
                                            for f in pg.figures + pg.boxes)]
    if marks:
        shown = " ".join(f"({m.box[0]:.0f},{m.box[1]:.0f} {m.w:.0f}×{m.h:.0f})" for m in marks[:30])
        out.extend(wrap("Small graphics (bullets, icons, logos as x,y w×h; crop them from figures/original-graphics.pdf "
                        f"if needed): {shown}" + (f" … (+{len(marks) - 30})" if len(marks) > 30 else "")))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: python3 -m tex_annotator.pdf_import original.pdf text.json out-folder")
    src, js, outdir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    outdir.mkdir(parents=True, exist_ok=True)
    res = analyse(src, json.loads(js.read_text()), outdir, progress=print, source_name=src.name)
    print(json.dumps({k: v for k, v in res.items() if k != "pages"}, indent=1))
