// Reads a PDF's text layer for "Import PDF": every text run with its exact position, size,
// direction and font, plus page sizes, links, bookmarks and document info. The server turns this
// into lines, blocks, styles and figure labels (latex_copilot/pdf_import.py).
//
// Coordinates are PDF points in the displayed page (crop box and page rotation applied), with the
// origin at the TOP-left and y pointing down, which matches how Ghostscript renders the pages.
// Runs in the browser (window.extractPdfText) and in Node for tests (module.exports).
(function (root) {
  const r2 = v => Math.round(v * 100) / 100;
  const r3 = v => Math.round(v * 1000) / 1000;

  async function outlineEntries(doc, items, level, acc) {
    for (const it of items || []) {
      if (acc.length >= 500) break;
      let page = null;
      try {
        let dest = it.dest;
        if (typeof dest === "string") dest = await doc.getDestination(dest);
        if (Array.isArray(dest) && dest[0] && typeof dest[0] === "object") page = (await doc.getPageIndex(dest[0])) + 1;
        else if (Array.isArray(dest) && Number.isInteger(dest[0])) page = dest[0] + 1;
      } catch { /* unresolvable destination: keep the title anyway */ }
      acc.push([level, (it.title || "").trim(), page]);
      await outlineEntries(doc, it.items, level + 1, acc);
    }
    return acc;
  }

  async function extractPdfText(pdfjsLib, data, onProgress) {
    const doc = await pdfjsLib.getDocument({ data, isEvalSupported: false }).promise;
    const out = { numPages: doc.numPages, pages: [], fonts: {}, outline: [], info: {} };
    try {
      const m = await doc.getMetadata();
      const i = (m && m.info) || {};
      out.info = { title: i.Title || "", author: i.Author || "", subject: i.Subject || "", creator: i.Creator || "", producer: i.Producer || "" };
    } catch { /* no metadata */ }
    try { out.outline = await outlineEntries(doc, await doc.getOutline(), 1, []); } catch { /* no bookmarks */ }

    for (let n = 1; n <= doc.numPages; n++) {
      const page = await doc.getPage(n);
      const vp = page.getViewport({ scale: 1 });
      const tc = await page.getTextContent();
      try { await page.getOperatorList(); } catch { /* fonts stay unnamed */ }   // loads the fonts, so their real names are known
      const items = [];
      for (const it of tc.items) {
        if (typeof it.str !== "string" || !it.str.length) continue;
        const t = pdfjsLib.Util.transform(vp.transform, it.transform);
        const size = Math.hypot(t[2], t[3]);
        if (!(size > 0.5)) continue;                      // invisible or degenerate text
        const len = Math.hypot(t[0], t[1]) || 1;
        const fid = it.fontName;
        if (!(fid in out.fonts)) {
          let name = "";
          try { const f = page.commonObjs.get(fid); name = (f && (f.name || f.fallbackName)) || ""; } catch { /* not loaded */ }
          out.fonts[fid] = name || (tc.styles[fid] && tc.styles[fid].fontFamily) || fid;
        }
        // [text, x, y (baseline origin), advance width, font size, direction x, direction y, font id]
        items.push([it.str, r2(t[4]), r2(t[5]), r2(it.width), r2(size), r3(t[0] / len), r3(t[1] / len), fid]);
      }
      const links = [];
      try {
        for (const a of await page.getAnnotations()) {
          if (a.subtype !== "Link" || !a.url || !a.rect) continue;
          const [x0, y0] = vp.convertToViewportPoint(a.rect[0], a.rect[1]);
          const [x1, y1] = vp.convertToViewportPoint(a.rect[2], a.rect[3]);
          links.push([r2(Math.min(x0, x1)), r2(Math.min(y0, y1)), r2(Math.max(x0, x1)), r2(Math.max(y0, y1)), a.url]);
        }
      } catch { /* no annotations */ }
      out.pages.push({ w: r2(vp.width), h: r2(vp.height), items, links });
      page.cleanup();
      if (onProgress) onProgress(n, doc.numPages);
    }
    await doc.destroy();
    return out;
  }

  if (typeof module === "object" && module.exports) module.exports = { extractPdfText };
  else root.extractPdfText = extractPdfText;
})(typeof window !== "undefined" ? window : globalThis);
