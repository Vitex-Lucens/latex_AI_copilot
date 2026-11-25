# Security

## Reporting a problem

Please report security problems privately, using GitHub's **Report a vulnerability** button on this repository's **Security** tab, rather than in a public issue.

## How Latex-Copilot is protected

Latex-Copilot is a local app: a small web server on your own computer, shown in your browser, with Claude Code doing the AI work under your own Claude account.

| Risk | What Latex-Copilot does |
|---|---|
| Other programs, other users on the computer, or websites using the local server | It listens only on `127.0.0.1`. Every request needs this user's access key, which the app puts in the link it opens and the browser then keeps in a `SameSite=Strict`, `HttpOnly` cookie. Requests from other websites are refused (Host and Origin checks; state changes need a JSON body, which browsers can't send cross-site without permission). The key, the log and the last prompt are kept in `~/.cache/latex-copilot`, which only you can read, and the key is never written to the log. |
| A project's `latexmkrc` (a Perl program) running on compile | Ignored, unless you start the app with `--latexmkrc` for a project you trust. |
| A document copying other files on the computer into its PDF (`\input{~/.ssh/…}`, an image from elsewhere) | Checked after every compile, using the list of files TeX records. If the document read files outside its project, other than TeX's own files and fonts, the PDF is removed and the PDF pane lists the files, until you allow them for that project. What you allow is kept in `~/.cache/latex-copilot/trust.json`, outside every project, so neither a document nor Claude can allow anything. biber, which can download from the internet, doesn't run for such a document, so the files can't be sent anywhere. Older TeX Live (tested: 2022) also refuses these reads itself; TeX Live 2026 and MiKTeX 26 don't. |
| A document writing files elsewhere on the computer | Blocked by TeX (tested: TeX Live 2022 and 2026, MiKTeX 26). Shell escape stays at TeX's restricted default. |
| A document running code with LuaLaTeX | LuaLaTeX documents can run Lua code that reads and changes any file, beyond TeX's own limits. So a document can't pick LuaLaTeX by itself: it needs `--engine lua`, or your OK for that project in the PDF pane. XeLaTeX, which runs no code, is used without asking. |
| A malicious PDF running code in the app | PDF.js never evaluates code from PDFs (`isEvalSupported: false`, the fix for CVE-2024-4367). |
| HTML or scripts in a project running in the app | Served as plain text; SVG images can't run scripts. |
| Instructions hidden in a document (prompt injection) | Claude Code gets only file tools (Read, Edit, Write, Glob, Grep), confined to the project folder: no commands, no web access, no plugins or MCP servers. It can't allow outside files or LuaLaTeX either: only you can, in the PDF pane. Every edit is highlighted and can be undone. |
| Untrusted PDFs in Ghostscript | Always run with `-dSAFER`; the installers install current versions. |

## What leaves your computer

What Claude needs for each request goes to Anthropic through your Claude account: your messages, the project files Claude reads, images of annotated areas, and for a PDF import the extracted text and page images. Anthropic's terms for your account apply. Apart from that, Latex-Copilot only asks GitHub (`api.github.com`), at most once a day, for the latest version number; nothing about you or your documents is sent. `--no-update-check` or `LATEX_COPILOT_NO_UPDATE_CHECK=1` turns that off.

Locally, the last prompt sent to Claude is kept in `~/.cache/latex-copilot/last-prompt.md` for troubleshooting, and deleted files go to `~/.cache/latex-copilot/trash`.

## Installing

The one-line installers download the app from this repository over HTTPS, and, only after asking, Python, TeX, Ghostscript and Claude Code from their official sources (Homebrew, apt, winget, Artifex, Anthropic). Every [release](https://github.com/Vitex-Lucens/latex_AI_copilot/releases) is built by GitHub Actions after the tests pass on Windows, macOS and Ubuntu. Its installers install exactly that version, and `SHA256SUMS.txt` lists the SHA-256 checksums of its files. (From a copy of the repository, `LATEX_COPILOT_REF` picks the version to download.)
