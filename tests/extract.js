// Read a PDF's text layer the way the browser does (static/pdf-extract.js), for tests and for working on the
// PDF analysis:  node tests/extract.js input.pdf output.json
const fs = require("fs");
const path = require("path");
const STATIC = path.join(__dirname, "..", "tex_annotator", "static");
const pdfjsLib = require(path.join(STATIC, "vendor", "pdf.min.js"));
pdfjsLib.GlobalWorkerOptions.workerSrc = path.join(STATIC, "vendor", "pdf.worker.min.js");
const { extractPdfText } = require(path.join(STATIC, "pdf-extract.js"));

const [, , input, output] = process.argv;
if (!input || !output) {
  console.error("usage: node tests/extract.js input.pdf output.json");
  process.exit(2);
}
extractPdfText(pdfjsLib, new Uint8Array(fs.readFileSync(input))).then(r => {
  fs.writeFileSync(output, JSON.stringify(r));
  console.log(`${input}: ${r.pages.length} page(s), ${r.pages.reduce((n, p) => n + p.items.length, 0)} text items`);
}).catch(e => { console.error(e); process.exit(1); });
