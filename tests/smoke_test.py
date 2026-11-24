#!/usr/bin/env python3
"""
Smoke test for tex-annotator on Windows, macOS and Linux: everything that depends on the system. It checks
compiling with latexmk, SyncTeX both ways, the PDF import analysis (with the browser's pdf.js text
extraction run under Node.js), that the generated layered LaTeX compiles, and that the server answers.
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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tex_annotator import app, pdf_import  # noqa: E402

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


def request(port, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"} if body is not None else {},
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def wait_for_server(port, proc, seconds=180):
    end = time.time() + seconds
    while time.time() < end:
        if proc.poll() is not None:
            raise AssertionError(f"the server exited with code {proc.returncode}")
        try:
            return json.loads(request(port, "/api/status"))
        except OSError:
            time.sleep(1)
    raise AssertionError("the server didn't answer")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="texann-smoke-"))   # deliberately not resolved: the app must cope
    proj = tmp / "project"
    proj.mkdir()
    (proj / "main.tex").write_text(SAMPLE, encoding="utf-8")
    print(f"tex-annotator {app.__version__} · Python {sys.version.split()[0]} · {sys.platform}")
    print("claude:", app.claude_command(), "· gs:", pdf_import.gs_path())

    step("compile with latexmk")
    app.S = app.State(proj, proj / "main.tex", "pdf", None)
    res = app.compile_tex()
    assert res["ok"], res["errors"]
    assert (proj / "main.pdf").exists()

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

    step("the server answers")
    port = free_port()
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    server = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "tex_annotator", str(proj), "--no-browser",
                               "--port", str(port)], env=env)
    try:
        status = wait_for_server(port, server)
        assert status["mainExists"] and status["pdfExists"], status
        assert "TeX Annotator" in request(port, "/").decode("utf-8")
        assert request(port, "/pdf-extract.js").startswith(b"//")
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
