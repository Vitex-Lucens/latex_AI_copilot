# Security

## Reporting a problem

Please report security problems privately, using GitHub's **Report a vulnerability** button on this repository's **Security** tab, rather than in a public issue.

## How Latex-Copilot is protected

Latex-Copilot is a local app: a small web server on your own computer, shown in your browser, with Claude Code doing the AI work under your own Claude account.

| Risk | What Latex-Copilot does |
|---|---|
| Other programs, other users on the computer, or websites using the local server | It listens only on `127.0.0.1`. Every request needs this user's access key, which the app puts in the link it opens and the browser then keeps in a `SameSite=Strict`, `HttpOnly` cookie. The key file (`~/.cache/latex-copilot/access-key`) is readable only by you. Requests from other websites are refused (Host and Origin checks; state changes need a JSON body, which browsers can't send cross-site without permission). |
| A project's `latexmkrc` (a Perl program) running on compile | Ignored, unless you start the app with `--latexmkrc` for a project you trust. |
| A document reading other files on the computer into the PDF | Partly. With TeX Live the app sets `openin_any=p` (and `openout_any=p`), which older TeX Live (tested: 2021–2022) enforces, so a document can only read files in its project and in TeX itself. **TeX Live 2026 (tested on macOS) and MiKTeX don't enforce it**, so there a document can still `\input` other files it can name. Writing outside the project stays blocked, and shell escape stays at TeX's restricted default. Only compile documents you trust, and look at a PDF before sharing it. |
| A malicious PDF running code in the app | PDF.js never evaluates code from PDFs (`isEvalSupported: false`, the fix for CVE-2024-4367). |
| HTML or scripts in a project running in the app | Served as plain text; SVG images can't run scripts. |
| Instructions hidden in a document (prompt injection) | Claude Code gets only file tools (Read, Edit, Write, Glob, Grep), confined to the project folder: no commands, no web access, no plugins or MCP servers. Every edit is highlighted and can be undone. |
| Untrusted PDFs in Ghostscript | Always run with `-dSAFER`; the installers install current versions. |

## What leaves your computer

What Claude needs for each request goes to Anthropic through your Claude account: your messages, the project files Claude reads, images of annotated areas, and for a PDF import the extracted text and page images. Anthropic's terms for your account apply. Latex-Copilot itself sends nothing anywhere else.

Locally, the last prompt sent to Claude is kept in `~/.cache/latex-copilot/last-prompt.md` for troubleshooting, and deleted files go to `~/.cache/latex-copilot/trash`.

## Installing

The one-line installers download the app from this repository over HTTPS, and, only after asking, Python, TeX, Ghostscript and Claude Code from their official sources (Homebrew, apt, winget, Artifex, Anthropic). To install an exact version, set `LATEX_COPILOT_REF` to a release tag.
