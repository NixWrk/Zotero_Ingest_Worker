# Regenerating the bundled KaTeX assets

These files are vendored so formulas render in viewers without JavaScript
(e.g. Zotero's HTML reader). They are produced once and committed:

- `katex.min.js`   — KaTeX engine, executed in embedded V8 (`mini-racer`) at runtime.
- `katex.inlined.css` — KaTeX stylesheet with every `woff2` font embedded as a
  `data:` URI, so a single `<style>` makes output fully self-contained.
- `LICENSE`         — KaTeX MIT license (redistribution requirement).

To upgrade KaTeX (Node + npm required only for this offline step, never at runtime):

```bash
mkdir _katex && cd _katex
npm install katex@<version> --no-audit --no-fund
cp node_modules/katex/dist/katex.min.js  <pkg>/assets/katex/katex.min.js
cp node_modules/katex/LICENSE            <pkg>/assets/katex/LICENSE
node -e '
const fs=require("fs"),path=require("path");
const d=path.join("node_modules","katex","dist");
let css=fs.readFileSync(path.join(d,"katex.min.css"),"utf8");
for(const f of fs.readdirSync(path.join(d,"fonts"))){
  if(!f.endsWith(".woff2"))continue;
  const u="data:font/woff2;base64,"+fs.readFileSync(path.join(d,"fonts",f)).toString("base64");
  css=css.split("fonts/"+f).join(u);
}
css=css.replace(/url\(fonts\/[^)]+\)\s*format\("(?:woff|truetype|opentype)"\),?/g,"");
fs.writeFileSync("katex.inlined.css",css);
'
cp katex.inlined.css <pkg>/assets/katex/katex.inlined.css
```

Then run `pytest tests/test_single_file_html.py -k katex` to confirm rendering.
