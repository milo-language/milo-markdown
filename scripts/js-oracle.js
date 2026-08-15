// Render stdin with commonmark.js — the CommonMark spec's own reference
// implementation — for scripts/fuzz-oracle.py --js to adjudicate with.
//
//   npm i commonmark
//   python3 scripts/fuzz-oracle.py --n 5000 --js "node scripts/js-oracle.js"
//
// cmark is the always-available second opinion, but where cmark and
// commonmark.js disagree, commonmark.js is the one the spec is written against.
const cm = require("commonmark");
const reader = new cm.Parser();
const writer = new cm.HtmlRenderer();
let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => process.stdout.write(writer.render(reader.parse(input))));
