/** An InfiniEvolve campaign, as integrations/infinievolve/campaign_snapshot.py writes it, and what the campaign
 * view derives from it: the lineage laid out as a tree, each node against its parent, and the best-so-far curve. */

export interface CampaignFlag { name: string; level: 'soft' | 'hard' | 'void'; note: string }
export interface CampaignNode {
  id: string; index: number; parent: string | null; start: string | null;
  status: 'archived' | 'remeasuring' | 'running' | 'prepared';
  valid: boolean; value: number | null; display: string; metrics: Record<string, number | null>;
  /** each metric as its declaration formats it (absent in older snapshots) */
  texts?: Record<string, string>;
  flags: CampaignFlag[]; flagsCleared: boolean; void: boolean; excluded: boolean; duplicate: boolean;
  agentSeconds: number | null; wallSeconds: number | null; archivedAt: string | null; error: string | null;
  report: string; cost: number | null; submittedAt?: string | null;
  /** added by the server from the session's own files (sessions.py); absent from a bare snapshot */
  timeLimitSeconds?: number | null; checkSeconds?: number | null;
  /** while running or re-measured: developing (the agent works) or checking (its work is evaluated) */
  phase?: 'develop' | 'check' | null; developedAt?: string | null;
}
export interface CampaignMetric { name: string; direction: 'max' | 'min'; scale: 'ratio' | 'absolute'; format: string; anchor: unknown;
  /** values worth a line on the curve, such as a known best solution's; they never rank */
  marks: { label: string; value: number }[] }
export interface CampaignTask { name: string; metric: CampaignMetric; nodes: CampaignNode[]; stopped: string | null; paused: string | null }
export interface CampaignSnapshot { format: 'infinievolve-campaign'; version: 1; campaign: string; stamp: string; tasks: CampaignTask[] }

export function parseSnapshot(data: unknown): CampaignSnapshot {
  const s = data as CampaignSnapshot;
  if (!s || s.format !== 'infinievolve-campaign' || s.version !== 1 || !Array.isArray(s.tasks)) throw new Error('Not an InfiniEvolve campaign snapshot.');
  for (const t of s.tasks) {
    if (!t || typeof t.name !== 'string' || !t.metric || !Array.isArray(t.nodes)) throw new Error('A task in the snapshot is malformed.');
    const marks: unknown = t.metric.marks;
    t.metric.marks = (Array.isArray(marks) ? marks : []).filter((m): m is { label: string; value: number } =>
      !!m && typeof m.label === 'string' && typeof m.value === 'number' && Number.isFinite(m.value) && (t.metric.scale !== 'ratio' || m.value > 0));
    const ids = new Set<string>();
    for (const n of t.nodes) {
      if (!n || typeof n.id !== 'string' || ids.has(n.id)) throw new Error(`Task ${t.name} has a malformed or repeated node.`);
      ids.add(n.id);
    }
  }
  return s;
}

/** Ranked: counts toward the best and the curve. */
export const ranks = (n: CampaignNode, m: CampaignMetric) =>
  n.status !== 'running' && n.status !== 'prepared' && n.valid && !n.excluded && !n.void &&
  typeof n.value === 'number' && Number.isFinite(n.value) && (m.scale === 'absolute' || n.value > 0);
export const better = (a: number, b: number, m: CampaignMetric) => m.direction === 'max' ? a > b : a < b;

export function bestNode(t: CampaignTask): CampaignNode | null {
  let best: CampaignNode | null = null;
  for (const n of t.nodes) if (ranks(n, t.metric) && (!best || better(n.value!, best.value!, t.metric))) best = n;
  return best;
}
/** The baseline when it ranks, else the first session that does. */
export function referenceNode(t: CampaignTask): CampaignNode | null {
  return t.nodes.find(n => ranks(n, t.metric)) ?? null;
}

export type Change = 'better' | 'worse' | 'same' | 'first' | 'none';
/** A node against its parent: the change, and its size as text (percent on a ratio scale, an amount otherwise). */
export function versusParent(t: CampaignTask, n: CampaignNode): { change: Change; text: string } {
  if (!ranks(n, t.metric)) return { change: 'none', text: '' };
  const parent = t.nodes.find(p => p.id === n.parent);
  if (!parent || !ranks(parent, t.metric)) return { change: 'first', text: '' };
  const a = n.value!, b = parent.value!;
  if (a === b) return { change: 'same', text: '±0' };
  const change: Change = better(a, b, t.metric) ? 'better' : 'worse';
  if (t.metric.scale === 'ratio') {
    const r = a / b;
    // A change of more than double reads better as a factor than as thousands of percent.
    if (r >= 2 || r <= 0.5) return { change, text: `×${Number(r.toPrecision(r >= 100 || r < 0.01 ? 3 : 2))}` };
    const pct = (r - 1) * 100;
    return { change, text: `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%` };
  }
  const d = a - b;
  return { change, text: `${d > 0 ? '+' : ''}${Math.abs(d) >= 100 ? d.toFixed(0) : Number(d.toPrecision(3))}` };
}

export interface CurvePoint { id: string; x: number; value: number | null; ranked: boolean; best: number | null; isBest: boolean }
/** One point per session in archive order (the baseline is x = 0), with the best ranked value up to it. */
export function bestCurve(t: CampaignTask): CurvePoint[] {
  let best: number | null = null, bestId: string | null = null;
  const points: CurvePoint[] = [];
  t.nodes.filter(n => n.status !== 'running' && n.status !== 'prepared' && !n.void).forEach((n, x) => {
    const ok = ranks(n, t.metric);
    if (ok && (best === null || better(n.value!, best, t.metric))) { best = n.value; bestId = n.id; }
    points.push({ id: n.id, x, value: typeof n.value === 'number' ? n.value : null, ranked: ok, best, isBest: false });
  });
  for (const p of points) p.isBest = p.id === bestId;
  return points;
}

export type TaskStatus = 'running' | 'paused' | 'stopped' | 'waiting';
/** Running while any session is out or being scored again; then paused, stopped, or waiting for its next session. */
export function taskStatus(t: CampaignTask): TaskStatus {
  if (t.nodes.some(n => n.status === 'running' || n.status === 'remeasuring')) return 'running';
  return t.paused ? 'paused' : t.stopped ? 'stopped' : 'waiting';
}
/** The task to open first: one that is running, else the first. */
export function firstTask(s: CampaignSnapshot): CampaignTask | null {
  return s.tasks.find(t => taskStatus(t) === 'running') ?? s.tasks[0] ?? null;
}

/** depth: the generation (rows, top to bottom); row: the column, fractional for a parent centred over its children. */
export interface Placed { id: string; depth: number; row: number }
/** A tree laid out top to bottom: depth is the generation; leaves take one column each in a depth-first walk in
 * archive order, and a parent sits centred over its children, so a straight line of descent stays straight.
 * Nodes whose parent is unknown start their own tree. */
export function layoutTree(nodes: CampaignNode[]): Placed[] {
  const ids = new Set(nodes.map(n => n.id));
  const children = new Map<string, CampaignNode[]>();
  const roots: CampaignNode[] = [];
  for (const n of [...nodes].sort((a, b) => a.index - b.index || a.id.localeCompare(b.id))) {
    if (n.parent && ids.has(n.parent) && n.parent !== n.id) {
      if (!children.has(n.parent)) children.set(n.parent, []);
      children.get(n.parent)!.push(n);
    } else roots.push(n);
  }
  const placed: Placed[] = [], seen = new Set<string>();
  let next = 0;
  const walk = (n: CampaignNode, depth: number): number => {
    if (seen.has(n.id)) return next;   // a cycle in broken data must not hang the page
    seen.add(n.id);
    const rows = (children.get(n.id) ?? []).map(k => walk(k, depth + 1));
    const row = rows.length ? rows[0] : next++;   // over its first child; the others to its right
    placed.push({ id: n.id, depth, row });
    return row;
  };
  for (const r of roots) walk(r, 0);
  for (const n of nodes) if (!seen.has(n.id)) placed.push({ id: n.id, depth: 0, row: next++ });   // unreachable through a cycle
  return placed;
}

/** A session's time in seconds: developing (its agent at work), then checking (its work evaluated, and any
 * re-measurement), against its time limit; live while it still grows. now is in milliseconds. */
export interface TimeUse { develop: number; check: number; limit: number | null; live: 'develop' | 'check' | null }
export function timeUse(n: CampaignNode, now: number): TimeUse {
  const limit = typeof n.timeLimitSeconds === 'number' && n.timeLimitSeconds > 0 ? n.timeLimitSeconds : null;
  const at = (iso: string | null | undefined) => { const t = iso ? Date.parse(iso) : NaN; return Number.isFinite(t) ? t : null; };
  if (n.status === 'running') {
    const began = at(n.submittedAt), ended = at(n.developedAt);
    if (began == null) return { develop: 0, check: 0, limit, live: n.phase === 'check' ? 'check' : 'develop' };
    if (n.phase === 'check' && ended != null)
      return { develop: Math.max(0, (ended - began) / 1000), check: Math.max(0, (now - ended) / 1000), limit, live: 'check' };
    return { develop: Math.max(0, (now - began) / 1000), check: 0, limit, live: 'develop' };
  }
  if (n.status === 'prepared') return { develop: 0, check: 0, limit, live: null };
  return { develop: n.agentSeconds ?? 0, check: n.checkSeconds ?? 0, limit, live: n.status === 'remeasuring' ? 'check' : null };
}
/** The parts of a ring of time the two phases fill, in order. scale is the limit, or for a session without one
 * the longest session of its task; a session that took longer (validation comes after the agent's limit) fills
 * the whole ring, each phase in proportion. */
export function ringParts(use: TimeUse, scale: number): { develop: number; check: number } {
  const whole = Math.max(scale, use.develop + use.check);
  if (!(whole > 0)) return { develop: 0, check: 0 };
  return { develop: use.develop / whole, check: use.check / whole };
}

/** 4:05, or 1:02:03 past an hour */
export const clock = (seconds: number) => {
  const s = Math.max(0, Math.floor(seconds)), h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
};

/** Tick values for a curve axis: about five round numbers across [lo, hi], or powers of ten on a log axis. */
export function ticks(lo: number, hi: number, log: boolean): number[] {
  if (!(hi > lo)) return [lo];
  if (log) {
    const out: number[] = [];
    for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
      const v = 10 ** e;
      if (v >= lo * 0.999 && v <= hi * 1.001) out.push(v);
    }
    return out.length >= 2 ? out : [lo, hi];
  }
  const raw = (hi - lo) / 4, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(f => f * mag).find(s => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}
export function tickText(v: number): string {
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1000 || a < 0.01) return v.toExponential(0).replace('e+', 'e');
  return String(Number(v.toPrecision(3)));
}
