#!/usr/bin/env python3
"""
Smoke test for Latex-Copilot on Windows, macOS and Linux: everything that depends on the system. It checks
compiling with latexmk, SyncTeX both ways, the PDF import analysis (with the browser's pdf.js text
extraction run under Node.js), that the generated layered LaTeX compiles, that the server answers, and the
security measures: a project's latexmkrc doesn't run, the server needs its access key, and project HTML is
served as plain text. Whether LaTeX can read or write files outside the project depends on the TeX version, so
those checks report a warning rather than fail.
Claude Code isn't needed. CI runs it on all three systems; it's also handy locally:

    python tests/smoke_test.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from latex_copilot import app, pdf_import  # noqa: E402

SAMPLE = r"""\documentclass{article}
\usepackage[a4paper,margin=25mm]{geometry}
\usepackage{tikz}
\begin{document}
\section*{Smoke test}
This document checks compiling, SyncTeX and the PDF import.

\begin{center}
\begin{tikzpicture}
\draw[fill=blue!15] (0,0) rectangle (6,3);
\draw[thick,red] (0.3,0.3) -- (2,2.4) -- (4,1.2) -- (5.7,2.7);
\node at (3,-0.4) {Time (s)};
\node[rotate=90] at (-0.4,1.5) {Amplitude};
\end{tikzpicture}
\end{center}

A second paragraph, so the page has some ordinary text as well.
\end{document}
"""

CHECK = r"""\documentclass{article}
\usepackage[paperwidth=595.28bp,paperheight=841.89bp,margin=20bp]{geometry}
\usepackage{graphicx,tikz}
\pagestyle{empty}
\begin{document}
\input{layers/page-01}\newpage
\noindent\input{figures/FIGURE}
\end{document}
"""


def step(msg):
    print(f"\n--- {msg}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


KEY = ""


def request(port, path, body=None, key=True, full=False):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if key:
        headers["X-Latex-Copilot-Token"] = KEY
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers=headers, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return (r.status, r.headers, r.read()) if full else r.read()
    except urllib.error.HTTPError as e:
        if full:
            return e.code, e.headers, e.read()
        raise


def wait_for_server(port, proc, seconds=180):
    end = time.time() + seconds
    while time.time() < end:
        if proc.poll() is not None:
            raise AssertionError(f"the server exited with code {proc.returncode}")
        try:
            return json.loads(request(port, "/api/status"))
        except urllib.error.HTTPError:
            raise
        except OSError:
            time.sleep(1)
    raise AssertionError("the server didn't answer")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="latex-copilot-smoke-"))   # deliberately not resolved: the app must cope
    proj = tmp / "project"
    proj.mkdir()
    (proj / "main.tex").write_text(SAMPLE, encoding="utf-8")
    print(f"latex-copilot {app.__version__} · Python {sys.version.split()[0]} · {sys.platform}")
    print("claude:", app.claude_command(), "· gs:", pdf_import.gs_path())

    step("compile with latexmk; a latexmkrc in the project must not run")
    marker = tmp / "latexmkrc-ran"
    (proj / "latexmkrc").write_text(f"open(my $f, '>', '{marker.as_posix()}'); close($f);\n", encoding="utf-8")
    app.S = app.State(proj, proj / "main.tex", "pdf", None)
    res = app.compile_tex()
    assert res["ok"], res["errors"]
    assert (proj / "main.pdf").exists()
    assert not marker.exists(), "the project's latexmkrc was run"
    (proj / "latexmkrc").unlink()

    step("LaTeX reading and writing files outside the project")
    ver = subprocess.run(["pdflatex", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    (tmp / "secret.txt").write_text("SECRET-OUTSIDE-THE-PROJECT", encoding="utf-8")
    outside = {
        "read": "\\documentclass{article}\\begin{document}\\input{../secret.txt}\\end{document}\n",
        "write": "\\documentclass{article}\\newwrite\\w\\immediate\\openout\\w=../written.txt "
                 "\\immediate\\write\\w{x}\\immediate\\closeout\\w\\begin{document}x\\end{document}\n",
    }
    for what, doc in outside.items():
        folder = tmp / what
        folder.mkdir()
        (folder / "main.tex").write_text(doc, encoding="utf-8")
        app.S = app.State(folder, folder / "main.tex", "pdf", None)
        res = app.compile_tex()
        done = res["ok"] if what == "read" else (tmp / "written.txt").exists()
        if not done:
            print(f"{what}: refused, as it should be ({ver})")
            continue
        # Defence in depth that depends on the TeX version: report it, don't fail. To tell TeX's behaviour
        # from the app's settings, run pdflatex directly too.
        env = dict(os.environ, openin_any="p", openout_any="p")
        direct = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "main.tex"], cwd=folder,
                                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                stdin=subprocess.DEVNULL, timeout=300)
        said = [ln.strip() for ln in (direct.stdout + direct.stderr).splitlines()
                if "../" in ln or "Not reading" in ln or "Not writing" in ln or ln.startswith("!")][:5]
        msg = (f"LaTeX can {what} files outside the project on {ver}. Plain pdflatex with openin_any=p: "
               f"exit {direct.returncode}, {' | '.join(said) or 'no messages'}")
        print("WARNING:", msg)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=LaTeX can {what} outside the project::{msg}", flush=True)
    app.S = app.State(proj, proj / "main.tex", "pdf", None)

    step("SyncTeX: source -> PDF -> source")
    line = SAMPLE.splitlines().index("A second paragraph, so the page has some ordinary text as well.") + 1
    box = app.synctex_view("main.tex", line)
    assert box, "synctex view found nothing"
    hit = app.synctex_lookup(box["page"], box["x"] + 20, box["y"] + box["h"] / 2)
    assert hit and hit[0] == "main.tex" and abs(hit[1] - line) <= 1, hit
    print("line", line, "->", box, "->", hit)

    step("PDF import: the text layer (pdf.js under Node.js), then the analysis")
    text_json = tmp / "text.json"
    subprocess.run(["node", str(ROOT / "tests" / "extract.js"), str(proj / "main.pdf"), str(text_json)], check=True)
    out = tmp / "imported"
    summary = pdf_import.analyse(proj / "main.pdf", json.loads(text_json.read_text(encoding="utf-8")), out, progress=print)
    print(json.dumps({k: v for k, v in summary.items() if k != "pages"}))
    assert summary["figures"] >= 1, "no figure found"
    fig = summary["pages"][0]["figures"][0]
    for f in (f"figures/{fig}.png", f"figures/{fig}.tex", "figures/original-graphics.pdf", "layers/page-01.tex",
              "import/extraction.md", "import/pages/page-01.png", "import/analysis.json"):
        assert (out / f).exists(), f"missing {f}"
    snippet = (out / "figures" / f"{fig}.tex").read_text(encoding="utf-8")
    assert "Time (s)" in snippet and "Amplitude" in snippet, "the figure's labels aren't in its snippet:\n" + snippet
    extraction = (out / "import" / "extraction.md").read_text(encoding="utf-8")
    assert "A second paragraph" in extraction and "Smoke test" in extraction

    step("the generated layers compile")
    (out / "check.tex").write_text(CHECK.replace("FIGURE", fig), encoding="utf-8")
    app.S = app.State(out, out / "check.tex", "pdf", None)
    res = app.compile_tex()
    assert res["ok"], res["errors"]

    step("the server answers, with its access key only")
    global KEY
    app.CACHE.mkdir(parents=True, exist_ok=True)
    KEY = app.load_token()                       # the same key file the server reads
    (proj / "notes.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    port = free_port()
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    server = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "latex_copilot", str(proj), "--no-browser",
                               "--port", str(port)], env=env)
    try:
        status = wait_for_server(port, server)
        assert status["mainExists"] and status["pdfExists"], status
        assert "Latex-Copilot" in request(port, "/", key=False).decode("utf-8")      # the page shell is public
        assert request(port, "/pdf-extract.js", key=False).startswith(b"//")
        code, _, body = request(port, "/api/status", key=False, full=True)
        assert code == 403 and b"locked" in body, (code, body)                       # data needs the key
        code, _, _ = request(port, "/api/compile", {}, key=False, full=True)
        assert code == 403, code
        code, headers, _ = request(port, f"/?token={KEY}", key=False, full=True)  # the link sets the cookie
        assert code == 200 or "latex_copilot=" in (headers.get("Set-Cookie") or ""), code
        code, headers, _ = request(port, "/api/raw?path=notes.html", full=True)
        assert code == 200 and headers["Content-Type"].startswith("text/plain"), headers["Content-Type"]
        claude = json.loads(request(port, "/api/claude"))
        assert {"installed", "signedIn", "login"} <= claude.keys(), claude
        print("claude:", {k: claude[k] for k in ("installed", "signedIn", "method")})
        compiled = json.loads(request(port, "/api/compile", {}))
        assert compiled["ok"], compiled
    finally:
        server.terminate()
        server.wait(timeout=30)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nAll smoke tests passed.")


def annotate(title: str, text: str):
    """Show an error on the GitHub Actions run page (logs there need a login; annotations don't)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        text = "\n".join(text.strip().splitlines()[-40:])
        print(f"::error title={title}::" + text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        annotate(f"Smoke test failed on {sys.platform}", traceback.format_exc())
        raise
