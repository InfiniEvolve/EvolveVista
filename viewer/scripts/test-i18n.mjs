// Every interface string of the campaign page has a Chinese text, here or in TraceVista's table, and every text
// here is still used: literal keys of t() calls (both branches of a conditional) and the serverMessages table.
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import ts from 'typescript';

const src = new URL('../src/', import.meta.url);
const own = JSON.parse(readFileSync(new URL('i18n.zh.json', src), 'utf8'));
const tracevista = JSON.parse(readFileSync(new URL('../../third_party/TraceVista/viewer/src/trajectory/i18n.zh.json', import.meta.url), 'utf8'));
const keys = new Set();
const literals = (node, out) => {
  if (ts.isStringLiteralLike(node)) out.add(node.text);
  else if (ts.isConditionalExpression(node)) { literals(node.whenTrue, out); literals(node.whenFalse, out); }
  else if (ts.isParenthesizedExpression(node)) literals(node.expression, out);
};
for (const name of readdirSync(src).filter(n => /\.tsx?$/.test(n))) {
  const ast = ts.createSourceFile(name, readFileSync(new URL(name, src), 'utf8'), ts.ScriptTarget.Latest, true, name.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const visit = node => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && ['t', 'translate'].includes(node.expression.text)) {
      const arg = node.expression.text === 'translate' ? node.arguments[1] : node.arguments[0];
      if (arg) literals(arg, keys);
    }
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.name.text === 'serverMessages' && node.initializer && ts.isObjectLiteralExpression(node.initializer))
      for (const p of node.initializer.properties) if (ts.isPropertyAssignment(p)) literals(p.initializer, keys);
    ts.forEachChild(node, visit);
  };
  visit(ast);
}
keys.delete('');
const missing = [...keys].filter(k => !(k in own) && !(k in tracevista));
assert.deepEqual(missing, [], 'untranslated interface strings');
const stale = Object.keys(own).filter(k => !keys.has(k));
assert.deepEqual(stale, [], 'translations nothing uses');
console.log(`PASS: ${keys.size} interface strings translated (${Object.keys(own).length} here, the rest TraceVista's)`);
