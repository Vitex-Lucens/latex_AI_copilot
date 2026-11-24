# tex-annotator

An Overleaf-style LaTeX editor that runs on your own computer, with Claude built in.
You get your project's files, the source and the compiled PDF side by side, plus a chat with Claude that can read and edit the whole project.
You can also draw on the PDF and pin comments to it, and Claude makes the matching changes in the `.tex` files.
No LaTeX source? Import a PDF, and Claude rebuilds it as an editable LaTeX document.

![tex-annotator](docs/screenshot.png)

## What you need

| | How to get it |
|---|---|
| **macOS or Linux** | On Windows, use it inside WSL. |
| **Python 3.10+** | macOS: `brew install python` · Ubuntu/Debian: `sudo apt install python3` (usually already installed) |
| **TeX Live** (with `latexmk` and `synctex`) | macOS: install [MacTeX](https://tug.org/mactex/) · Ubuntu/Debian: `sudo apt install texlive-latex-extra latexmk` |
| **Claude Code**, signed in | Install it from [claude.com/claude-code](https://claude.com/claude-code), then run `claude` once in a terminal and sign in. tex-annotator uses that login, so it doesn't need an API key. |
| **Ghostscript** (only to import PDFs) | macOS: included with MacTeX, or `brew install ghostscript` · Ubuntu/Debian: `sudo apt install ghostscript` (often already installed) |

## Install

1. Unzip `tex-annotator-<version>.zip`.
2. In a terminal, go into the unzipped folder and run:

   ```bash
   ./install.sh
   ```

   This puts the app in `~/.local/share/tex-annotator` and adds a `tex-annotator` command. You don't need admin rights.
   It finishes by checking the requirements above and telling you if anything is missing.
   If it offers to add `~/.local/bin` to your PATH, say yes, then open a new terminal.

3. To check your setup at any time, run `tex-annotator --check`.

To **update**, unzip the new version and run `./install.sh` again. To **remove** the app, run `./install.sh --uninstall`.

## Use

```bash
cd path/to/your/latex/project
tex-annotator
```

Your browser opens at `http://127.0.0.1:8765`. Leave the terminal open while you work, and press **Ctrl+C** in it to stop.
tex-annotator finds the main document (the file with `\documentclass`) by itself. To pick a specific file, run `tex-annotator path/to/main.tex`.

| Area | What you can do |
|---|---|
| **Project files** (left) | Open files in tabs. Create files and folders, upload or drag in images, rename, delete. **★** makes a file the *main* document. Deleted files go to `~/.cache/tex-annotator/trash`, so you can get them back. |
| **Editor** | LaTeX highlighting, with separate undo per tab. **Ctrl+S** saves everything and recompiles. **Ctrl+F** finds text. **Ctrl+Enter** shows the cursor's line in the PDF. |
| **PDF** | **Select** tool: click any text to jump to its source line, in whichever file it lives. **Pencil** and **Comment**: mark up the page for Claude. **Eraser** removes marks. Zoom with **−/+**. |
| **Claude** (right) | Ask questions ("is the notation in chapter 2 consistent?") or ask for edits ("shorten the abstract", "split the results into its own file"). Your pencil marks and comments are sent with your next message. With **Include position** ticked, Claude also sees your current file, line and any selected text. Claude's edits are highlighted in green, the PDF recompiles, and **Undo Claude edit** reverts the last one. **Stop** interrupts Claude; **New chat** starts a fresh conversation. Under the message box, pick the **model** (Opus writes best, Sonnet costs about half as much, Haiku is much cheaper and fine for small fixes) and the **effort** (low = quicker and cheaper). |
| **Layout** | Drag the thin bars between panes to resize them (double-click a bar to reset). The buttons at the far left and right hide the file tree and the chat. Choose Light, Dark or Auto theme; the **◐** button switches the PDF to a dark blue-green reading mode. |

### Import a PDF (no LaTeX source needed)

```bash
tex-annotator path/to/report.pdf
```

This makes a project folder next to the PDF (`report-latex/`) and opens it. Press **Convert to LaTeX**. You can also run `tex-annotator` in an empty folder and choose or drop a PDF on the page.

Claude rebuilds the document as LaTeX that looks like the original and is easy to edit:

- **Text stays text.** It comes from the PDF's own text layer, so the wording is exact, and Claude turns it into real headings, paragraphs, lists, tables, maths, footnotes and links.
- **Figures become two layers.** Each figure is cut out as an image with its text removed (`figures/fig-02-1.png`), and its labels (axis titles, legends, callouts) are put back on top as real, editable text (`figures/fig-02-1.tex`).
- **Designed pages** (covers, posters, forms) can use a positioned copy instead (`layers/page-01.tex`): the page's graphics with every line of text placed exactly where it was.
- **Scanned pages** (no text layer) are read from the page images.

How it works: the app first takes the PDF apart on your computer. It measures the page layout, fonts, colours, columns, tables and figures, and writes them into `import/extraction.md`, next to an image of every page. Claude then follows a written conversion process ([`tex_annotator/prompts/pdf-import.md`](tex_annotator/prompts/pdf-import.md)) a few pages at a time. After each part the app compiles the result, and, if **Check the result against the original** is ticked, Claude compares each compiled page with the original and fixes differences. When it's done, the **Original** button (or **O**) switches the PDF pane between your new document and the PDF you imported.

Conversion uses your Claude account like any other request: the model chosen under the chat box does the work. Opus is the most faithful; Sonnet costs about half as much and suits long documents. A 10-page document takes a few minutes. Generated files the final document doesn't use are moved to `import/unused/`, and the `import/` folder can be deleted once you're happy.

### Options

```
tex-annotator [PROJECT | MAIN.tex | FILE.pdf] [--engine pdf|xe|lua] [--model MODEL] [--port N] [--no-browser]
```

- `--engine xe` or `--engine lua` compiles with XeLaTeX or LuaLaTeX. The default is pdfLaTeX. A first line `% !TEX program = xelatex` (or `lualatex`) in the main file does the same for that document.
- `--model` picks the Claude model. By default it uses your Claude Code default.
- `--port` sets the port. If 8765 is busy, it uses the next free one automatically.

## Good to know

- **Claude edits files directly.** It can only read and edit files inside the project folder, and it can't run commands. Every edit is highlighted and can be undone with **Undo Claude edit**. Using git in the project is still a good idea.
- **Usage:** Claude runs on your own Claude account, so it counts towards that account's usage. Each reply shows its model and cost. The header of the chat shows how big the conversation has become and what it has used so far. As a guide, an edit from PDF annotations costs about 2–3 cents on Sonnet, and a quick question on Haiku about a tenth of a cent.
- **Keeping usage low:**
  - Use **Haiku** or **low effort** for small fixes.
  - Press **New chat** when you move on to something unrelated: every message re-sends the whole conversation, and the meter turns orange when it gets long.
  - Your PDF annotations are sent compactly: the text they cover, the matching `.tex` lines, and a small cropped image only for pencil marks.
  - Claude starts with only the file tools this app needs. None of your other Claude Code plugins, connectors or skills are loaded.
- **Privacy:** the app runs on your computer and only accepts connections from your own browser. Only what you send to Claude leaves your machine: your messages, the files it reads, and images of the pages you annotated. Importing a PDF sends Claude the extracted text and images of the pages.
- **Problems?** Run `tex-annotator --check`. If the page shows "Lost connection", look at the terminal where tex-annotator is running, and at `~/.cache/tex-annotator/server.log`. To see exactly what was last sent to Claude, open `~/.cache/tex-annotator/last-prompt.md`.

## For developers

You can run it straight from this folder with `python3 -m tex_annotator [project]`; it only uses the Python standard library.
The PDF analysis can be run on its own, which is handy when working on it: `python3 -m tex_annotator.pdf_import original.pdf text.json out-folder`, where `text.json` is what `static/pdf-extract.js` returns for the PDF.
To try the multi-part conversion on a short document, start the app with `TEX_ANNOTATOR_PART_PAGES=2` (or `TEX_ANNOTATOR_PART_WORDS`) set.
It also works as a normal Python package: `pipx install .`
The bundled JavaScript libraries are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
