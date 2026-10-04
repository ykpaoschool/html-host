// Bundles the CodeMirror 6 editor into the single asset the app serves from
// static/vendor/. Run by hand (`npm run build`) when the editor dependency set
// changes; the output is committed, so building the app itself never needs node.
// See editor-implementation.md 1.2.
import { build } from "esbuild";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

// Default straight into the served location so `npm run build` is one step and
// no dist/ staging directory exists to keep in sync. Resolved against this
// file, not the cwd. An explicit argument still overrides it.
const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = process.argv[2] || resolve(HERE, "../../static/vendor/codemirror.js");
mkdirSync(dirname(OUT), { recursive: true });

const result = await build({
  entryPoints: [resolve(HERE, "src/entry.js")],
  outfile: OUT,
  bundle: true,
  format: "iife",
  globalName: "HtmlHostEditor",
  minify: true,
  target: ["es2020"],
  legalComments: "none",
  metafile: true,
  logLevel: "warning",
});

const code = readFileSync(OUT);
const gz = gzipSync(code);
console.log(`outfile      : ${OUT}`);
console.log(`bytes        : ${code.length}  (${(code.length / 1024).toFixed(1)} KiB)`);
console.log(`bytes gzip   : ${gz.length}  (${(gz.length / 1024).toFixed(1)} KiB)`);
console.log(`inputs       : ${Object.keys(result.metafile.inputs).length} modules bundled`);
console.log(`outputs      : ${Object.keys(result.metafile.outputs).length} file(s)`);

const text = code.toString("utf8");
const dynamicImport = [...text.matchAll(/\bimport\s*\(/g)].length;
const bareRequire = [...text.matchAll(/\brequire\s*\(\s*["'`]/g)].length;
const sourceMapRef = /sourceMappingURL/.test(text);
console.log(`dynamic import() occurrences : ${dynamicImport}`);
console.log(`bare require("...")          : ${bareRequire}`);
console.log(`sourceMappingURL present     : ${sourceMapRef}`);
console.log(`top-level global assignment  : ${/var HtmlHostEditor\s*=/.test(text) || /HtmlHostEditor\s*=/.test(text)}`);
