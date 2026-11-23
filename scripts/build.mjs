// Build the campaign page into evolve_vista/static, with TraceVista's trajectory viewer beside it, and copy the
// TraceVista skill into evolve_vista/tracevista_skill, so that an installed package has everything it serves.
// Both TraceVista parts come from the submodule in third_party/TraceVista at its checked-out commit; nothing in
// this repository is a copy of them (the outputs are not committed).
//
//     git submodule update --init      (once; `--remote` to follow TraceVista's main)
//     npm install && npm run build
import { execFileSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const tracevista = join(root, 'third_party', 'TraceVista');
const bin = name => join(root, 'node_modules', '.bin', name);
const run = (cmd, args, cwd = root) => execFileSync(cmd, args, { cwd, stdio: 'inherit' });
if (!existsSync(join(tracevista, 'package.json'))) throw new Error('third_party/TraceVista is empty: run git submodule update --init');

// 1. TraceVista's own build, with its own configuration; its packages resolve to this repository's node_modules.
run(bin('vite'), ['build', '--config', 'viewer/vite.config.ts', '--logLevel', 'warn'], tracevista);
// 2. The campaign page.
run(bin('tsc'), ['-p', 'viewer/tsconfig.json']);
run(bin('vite'), ['build', '--config', 'viewer/vite.config.ts', '--logLevel', 'warn']);

// 3. trajectory.html and every file it loads, found by following the references from the page.
const dist = join(tracevista, 'dist'), out = join(root, 'evolve_vista', 'static');
const REFERENCE = /(?:src|href)="\.\/([^"]+)"|from\s*"\.\/([^"]+)"|import\(\s*"\.\/([^"]+)"|url\(\.?\/?([^)#'"]+\.(?:woff2?|png|svg))\)/g;
const wanted = new Set(), queue = ['trajectory.html'];
while (queue.length) {
  const name = queue.pop();
  if (wanted.has(name)) continue;
  wanted.add(name);
  if (!/\.(html|js|css)$/.test(name)) continue;
  for (const m of readFileSync(join(dist, name), 'utf8').matchAll(REFERENCE)) {
    const found = m.slice(1).find(Boolean);
    queue.push(found.startsWith('assets/') ? found : join(dirname(name), found));
  }
}
for (const name of wanted) { mkdirSync(dirname(join(out, name)), { recursive: true }); cpSync(join(dist, name), join(out, name)); }

// 4. The skill an analysing agent follows and whose scripts extract, compile and validate.
const skill = join(tracevista, 'skills', 'tracevista-skill'), copy = join(root, 'evolve_vista', 'tracevista_skill');
rmSync(copy, { recursive: true, force: true });
const walk = dir => readdirSync(dir).flatMap(n => statSync(join(dir, n)).isDirectory() ? walk(join(dir, n)) : [join(dir, n)]);
for (const file of walk(skill)) {
  const rel = relative(skill, file);
  if (!(rel === 'SKILL.md' || /^(references\/.*\.md|scripts\/.*\.py)$/.test(rel))) continue;
  mkdirSync(dirname(join(copy, rel)), { recursive: true }); cpSync(file, join(copy, rel));
}

const git = (...args) => execFileSync('git', ['-C', tracevista, ...args]).toString().trim();
const source = { tracevista: { commit: git('rev-parse', 'HEAD'), dirty: !!git('status', '--porcelain', '--untracked-files=no'), license: 'MIT' },
  trajectoryFiles: [...wanted].sort() };
writeFileSync(join(out, 'SOURCE.json'), JSON.stringify(source, null, 1) + '\n');
cpSync(join(tracevista, 'LICENSE'), join(out, 'TRACEVISTA-LICENSE'));
cpSync(join(tracevista, 'LICENSE'), join(copy, 'LICENSE'));
console.log(`built evolve_vista/static (TraceVista ${source.tracevista.commit.slice(0, 7)}${source.tracevista.dirty ? ', modified' : ''}) and evolve_vista/tracevista_skill`);
