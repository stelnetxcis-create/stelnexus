#!/usr/bin/env node
/* StelNexus static asset build.

   Reads   app/asset_bundles.json + the sources under app/static/{css,js}
   Writes  app/static/dist/   content-hashed, minified bundles (+ hashed fonts/images that CSS points at)
           app/build/manifest.json   logical name -> hashed URL (read by app/core/assets.py)
           app/build/sw.js           service worker with the hashed shell list + build id injected

   Usage:  npm run build            build
           npm run build:check      exit 1 if the committed build is out of date with the sources
           npm run build:report     build and list CSS selectors defined more than once

   Nothing here needs the network. Hashed files never change content, so they can be cached for a
   year (deploy/Caddyfile); a new release simply produces new file names. */
import { transform } from 'esbuild';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { dedupeCss } from './css-dedupe.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const APP = path.join(ROOT, 'app');
const STATIC = path.join(APP, 'static');
const DIST = path.join(STATIC, 'dist');
const BUILD = path.join(APP, 'build');
const BUNDLES_FILE = path.join(APP, 'asset_bundles.json');
const URL_PREFIX = '/static/dist/';

const args = new Set(process.argv.slice(2));
const sha = (buf) => crypto.createHash('sha256').update(buf).digest('hex');
const short = (buf) => sha(buf).slice(0, 8);
const read = (rel) => fs.readFileSync(path.join(STATIC, rel));
const kb = (n) => (n / 1024).toFixed(1).padStart(6) + ' KB';

const bundles = JSON.parse(fs.readFileSync(BUNDLES_FILE, 'utf8'));

/* ---- input fingerprint (app/core/assets.py computes the same value to detect a stale build) ---- */
function walk(dir, base = dir) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const full = path.join(dir, e.name);
    return e.isDirectory() ? walk(full, base) : [path.relative(STATIC, full).split(path.sep).join('/')];
  });
}
function inputsHash() {
  const files = new Set(['sw.js']);
  for (const kind of ['css', 'js']) for (const list of Object.values(bundles[kind])) list.forEach((f) => files.add(f));
  for (const d of ['img', 'icons', 'fonts']) walk(path.join(STATIC, d)).forEach((f) => files.add(f));
  const h = crypto.createHash('sha256');
  for (const rel of [...files].sort()) {
    h.update(rel); h.update('\0'); h.update(read(rel)); h.update('\0');
  }
  h.update('bundles\0'); h.update(fs.readFileSync(BUNDLES_FILE));
  return h.digest('hex');
}

if (args.has('--check')) {
  const mf = path.join(BUILD, 'manifest.json');
  const current = inputsHash();
  const built = fs.existsSync(mf) ? JSON.parse(fs.readFileSync(mf, 'utf8')).inputs : null;
  if (built === current) { console.log('static build is up to date'); process.exit(0); }
  console.error('static build is STALE (sources changed since the last `npm run build`)');
  process.exit(1);
}

/* ---- output dir: keep yesterday's files so a page rendered just before a deploy can still load ---- */
fs.mkdirSync(DIST, { recursive: true });
fs.mkdirSync(BUILD, { recursive: true });
const written = new Set();
function emit(rel, buf) {
  const out = path.join(DIST, rel);
  fs.mkdirSync(path.dirname(out), { recursive: true });
  if (!fs.existsSync(out) || !fs.readFileSync(out).equals(buf)) fs.writeFileSync(out, buf);
  written.add(rel);
  return URL_PREFIX + rel;
}

/* Fonts/images referenced from CSS get hashed copies, and the url() is rewritten to match. */
const mediaCache = new Map();
function hashedMedia(staticUrl) {
  const rel = staticUrl.replace(/^\/static\//, '');
  if (mediaCache.has(rel)) return mediaCache.get(rel);
  const file = path.join(STATIC, rel);
  if (!fs.existsSync(file)) { console.warn(`  ! CSS references missing file ${staticUrl} (left as is)`); mediaCache.set(rel, staticUrl); return staticUrl; }
  const buf = fs.readFileSync(file);
  const ext = path.extname(rel);
  const out = emit(`${rel.slice(0, -ext.length)}.${short(buf)}${ext}`, buf);
  mediaCache.set(rel, out);
  return out;
}
const URL_RE = /url\(\s*(['"]?)(\/static\/(?!dist\/)[^'")\s?#]+)([?#][^'")\s]*)?\1\s*\)/g;
const rewriteUrls = (css) => css.replace(URL_RE, (_m, q, p, tail) => `url(${q}${hashedMedia(p)}${tail || ''}${q})`);

const manifest = { build: '', inputs: inputsHash(), css: {}, js: {}, preload: [], shell: [] };
const report = [];
let totalDead = 0;

for (const [name, files] of Object.entries(bundles.css)) {
  const joined = files.map((f) => read(f).toString('utf8')).join('\n');
  const d = args.has('--no-dedupe') ? { css: joined, removedDecls: 0, removedRules: 0, overlaps: [] } : dedupeCss(joined, { from: `${name}.css` });
  const min = await transform(d.css, { loader: 'css', minify: true, legalComments: 'none' });
  const body = Buffer.from(rewriteUrls(min.code));
  manifest.css[name] = emit(`${name}.${short(body)}.css`, body);
  totalDead += d.removedDecls;
  report.push({ kind: 'css', name, files, raw: Buffer.byteLength(joined), out: body, dead: d.removedDecls, overlaps: d.overlaps });
}

for (const [name, files] of Object.entries(bundles.js)) {
  const joined = files.map((f) => read(f).toString('utf8')).join('\n;\n');
  const min = await transform(joined, { loader: 'js', minify: true, target: 'es2018', legalComments: 'none' });
  const body = Buffer.from(min.code);
  manifest.js[name] = emit(`${name}.${short(body)}.js`, body);
  report.push({ kind: 'js', name, files, raw: Buffer.byteLength(joined), out: body });
}

/* Fonts the first paint needs: same hashed files the CSS @font-face rules point at, so the preload is a cache hit. */
manifest.preload = (bundles.preload || []).map((rel) => hashedMedia('/static/' + rel));
manifest.shell = bundles.shell.map(([kind, name]) => manifest[kind][name]);
manifest.build = short(Buffer.from(JSON.stringify([manifest.css, manifest.js, manifest.preload, manifest.shell])));

/* Service worker: same source, with the build id and hashed shell injected. The cache name changes
   with every build, so nobody hand-bumps a version string any more. */
let sw = fs.readFileSync(path.join(STATIC, 'sw.js'), 'utf8');
const swBefore = sw;
sw = sw.replace(/\/\*@build\*\/'[^']*'/, `/*@build*/'${manifest.build}'`)
       .replace(/\/\*@shell\*\/\[[^\]]*\]/, `/*@shell*/${JSON.stringify(manifest.shell)}`);
if (sw === swBefore) { console.error('sw.js is missing its /*@build*/ or /*@shell*/ placeholders'); process.exit(1); }
fs.writeFileSync(path.join(BUILD, 'sw.js'), sw);
fs.writeFileSync(path.join(BUILD, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');

/* Prune hashed files this build no longer produces, but only once they are a day old. */
let pruned = 0;
for (const rel of walk(DIST).map((f) => f.replace(/^dist\//, ''))) {
  if (written.has(rel)) continue;
  const full = path.join(DIST, rel);
  if (Date.now() - fs.statSync(full).mtimeMs > 24 * 3600 * 1000) { fs.unlinkSync(full); pruned++; }
}
for (const d of fs.readdirSync(DIST, { withFileTypes: true })) {   // remove emptied sub-folders
  if (d.isDirectory() && walk(path.join(DIST, d.name)).length === 0) fs.rmSync(path.join(DIST, d.name), { recursive: true });
}

/* ---- summary ---- */
const srcSizes = (files) => files.reduce((n, f) => n + read(f).length, 0);
let before = 0, after = 0, afterGz = 0;
console.log('bundle                          sources      built    gzip   brotli');
for (const r of report) {
  const src = srcSizes(r.files);
  const gz = zlib.gzipSync(r.out, { level: 9 }).length;
  const br = zlib.brotliCompressSync(r.out).length;
  before += src; after += r.out.length; afterGz += gz;
  console.log(`${(r.kind + ':' + r.name).padEnd(28)} ${kb(src)} ${kb(r.out.length)} ${kb(gz)} ${kb(br)}${r.dead ? `   (${r.dead} dead CSS declarations dropped)` : ''}`);
}
console.log(`\nbuild ${manifest.build}: ${Object.keys(manifest.css).length} css + ${Object.keys(manifest.js).length} js bundles` +
  `, ${totalDead} dead CSS declarations removed${pruned ? `, ${pruned} old files pruned` : ''}`);

if (args.has('--report')) {
  const site = report.find((r) => r.kind === 'css' && r.name === 'site');
  console.log('\nCSS selectors defined more than once in the site bundle (the overlap between app/luxe/arena/royale):');
  for (const o of site.overlaps.slice(0, 60)) console.log(`  ${String(o.definitions).padStart(2)}x  ${o.selector}`);
  if (site.overlaps.length > 60) console.log(`  ... and ${site.overlaps.length - 60} more`);
}
