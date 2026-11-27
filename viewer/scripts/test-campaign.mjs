import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';

const code = stripTypeScriptTypes(readFileSync(new URL('../src/campaign.ts', import.meta.url), 'utf8'));
const c = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));

const node = (id, parent, value, more = {}) => ({ id, index: Number(id.slice(1)), parent, start: parent ? 'best' : null, status: 'archived',
  valid: true, value, display: String(value), metrics: {}, flags: [], flagsCleared: false, void: false, excluded: false, duplicate: false,
  agentSeconds: null, wallSeconds: null, archivedAt: null, error: null, report: '', cost: null, ...more });
const task = (nodes, metric = {}) => ({ name: 't', nodes, stopped: null, paused: null,
  metric: { name: 'score', direction: 'max', scale: 'ratio', format: '{:.3f}', anchor: 'baseline', ...metric } });

// A branching lineage: s002 and s003 both start from s001; s004 continues s002.
const t = task([node('s000', null, 1), node('s001', 's000', 2), node('s002', 's001', 3), node('s003', 's001', 1.5),
  node('s004', 's002', 3, { duplicate: true }), node('s005', 's001', 9, { excluded: true, flags: [{ name: 'cheated', level: 'hard', note: '' }] }),
  node('s006', 's004', null, { status: 'running', valid: false })]);
const placed = Object.fromEntries(c.layoutTree(t.nodes).map(p => [p.id, p]));
assert.deepEqual([placed.s000.depth, placed.s001.depth, placed.s002.depth, placed.s004.depth, placed.s006.depth], [0, 1, 2, 3, 4]);
assert.equal(placed.s001.row, placed.s002.row, 'a parent sits over its first child, the others to its right');
assert.equal(placed.s000.row, placed.s001.row, 'an only child sits straight under its parent');
assert.equal(placed.s002.row, placed.s006.row, 'a straight line of descent stays straight');
assert.ok(placed.s003.row > placed.s002.row && placed.s005.row > placed.s003.row, 'later branches go to the right, in archive order');
assert.equal(new Set(c.layoutTree(t.nodes).map(p => `${p.depth}:${p.row}`)).size, t.nodes.length, 'no two cards overlap');

// Against the parent, and what ranks.
assert.deepEqual(c.versusParent(t, t.nodes[2]), { change: 'better', text: '+50.0%' });
assert.deepEqual(c.versusParent(t, t.nodes[3]), { change: 'worse', text: '-25.0%' });
const far = task([node('s000', null, 0.006), node('s001', 's000', 2.153), node('s002', 's001', 0.5)]);
assert.deepEqual(c.versusParent(far, far.nodes[1]), { change: 'better', text: '×359' }, 'large changes read as factors');
assert.deepEqual(c.versusParent(far, far.nodes[2]), { change: 'worse', text: '×0.23' });
assert.deepEqual(c.versusParent(t, t.nodes[4]), { change: 'same', text: '±0' });
assert.equal(c.versusParent(t, t.nodes[0]).change, 'first');
assert.equal(c.versusParent(t, t.nodes[5]).change, 'none', 'a node kept out of ranking is not compared');
assert.equal(c.bestNode(t).id, 's002', 'the excluded 9 is not the best; the earlier of two equal values is');
assert.equal(c.referenceNode(t).id, 's000');

// The best-so-far curve: running sessions are not on it, an excluded one is drawn but does not move the best.
const curve = c.bestCurve(t);
assert.deepEqual(curve.map(p => p.id), ['s000', 's001', 's002', 's003', 's004', 's005']);
assert.deepEqual(curve.map(p => p.best), [1, 2, 3, 3, 3, 3]);
assert.deepEqual(curve.map(p => p.ranked), [true, true, true, true, true, false]);
assert.deepEqual(curve.filter(p => p.isBest).map(p => p.id), ['s002']);

// Lower is better, on an absolute scale; a stub baseline that is not valid.
const lower = task([node('s000', null, null, { valid: false }), node('s001', 's000', 466), node('s002', 's001', 286), node('s003', 's002', 300)],
  { direction: 'min', scale: 'absolute' });
assert.equal(c.referenceNode(lower).id, 's001', 'without a valid baseline the first valid session is the reference');
assert.deepEqual(c.versusParent(lower, lower.nodes[2]), { change: 'better', text: '-180' });
assert.deepEqual(c.versusParent(lower, lower.nodes[3]), { change: 'worse', text: '+14' });
assert.deepEqual(c.bestCurve(lower).map(p => p.best), [null, 466, 286, 286]);

// Broken data never hangs the layout, and malformed snapshots are refused.
const loop = [node('s001', 's002', 1), node('s002', 's001', 2)];
assert.equal(c.layoutTree(loop).length, 2);
assert.throws(() => c.parseSnapshot({ format: 'other' }));
{
  const marked = c.parseSnapshot({ format: 'infinievolve-campaign', version: 1, tasks: [{ name: 't', nodes: [],
    metric: { scale: 'ratio', marks: [{ label: 'oracle', value: 1 }, { label: 'zero', value: 0 }, { label: 'text', value: 'x' }] } }] });
  assert.deepEqual(marked.tasks[0].metric.marks, [{ label: 'oracle', value: 1 }], 'marks that a ratio cannot hold are dropped');
  const old = c.parseSnapshot({ format: 'infinievolve-campaign', version: 1, tasks: [{ name: 't', nodes: [], metric: {} }] });
  assert.deepEqual(old.tasks[0].metric.marks, [], 'a snapshot without marks has none');
}
assert.throws(() => c.parseSnapshot({ format: 'infinievolve-campaign', version: 1, tasks: [{ name: 't', metric: {}, nodes: [node('s0', null, 1), node('s0', null, 2)] }] }));

// Axis ticks.
assert.deepEqual(c.ticks(0.006, 2.2, true), [0.01, 0.1, 1]);
assert.deepEqual(c.ticks(150, 480, false), [200, 300, 400]);
assert.equal(c.tickText(0.001), '1e-3');
// Task status: running while any session is out or re-measured; the first running task opens first.
const running = task([node('s000', null, 1), node('s001', 's000', null, { status: 'running', valid: false })]);
assert.equal(c.taskStatus(running), 'running');
assert.equal(c.taskStatus({ ...t, nodes: t.nodes.slice(0, 6), stopped: 'done' }), 'stopped');
assert.equal(c.taskStatus({ ...lower, paused: 'job failed' }), 'paused');
assert.equal(c.firstTask({ tasks: [{ ...lower, name: 'a', stopped: 'done' }, { ...running, name: 'b' }] }).name, 'b');
// A session's time: developing, then checking, against its limit; running ones grow with the clock.
{
  const at = Date.parse('2026-11-21T10:00:00Z');
  const done = node('s001', 's000', 1, { agentSeconds: 300, checkSeconds: 60, timeLimitSeconds: 600 });
  assert.deepEqual(c.timeUse(done, at), { develop: 300, check: 60, limit: 600, live: null });
  assert.deepEqual(c.ringParts(c.timeUse(done, at), 600), { develop: 0.5, check: 0.1 });
  const developing = node('s002', 's001', null, { status: 'running', submittedAt: '2026-11-21T09:58:00Z', phase: 'develop', timeLimitSeconds: 600 });
  assert.deepEqual(c.timeUse(developing, at), { develop: 120, check: 0, limit: 600, live: 'develop' });
  const checking = { ...developing, phase: 'check', developedAt: '2026-11-21T09:59:30Z' };
  assert.deepEqual(c.timeUse(checking, at), { develop: 90, check: 30, limit: 600, live: 'check' });
  const over = node('s003', 's001', 1, { agentSeconds: 700, checkSeconds: 50, timeLimitSeconds: 600 });
  assert.deepEqual(c.ringParts(c.timeUse(over, at), 600), { develop: 700 / 750, check: 50 / 750 }, 'a session past its limit fills the ring, each phase in proportion');
  assert.deepEqual(c.timeUse(node('s004', null, 1), at), { develop: 0, check: 0, limit: null, live: null }, 'a bare snapshot has no times');
  assert.equal(c.clock(3725), '1:02:05');
}
console.log('PASS: campaign lineage layout, comparisons, best-so-far curve and axis ticks');

// The cost a session's details show: OpenRouter's sum when any call is priced, else Claude Code's own.
const costly = node('s001', 's000', 2, { cost: 1.234 });
assert.deepEqual(c.sessionCost(costly, { state: 'ok', calls: 10, waiting: 0, cost: 0.4567, priced: 10 }), { text: '$0.457', source: 'openrouter' });
assert.deepEqual(c.sessionCost(costly, { state: 'ok', calls: 10, waiting: 3, cost: 0.2, priced: 7 }), { text: '≥ $0.200', source: 'openrouter' },
  'at least that much while some calls are not priced yet');
assert.deepEqual(c.sessionCost(costly, { state: 'no-key', calls: 10, waiting: 0, cost: 0, priced: 0 }), { text: '$1.23', source: 'agent' },
  'nothing priced: the cost Claude Code reported');
assert.deepEqual(c.sessionCost(node('s002', 's000', 2), null), { text: '-', source: null });
assert.equal(c.usd(12.345), '$12.35');
console.log('PASS: a session’s cost, OpenRouter’s first');
