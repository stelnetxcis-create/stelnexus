#!/usr/bin/env node
/* One-off source merge: rewrites the four CSS source files into two layers, with every
   declaration a later same-selector rule overrides removed (the same rule the build applies).

     css/base.css   = app.css + luxe.css        (login pages AND the console load this)
     css/theme.css  = arena.css + royale.css    (console only, loaded after base)

   Why two and not one: the login pages load only app+luxe, so luxe cannot lose declarations that
   arena/royale override. Rules are never reordered and different selectors are never combined, so
   the cascade is untouched; `npm run build` must produce byte-identical bundles before and after.

   Usage: node tools/css-merge.mjs         (writes base.css / theme.css, deletes the four old files) */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { dedupeCss } from './css-dedupe.mjs';

const CSS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'app', 'static', 'css');
const layers = {
  'base.css': ['app.css', 'luxe.css'],
  'theme.css': ['arena.css', 'royale.css'],
};

for (const [out, parts] of Object.entries(layers)) {
  const joined = parts.map((f) => `/* ===== from ${f} ===== */\n` + fs.readFileSync(path.join(CSS, f), 'utf8')).join('\n');
  const r = dedupeCss(joined, { from: out });
  fs.writeFileSync(path.join(CSS, out), r.css.replace(/\n{3,}/g, '\n\n'));
  console.log(`${out}: merged ${parts.join(' + ')}; dropped ${r.removedDecls} overridden declarations, ${r.removedRules} empty rules`);
}
for (const f of Object.values(layers).flat()) fs.rmSync(path.join(CSS, f));
