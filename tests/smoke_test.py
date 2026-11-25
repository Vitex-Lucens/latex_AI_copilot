#!/usr/bin/env python3
"""
Smoke test for Latex-Copilot on Windows, macOS and Linux: everything that depends on the system. It checks
compiling with latexmk, SyncTeX both ways, the PDF import analysis (with the browser's pdf.js text
extraction run under Node.js), that the generated layered LaTeX compiles, that the server answers, and the
security measures: a project's latexmkrc doesn't run, files from outside the project never reach a PDF until
allowed, documents can't write outside the project, a document can't pick LuaLaTeX by itself, biber doesn't
run after outside reads, the server needs its access key, and project HTML is served as plain text.
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
os.environ["LATEX_COPILOT_NO_UPDATE_CHECK"] = "1"     # the server started below needn't ask GitHub
from latex_copilot import app, pdf_import, texsafety  # noqa: E402

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

    step("files outside the project never reach a PDF, until allowed")
    app.TRUST_FILE = tmp / "trust.json"                 # not the real one in ~/.cache
    ver = subprocess.run(["pdflatex", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    secret = tmp / "secret.txt"
    secret.write_text("SECRET-OUTSIDE-THE-PROJECT", encoding="utf-8")
    peek = tmp / "read"
    peek.mkdir()
    (peek / "main.tex").write_text("\\documentclass{article}\\begin{document}\\input{../secret.txt}\\end{document}\n",
                                   encoding="utf-8")
    app.S = app.State(peek, peek / "main.tex", "pdf", None)
    res = app.compile_tex()
    assert not res["ok"] and not (peek / "main.pdf").exists(), "a file from outside the project reached the PDF"
    listed = [f["path"] for f in (res.get("blocked") or {}).get("files", [])]
    assert any(p.endswith("secret.txt") for p in listed), res
    print("blocked:", listed, f"({ver})")
    app.trust_project(files=app.S.outside_found)        # what "Allow" does
    res = app.compile_tex(force=True)
    assert res["ok"] and (peek / "main.pdf").exists(), res["errors"]
    print("compiles once allowed")

    step("documents can't write outside the project")
    poke = tmp / "write"
    poke.mkdir()
    (poke / "main.tex").write_text("\\documentclass{article}\\newwrite\\w\\immediate\\openout\\w=../written.txt "
                                   "\\immediate\\write\\w{x}\\immediate\\closeout\\w\\begin{document}x\\end{document}\n",
                                   encoding="utf-8")
    app.S = app.State(poke, poke / "main.tex", "pdf", None)
    app.compile_tex()
    assert not (tmp / "written.txt").exists(), "LaTeX wrote a file outside the project"
    print("refused")

    step("a document can't pick LuaLaTeX by itself")
    lua = tmp / "lua"
    lua.mkdir()
    (lua / "main.tex").write_text("% !TEX program = lualatex\n\\documentclass{article}\\begin{document}x\\end{document}\n",
                                  encoding="utf-8")
    app.S = app.State(lua, lua / "main.tex", "pdf", None)
    res = app.compile_tex()
    assert not res["ok"] and (res.get("blocked") or {}).get("kind") == "lualatex", res
    print("asks first")

    step("biber (which can download) doesn't run after outside reads")
    guard = [sys.executable, str(ROOT / "latex_copilot" / "texsafety.py"), "biber", "main.bcf"]
    env = dict(os.environ, LATEX_COPILOT_ROOT=str(peek), LATEX_COPILOT_TRUST=str(tmp / "no-trust.json"))
    (peek / "main.fls").write_text(f"PWD {peek}\nINPUT {secret}\n", encoding="utf-8")
    run = subprocess.run(guard, cwd=peek, env=env, capture_output=True, text=True)
    assert run.returncode == 3 and "didn't run biber" in run.stderr, (run.returncode, run.stderr)
    (peek / "main.fls").write_text(f"PWD {peek}\nINPUT {peek / 'main.tex'}\n", encoding="utf-8")
    run = subprocess.run(guard, cwd=peek, env=env, capture_output=True, text=True)
    assert run.returncode != 3, run.stderr
    print("guarded")
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
        assert request(port, "/logo.png", key=False).startswith(b"\x89PNG")             # the logo, for the start page
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
