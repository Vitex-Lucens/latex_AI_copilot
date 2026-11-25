# Third-party software

Latex-Copilot bundles these libraries, unmodified, in `latex_copilot/static/vendor/`:

| Library | Version | License | Files |
|---|---|---|---|
| [PDF.js](https://github.com/mozilla/pdf.js) (Mozilla Foundation) | 3.11.174 | [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) | `pdf.min.js`, `pdf.worker.min.js` |
| [CodeMirror 5](https://github.com/codemirror/codemirror5) (Marijn Haverbeke and others) | 5.65.16 | [MIT](https://github.com/codemirror/codemirror5/blob/master/LICENSE) | `codemirror.min.js`, `codemirror.min.css`, `stex.min.js`, `dialog.min.js`, `dialog.min.css`, `search.min.js`, `searchcursor.min.js`, `jump-to-line.min.js` |

The PDF.js files keep their original license header.

CodeMirror's MIT license:

> Copyright (C) 2017 by Marijn Haverbeke <marijn@haverbeke.berlin> and others
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
