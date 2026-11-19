/* Removes CSS declarations that can never take effect because a LATER rule with the identical
   selector (in the identical at-rule context) sets the same property.

   Why this is safe: same selector => same elements and same specificity; the later rule wins by
   source order, so the earlier declaration is dead wherever it would have applied. We deliberately
   do NOT try to merge different selectors or move declarations (that can change cascade order).
   Conservative guards: !important is respected, and an earlier value is kept as a fallback when
   the later value uses syntax an older browser might reject (dvh, color-mix, oklch, ...).
   Rules inside @keyframes / @layer / nested rules are left alone. */
import postcss from 'postcss';

const SCOPE_OK = new Set(['media', 'supports', 'container']);
const RISKY_VALUE = /\b\d*\.?\d+(dvh|svh|lvh|dvw|svw|lvw|cqw|cqh)\b|color-mix\(|oklch\(|oklab\(|\blab\(|\blch\(|light-dark\(|anchor\(|image-set\(|-webkit-|-moz-|env\(/i;
const norm = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\s+/g, ' ').trim();

function contextKey(rule) {
  const parts = [];
  for (let p = rule.parent; p && p.type !== 'root'; p = p.parent) {
    if (p.type === 'rule') return null;                         // nested rule: skip
    if (p.type !== 'atrule' || !SCOPE_OK.has(p.name.toLowerCase())) return null;
    parts.push(`@${p.name.toLowerCase()} ${norm(p.params)}`);
  }
  return parts.reverse().join(' > ');
}
const propKey = (d) => (d.prop.startsWith('--') ? d.prop : d.prop.toLowerCase());

export function dedupeCss(source, { from = 'bundle.css' } = {}) {
  const root = postcss.parse(source, { from });
  const groups = new Map();
  root.walkRules((rule) => {
    const ctx = contextKey(rule);
    if (ctx === null) return;
    const key = `${ctx} {{ ${norm(rule.selector)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(rule);
  });

  let removedDecls = 0, removedRules = 0;
  const overlaps = [];
  for (const [key, rules] of groups) {
    if (rules.length < 2) continue;
    overlaps.push({ selector: key.replace(' {{ ', ' '), definitions: rules.length });
    // For each property, remember the LAST declaration that sets it.
    const last = new Map();
    rules.forEach((rule, ri) => rule.each((d) => {
      if (d.type === 'decl') last.set(propKey(d), { ri, d });
    }));
    rules.forEach((rule, ri) => {
      rule.each((d) => {
        if (!d || d.type !== 'decl') return;
        const winner = last.get(propKey(d));
        if (!winner || winner.ri === ri) return;               // last definition lives in this rule
        if (winner.ri < ri) return;                            // cannot happen, defensive
        if (d.important && !winner.d.important) return;        // earlier !important beats later normal
        if (RISKY_VALUE.test(winner.d.value)) return;          // keep earlier value as fallback
        d.remove();
        removedDecls++;
      });
    });
  }

  // Drop rules (and at-rules) that ended up empty.
  const prune = (node) => {
    if (node.type === 'rule' && node.nodes && node.nodes.every((n) => n.type === 'comment') ) {
      node.remove(); removedRules++; return;
    }
    if (node.type === 'atrule' && SCOPE_OK.has(node.name.toLowerCase()) && node.nodes) {
      [...node.nodes].forEach(prune);
      if (node.nodes.every((n) => n.type === 'comment')) node.remove();
    }
  };
  [...root.nodes].forEach(prune);

  overlaps.sort((a, b) => b.definitions - a.definitions);
  return { css: root.toString(), removedDecls, removedRules, overlaps };
}
