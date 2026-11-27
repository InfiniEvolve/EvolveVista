import { type ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { LangProvider, LangToggle, useLang } from './i18n';
import { bestCurve, bestNode, firstTask, layoutTree, parseSnapshot, referenceNode, clock, ringParts, taskStatus, ticks, tickText, timeUse, versusParent,
  type CampaignNode, type CampaignSnapshot, type CampaignTask, type Change, type TaskStatus } from './campaign';
import '@tracevista/trajectory.css';
import '@tracevista/interface.css';
import './campaign.css';
import SessionPane, { followed, type PaneState } from './CampaignSession';
import { vistaApi } from './campaign-vista';

const TOKEN_STORE = 'tracevista.campaign.token';
const STEP = 36;   // the curve's distance between two sessions
// the lineage: a circle per session with its words under it, in a column of fixed width where they wrap.
// The circle's edge is its ring of time; around it, the best's gold ring (BEST); RIM
// is how far they reach, where edges end and the words begin.
const COLUMN = 84, R = 17, PAD = 20, EDGE_ROOM = 22, VALUE_LINE = 16, SUB_LINE = 14, SUB_SIZE = 10.5;
const RING = R, BEST = R + 5, RIM = R + 9.5, LABEL_GAP = 2;
const LABEL_WIDTH = COLUMN - 14;
/** The view's palette (campaign.css): a strong colour for strokes and text, a soft one for fills. */
type Tone = 'better' | 'worse' | 'same' | 'first' | 'none' | 'failed';
const ink = (k: Tone) => `var(--cv-${k})`, soft = (k: Tone) => `var(--cv-${k}-soft)`;
const tone: Record<Change, string> = { better: ink('better'), worse: ink('worse'), same: ink('same'), first: ink('first'), none: ink('none') };
type T = ReturnType<typeof useLang>['t'];
// spelled out as t() calls so that the translation check sees every label
const statusLabel = (t: T, k: TaskStatus) => ({ running: t('Running'), paused: t('Paused'), stopped: t('Stopped'), waiting: t('Waiting for sessions') })[k];
/** The shorter words beside a count: "3 waiting". */
const countLabel = (t: T, k: TaskStatus) => ({ running: t('Running'), paused: t('Paused'), stopped: t('Stopped'), waiting: t('Waiting') })[k];
type Pick = (task: string, id: string | null) => void;
/** A press on a circle that may become a drag to the analysis area. */
type Grab = (task: string, id: string, e: React.PointerEvent) => void;

function readToken() {
  const token = new URLSearchParams(location.hash.slice(1)).get('live');
  if (token) {
    try { sessionStorage.setItem(TOKEN_STORE, token); } catch { /* the token lasts for this page */ }
    history.replaceState(history.state, '', location.pathname + location.search);
    return token;
  }
  try { return sessionStorage.getItem(TOKEN_STORE) ?? ''; } catch { return ''; }
}
const minutes = (s: number | null) => s == null ? '-' : `${(s / 60).toFixed(1)} min`;
const plain = (v: number | null) => v == null ? '-' : Math.abs(v) >= 1e7 || (v !== 0 && Math.abs(v) < 0.001) ? v.toExponential(3) : String(Number(v.toPrecision(Math.abs(v) >= 1000 ? 7 : 5)));
const pending = (n: CampaignNode) => n.status === 'running' || n.status === 'prepared';
const toneOf = (t: CampaignTask, n: CampaignNode): Tone => pending(n) ? 'first' : n.excluded ? 'failed' : !n.valid ? 'none' : versusParent(t, n).change;
/** The sessions from the root to the best: the lineage that produced it. */
function pathToBest(t: CampaignTask): Set<string> {
  const byId = new Map(t.nodes.map(n => [n.id, n])), path = new Set<string>();
  for (let n = bestNode(t); n && !path.has(n.id); n = n.parent ? byId.get(n.parent) ?? null : null) path.add(n.id);
  return path;
}

/** The words under a session's value: "★ Best", "Running", "Same work as earlier"... */
function nodeNote(task: CampaignTask, n: CampaignNode, t: T): string {
  return bestNode(task)?.id === n.id ? `★ ${t('Best')}` : n.status === 'running' ? t('Running') : n.status === 'remeasuring' ? t('Re-measuring')
    : !pending(n) && !n.valid ? t('Not valid') : n.excluded ? t('Not ranked') : n.duplicate ? t('Same work as earlier') : '';
}
let measurer: CanvasRenderingContext2D | null | undefined;
/** How wide a text is drawn in the page's font; a rough guess where there is no canvas. */
function textWidth(text: string, size: number, weight: number): number {
  if (measurer === undefined) measurer = typeof document === 'undefined' ? null : document.createElement('canvas').getContext('2d');
  if (!measurer) return text.length * size * 0.6;
  measurer.font = `${weight} ${size}px ${getComputedStyle(document.body).fontFamily || 'sans-serif'}`;
  return measurer.measureText(text).width;
}
/** How many lines a text takes when wrapped at width (words, or characters for text without spaces). */
function lineCount(text: string, size: number, weight: number, width: number): number {
  if (!text) return 0;
  const words = /\s/.test(text) ? text.split(/(?<=\s)/) : [...text];
  let lines = 1, line = 0;
  for (const word of words) {
    const w = textWidth(word, size, weight);
    if (line > 0 && line + w > width) { lines += 1; line = textWidth(word.trimStart(), size, weight); } else line += w;
  }
  return lines;
}
/** The words under a session's circle: its value, its change against its parent, its note. */
function nodeWords(task: CampaignTask, n: CampaignNode, t: T) {
  const v = versusParent(task, n), note = nodeNote(task, n, t);
  return { value: pending(n) ? '…' : n.display, change: v.text, note };
}
/** The value, the change on its own line, then the note wrapped (measured a little narrower: the web font may arrive later). */
const labelHeight = (w: ReturnType<typeof nodeWords>) =>
  VALUE_LINE + (w.change ? SUB_LINE : 0) + lineCount(w.note, SUB_SIZE, 400, LABEL_WIDTH - 4) * SUB_LINE;
/** The lineage's geometry: fixed columns; each generation as tall as the most words any of its sessions has. */
function treeGeometry(task: CampaignTask, t: T) {
  const placed = layoutTree(task.nodes);
  const byId = new Map(task.nodes.map(n => [n.id, n]));
  const words = new Map(placed.map(p => [p.id, nodeWords(task, byId.get(p.id)!, t)]));
  const depths = Math.max(0, ...placed.map(p => p.depth)) + 1;
  const tallest = Array.from({ length: depths }, (_, d) =>
    Math.max(VALUE_LINE, ...placed.filter(p => p.depth === d).map(p => labelHeight(words.get(p.id)!))));
  const top: number[] = [];
  for (let d = 0, y = PAD + RIM; d < depths; d++) { top.push(y); y += RIM + LABEL_GAP + tallest[d] + EDGE_ROOM + RIM; }
  const at = new Map(placed.map(p => [p.id, { x: PAD + COLUMN / 2 + p.row * COLUMN, y: top[p.depth],
    below: top[p.depth] + RIM + LABEL_GAP + labelHeight(words.get(p.id)!) }]));
  const width = PAD * 2 + (Math.max(0, ...placed.map(p => p.row)) + 1) * COLUMN;
  const height = top[depths - 1] + RIM + LABEL_GAP + tallest[depths - 1] + PAD;
  return { placed, words, at, width, height };
}
/** Wide enough for the tree and a readable curve, no wider. */
const panelWidth = (task: CampaignTask, t: T) => Math.max(560, treeGeometry(task, t).width + 40);

/** The time now, in milliseconds, renewed every second while on: what running sessions' rings grow by. */
function useNow(on: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [on]);
  return now;
}
const live = (n: CampaignNode) => n.status === 'running' || n.status === 'remeasuring';
/** An arc of a circle from part a to part b of its way round, clockwise from twelve o'clock. */
function arc(x: number, y: number, r: number, a: number, b: number): string {
  const point = (f: number) => [x + r * Math.sin(f * 2 * Math.PI), y - r * Math.cos(f * 2 * Math.PI)].map(v => v.toFixed(2)).join(',');
  return `M${point(a)}A${r},${r} 0 ${b - a > 0.5 ? 1 : 0} 1 ${point(b)}`;
}
/** A session's time as a ring: dashed for the time limit, solid as it is used, in the colour of each phase,
 * developing then checking. */
function TimeRing({ x, y, r = RING, parts, live: growing }: { x: number; y: number; r?: number; parts: { develop: number; check: number }; live: 'develop' | 'check' | null }) {
  const piece = (a: number, b: number, kind: 'develop' | 'check') => b - a <= 0.002 ? null
    : b - a >= 0.999 ? <circle cx={x} cy={y} r={r} className={`tree-time ${kind}${growing === kind ? ' live' : ''}`} />
    : <path d={arc(x, y, r, a, b)} className={`tree-time ${kind}${growing === kind ? ' live' : ''}`} />;
  return <g className="tree-ring">
    <circle cx={x} cy={y} r={r} className="tree-budget" />
    {piece(0, parts.develop, 'develop')}
    {piece(parts.develop, parts.develop + parts.check, 'check')}
  </g>;
}

/** A session's circle as the lineage draws it, smaller: its number, its colour and its ring of time. */
function SessionMark({ task, node }: { task: CampaignTask; node: CampaignNode }) {
  const use = timeUse(node, useNow(live(node))), k = toneOf(task, node), size = 30, c = size / 2;
  const hollow = pending(node) || node.excluded || !node.valid;
  const number = /^s\d+$/.test(node.id) ? String(Number(node.id.slice(1))) : node.id;
  return <svg className="pane-mark" width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={node.id}>
    <title>{`${node.id}: ${node.display}`}</title>
    <circle cx={c} cy={c} r={c - 2} fill={hollow ? 'var(--surface)' : soft(k)} />
    <TimeRing x={c} y={c} r={c - 2} parts={ringParts(use, use.limit ?? use.develop + use.check)} live={use.live} />
    <text x={c} y={c + 4} textAnchor="middle" className="tree-number" fill={ink(k)}>{number}</text>
  </svg>;
}

/** The lineage, top to bottom: one circle per session with its number, its value and change under it, each
 * session over its first child, the others to its right. Colour compares it with its parent; its edge is its time
 * (development, then validation, against its limit); a gold ring marks the best, a hollow circle one that is
 * still running, not valid or not ranked. */
function Tree({ task, selected, onPick, onGrab }: { task: CampaignTask; selected: string | null; onPick: Pick; onGrab?: Grab }) {
  const { t } = useLang();
  const { placed, words, at, width, height } = useMemo(() => treeGeometry(task, t), [task, t]);
  const now = useNow(task.nodes.some(live));
  if (!placed.length) return <div className="curve-empty">{t('Waiting for the first snapshot.')}</div>;
  const byId = new Map(task.nodes.map(n => [n.id, n]));
  const best = bestNode(task)?.id, path = pathToBest(task);
  const uses = new Map(task.nodes.map(n => [n.id, timeUse(n, now)]));
  // sessions without a time limit are drawn against the longest session of the task
  const longest = Math.max(0, ...[...uses.values()].map(u => u.develop + u.check));
  return <svg className="tree" width={width} height={height} role="img" aria-label={t('Lineage of {task}', { task: task.name })}>
    {task.nodes.filter(n => n.parent && at.has(n.parent)).map(n => {
      // from under the parent's words to the child's rings, turning halfway down the room between generations
      const a = at.get(n.parent!)!, b = at.get(n.id)!, from = a.below + 4, end = b.y - RIM, mid = end - EDGE_ROOM / 2;
      const turn = Math.sign(b.x - a.x) * Math.min(6, Math.abs(b.x - a.x) / 2);
      return <path key={`${n.parent}-${n.id}`} className={`tree-edge${n.status === 'running' ? ' running' : ''}${path.has(n.id) ? ' to-best' : ''}`}
        d={Math.abs(a.x - b.x) < 0.5 ? `M${a.x},${from}V${end}` : `M${a.x},${from}V${mid - 6}Q${a.x},${mid} ${a.x + turn},${mid}H${b.x - turn}Q${b.x},${mid} ${b.x},${mid + 6}V${end}`} />;
    })}
    {placed.map(p => {
      const n = byId.get(p.id)!, { x, y } = at.get(p.id)!, k = toneOf(task, n), c = ink(k), w = words.get(p.id)!;
      const hollow = pending(n) || n.excluded || !n.valid;
      const number = /^s\d+$/.test(n.id) ? String(Number(n.id.slice(1))) : n.id;
      const use = uses.get(n.id)!, parts = ringParts(use, use.limit ?? longest);
      const timing = [`${t('Development')} ${clock(use.develop)}`, `${t('Validation')} ${clock(use.check)}`,
        use.limit ? t('limit {time}', { time: clock(use.limit) }) : ''].filter(Boolean).join(' · ');
      return <g key={n.id} className={`tree-node${pending(n) ? ' pending' : ''}${use.live ? ' ticking' : ''}`} onClick={() => onPick(task.name, n.id)}
        onPointerDown={e => { if (e.button === 0 && onGrab) { e.preventDefault(); onGrab(task.name, n.id, e); } }}>
        <title>{`${n.id}: ${n.display}\n${timing}`}</title>
        {selected === n.id && <rect x={x - COLUMN / 2 + 3} y={y - RIM + 1} width={COLUMN - 6} height={2 * RIM + LABEL_GAP + labelHeight(w) + 2}
          rx={8} className="tree-selected" />}
        {best === n.id && <circle cx={x} cy={y} r={BEST} className="tree-best" />}
        <circle cx={x} cy={y} r={R} fill={hollow ? 'var(--surface)' : soft(k)} className="tree-dot" />
        <TimeRing x={x} y={y} parts={parts} live={use.live} />
        <text x={x} y={y + 4} textAnchor="middle" className="tree-number" fill={c}>{number}</text>
        <foreignObject x={x - LABEL_WIDTH / 2} y={y + RIM + LABEL_GAP} width={LABEL_WIDTH} height={labelHeight(w) + SUB_LINE} className="tree-words">
          <div className="tree-label">
            <b className={n.excluded ? 'struck' : ''}>{w.value}</b>
            {w.change && <em style={{ color: c }}>{w.change}</em>}
            {w.note && <span className={best === n.id ? 'tree-note-best' : ''}>{w.note}</span>}
          </div>
        </foreignObject>
      </g>;
    })}
  </svg>;
}
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(600);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([e]) => setWidth(Math.max(240, e.contentRect.width)));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

function Curve({ task, selected, onSelect }: { task: CampaignTask; selected: string | null; onSelect: (id: string) => void }) {
  const { t } = useLang();
  const [ref, width] = useWidth<HTMLDivElement>();
  const points = bestCurve(task), reference = referenceNode(task);
  const marks = task.metric.marks;
  const values = [...points.flatMap(p => [p.value, p.best]), ...marks.map(m => m.value)].filter((v): v is number => typeof v === 'number' && Number.isFinite(v));
  const H = 150, pad = { l: 52, r: 18, t: 12, b: 26 };
  if (!values.length) return <div ref={ref} className="curve-empty">{t('No measured session yet.')}</div>;
  const usable = task.metric.scale === 'ratio' ? values.filter(v => v > 0) : values;
  let lo = Math.min(...usable), hi = Math.max(...usable);
  const log = task.metric.scale === 'ratio' && lo > 0 && hi / lo > 20;
  if (lo === hi) { lo = log ? lo / 2 : lo - Math.abs(lo || 1) * 0.1; hi = log ? hi * 2 : hi + Math.abs(hi || 1) * 0.1; }
  else if (!log) { const m = (hi - lo) * 0.08; lo -= m; hi += m; }
  const y = (v: number) => {
    const f = log ? (Math.log(v) - Math.log(lo)) / (Math.log(hi) - Math.log(lo)) : (v - lo) / (hi - lo);
    return pad.t + (1 - Math.min(1, Math.max(0, f))) * (H - pad.t - pad.b);
  };
  // one grid cell per session; squeezed only when the sessions would not fit
  const span = Math.max(1, points.length - 1);
  const step = Math.min(STEP, (width - pad.l - pad.r - 90) / span);
  const x = (i: number) => pad.l + i * step;
  const right = x(span) + step / 2;
  const every = Math.ceil(34 / step);
  let path = '', last: number | null = null;
  points.forEach((p, i) => {
    if (p.best == null) return;
    path += last == null ? `M${x(i)},${y(p.best)}` : `L${x(i)},${y(last)}L${x(i)},${y(p.best)}`;
    last = p.best;
  });
  if (last != null) path += `L${x(points.length - 1)},${y(last)}`;
  const byId = new Map(task.nodes.map(n => [n.id, n]));
  return <div ref={ref} className="curve">
    <svg width={Math.min(width, right + 100)} height={H} role="img" aria-label={t('Best value after each session')}>
      {points.map((p, i) => <line key={p.id} x1={x(i)} x2={x(i)} y1={pad.t} y2={H - pad.b} className="curve-grid" />)}
      {ticks(lo, hi, log).map(v => <g key={v}><line x1={pad.l} x2={right} y1={y(v)} y2={y(v)} className="curve-grid" />
        <text x={pad.l - 6} y={y(v) + 3} textAnchor="end" className="curve-tick">{tickText(v)}</text></g>)}
      {reference && reference.value != null && <g><line x1={pad.l} x2={right} y1={y(reference.value)} y2={y(reference.value)} className="curve-reference" />
        <text x={right + 6} y={y(reference.value) + 3} className="curve-tick">{t('reference {id}', { id: reference.id })}</text></g>}
      {marks.map(m => <g key={m.label}><line x1={pad.l} x2={right} y1={y(m.value)} y2={y(m.value)} className="curve-mark" />
        <text x={right + 6} y={y(m.value) + 3} className="curve-mark-label">{m.label}</text></g>)}
      <path d={path} className="curve-best" />
      {points.map((p, i) => {
        const n = byId.get(p.id)!, label = `${p.id}: ${n.display}${p.ranked ? '' : ` (${t('not ranked')})`}`;
        if (p.value == null || (task.metric.scale === 'ratio' && p.value <= 0))
          return <g key={p.id} onClick={() => onSelect(p.id)} className="curve-point"><title>{`${p.id}: ${t('no value')}`}</title>
            <text x={x(i)} y={H - pad.b - 3} textAnchor="middle" className="curve-missing">×</text></g>;
        return <g key={p.id} onClick={() => onSelect(p.id)} className="curve-point"><title>{label}</title>
          <circle cx={x(i)} cy={y(p.value)} r={selected === p.id ? 6 : p.isBest ? 5 : 4}
            className={p.isBest ? 'best' : p.ranked ? 'ranked' : 'unranked'}
            style={p.ranked && !p.isBest ? { fill: soft(toneOf(task, n)), stroke: ink(toneOf(task, n)) } : undefined} /></g>;
      })}
      {points.map((p, i) => i % every === 0 &&
        <text key={p.id} x={x(i)} y={H - 10} textAnchor="middle" className="curve-tick">{p.id}</text>)}
    </svg>
    {log && <div className="curve-legend"><span>{t('Log scale')}</span></div>}
  </div>;
}

/** Where a session's details float once dragged out from under the curve: w and h are the size they had there. */
type Place = { x: number; y: number; w: number; h?: number };
const MIN_WIDTH = 260, MARGIN = 8, SHOWN = 300;   // SHOWN: how much of the details under the curve a pick brings into view
const inView = ({ x, y, w, h }: Place): Place => {
  const width = Math.max(Math.min(MIN_WIDTH, innerWidth - 2 * MARGIN), Math.min(w, innerWidth - 2 * MARGIN));
  return { w: width, h, x: Math.min(Math.max(MARGIN, x), Math.max(MARGIN, innerWidth - width - MARGIN)),
    y: Math.min(Math.max(MARGIN, y), Math.max(MARGIN, innerHeight - 120)) };
};
/** One session's details: under the task's curve at first (place null), floating once dragged by the title bar,
 * put back under the curve with its button; closed with × or Escape. */
/** An agent's report: `field:` lines start sections, `- ` lines are bullets, `code` stays code. */
const inline = (text: string) => text.split(/(`[^`]+`)/).map((part, i) =>
  part.length > 2 && part.startsWith('`') && part.endsWith('`') ? <code key={i}>{part.slice(1, -1)}</code> : part);
const heading = (field: string) => { const words = field.replace(/_/g, ' '); return words[0].toUpperCase() + words.slice(1); };
function Report({ text }: { text: string }) {
  const sections: { title: string | null; lines: string[] }[] = [];
  for (const line of text.split('\n')) {
    const field = /^([A-Za-z][\w ]{0,30}):\s*(.*)$/.exec(line);
    if (field) sections.push({ title: heading(field[1]), lines: field[2] ? [field[2]] : [] });
    else if (sections.length) sections[sections.length - 1].lines.push(line);
    else sections.push({ title: null, lines: [line] });
  }
  return <div className="float-report">{sections.map((section, i) => {
    const blocks: ReactNode[] = [];
    let items: string[] = [];
    const flush = () => { if (items.length) blocks.push(<ul key={blocks.length}>{items.map((item, j) => <li key={j}>{inline(item)}</li>)}</ul>); items = []; };
    for (const line of section.lines) {
      const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
      if (bullet) items.push(bullet[1]);
      else if (line.trim()) { flush(); blocks.push(<p key={blocks.length}>{inline(line.trim())}</p>); }
    }
    flush();
    return <section key={i}>{section.title && <h4>{section.title}</h4>}{blocks}</section>;
  })}</div>;
}

function Details({ task, node, onSelect, onClose, place, onMove, reveal }: { task: CampaignTask; node: CampaignNode; onSelect: (id: string) => void;
  onClose: () => void; place: Place | null; onMove: (p: Place | null) => void; reveal: boolean }) {
  const { t } = useLang();
  const isBest = bestNode(task)?.id === node.id;
  const v = versusParent(task, node);
  const children = task.nodes.filter(n => n.parent === node.id);
  const box = useRef<HTMLElement>(null);
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    addEventListener('keydown', key);
    return () => removeEventListener('keydown', key);
  }, [onClose]);
  // Under the curve, a newly picked session's details are scrolled up until their first part shows, no further,
  // so that the lineage and the curve stay in sight as far as they can; those shown by default are not.
  useEffect(() => {
    const stage = box.current?.closest('.campaign-stage');
    if (!reveal || place || !box.current || !stage) return;
    const over = box.current.getBoundingClientRect().top - (stage.getBoundingClientRect().bottom - SHOWN);
    if (over > 0) stage.scrollBy({ top: over, behavior: 'smooth' });
  }, [node.id, place]);
  // The drag listens on the window, so it carries on when the box leaves its place under the curve to float.
  const down = (e: React.PointerEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('button') || e.button !== 0 || !box.current) return;
    e.preventDefault();
    const r = box.current.getBoundingClientRect();
    const at = place ?? { x: r.left, y: r.top, w: r.width, h: Math.min(r.height, 560, innerHeight - 2 * MARGIN) };
    const from = { x: e.clientX, y: e.clientY };
    let moving = !!place;
    const move = (ev: PointerEvent) => {
      if (!moving && Math.hypot(ev.clientX - from.x, ev.clientY - from.y) < 5) return;   // a click does not lift it out
      moving = true;
      onMove(inView({ x: at.x + ev.clientX - from.x, y: at.y + ev.clientY - from.y, w: at.w, h: at.h }));
    };
    const up = () => { removeEventListener('pointermove', move); removeEventListener('pointerup', up); removeEventListener('pointercancel', up); };
    addEventListener('pointermove', move); addEventListener('pointerup', up); addEventListener('pointercancel', up);
  };
  return <aside ref={box} className={`campaign-float${place ? '' : ' docked'}`} style={place ? { left: place.x, top: place.y, width: place.w, height: place.h } : undefined}
    role="dialog" aria-label={`${t('Session')} ${node.id}`}>
    <div className="campaign-float-bar" onPointerDown={down} title={place ? t('Drag to move') : t('Drag out to float')}>
      <span className="eyebrow">{t('Session')}</span>
      <b>{node.id} · {node.status === 'archived' || node.status === 'remeasuring' ? node.display : t('Running')}</b>
      {place && <button className="campaign-float-dock" onClick={() => onMove(null)} title={t('Put back under the curve')} aria-label={t('Put back under the curve')}>⤓</button>}
      <button className="campaign-float-close" onClick={onClose} aria-label={t('Close')} title={`${t('Close')} (Esc)`}>×</button>
    </div>
    <div className="campaign-float-body">
      <div className="float-hero">
        <div>
          <div className="float-value">{pending(node) ? '…' : node.display}</div>
          <div className="float-metric">{task.metric.name}</div>
        </div>
        {v.text && <div className="float-delta" style={{ color: tone[v.change], background: soft(toneOf(task, node)) }}>
          {v.text}<small>{t('against its parent')}</small></div>}
      </div>
      <div className="float-badges">
        {isBest && <span className="float-badge best">★ {t('Best')}</span>}
        {node.status === 'running' || node.status === 'prepared' ? <span className="float-badge first">{t('Running')}</span>
          : node.status === 'remeasuring' ? <span className="float-badge first">{t('Re-measuring')}</span>
          : <span className={`float-badge ${node.valid ? 'ok' : 'muted'}`}>{node.valid ? t('Valid') : t('Not valid')}</span>}
        {node.flags.map(f => <span key={f.name} className={`float-badge ${f.level === 'soft' ? 'muted' : 'bad'}${node.flagsCleared ? ' cleared' : ''}`} title={f.note}>{f.name} · {f.level}</span>)}
      </div>
      <div className="float-lineage">
        <span>{t('Started from')}</span>
        {node.parent ? <><button className="float-chip" onClick={() => onSelect(node.parent!)}>{node.parent}</button>{node.start && <em>{node.start}</em>}</>
          : <em>{t('The unmodified baseline.')}</em>}
        {children.length > 0 && <><span className="float-gap">{t('Led to')}</span>
          {children.map(c => <button key={c.id} className="float-chip" onClick={() => onSelect(c.id)}>{c.id}</button>)}</>}
      </div>
      <div className="float-tiles">
        <div><span><i className="phase-dot develop" />{t('Development')}</span><b>{minutes(node.agentSeconds)}</b></div>
        <div><span><i className="phase-dot check" />{t('Validation')}</span><b>{minutes(node.checkSeconds ?? null)}</b></div>
        <div><span>{t('Wall time')}</span><b>{minutes(node.wallSeconds)}</b></div>
        <div><span>{t('Cost')}</span><b>{node.cost != null ? `$${node.cost.toFixed(2)}` : '-'}</b></div>
      </div>
      {Object.keys(node.metrics).length > 0 && <><h3>{t('Measurements')}</h3>
        <table className="campaign-metrics"><tbody>
          {[task.metric.name, ...Object.keys(node.metrics).filter(k => k !== task.metric.name).sort()].filter(k => k in node.metrics).map(k =>
            <tr key={k} className={k === task.metric.name ? 'ranking' : ''}><th>{k}{k === task.metric.name && <small> · {t('ranks')}</small>}</th>
              <td>{k === task.metric.name ? <>{node.display}{(node.texts?.[k] ?? plain(node.metrics[k])) !== node.display &&
                <small className="float-first">{t('first measured {value}', { value: node.texts?.[k] ?? plain(node.metrics[k]) })}</small>}</>
                : node.texts?.[k] ?? plain(node.metrics[k])}</td></tr>)}
        </tbody></table></>}
      {node.flags.some(f => f.note) && <><h3>{t('Flags')}{node.flagsCleared ? ` · ${t('cleared')}` : ''}</h3>
        {node.flags.filter(f => f.note).map(f => <p key={f.name} className="float-flag"><b>{f.name}</b> {f.note}</p>)}</>}
      {node.error && <><h3>{t('Note')}</h3><p>{node.error}</p></>}
      {node.report && <><h3>{t("The agent's report")}</h3><Report text={node.report} /></>}
    </div>
  </aside>;
}

function Sparkline({ task }: { task: CampaignTask }) {
  const values = bestCurve(task).map(p => p.best).filter((v): v is number => typeof v === 'number' && (task.metric.scale === 'absolute' || v > 0));
  if (values.length < 2) return <svg className="task-spark" width="64" height="20" aria-hidden="true" />;
  const log = task.metric.scale === 'ratio' && Math.max(...values) / Math.min(...values) > 20;
  const f = (v: number) => log ? Math.log(v) : v;
  const lo = Math.min(...values.map(f)), hi = Math.max(...values.map(f)), span = hi - lo || 1;
  const points = values.map((v, i) => {
    const up = task.metric.direction === 'max' ? (f(v) - lo) / span : (hi - f(v)) / span;   // up is better either way
    return `${(i / (values.length - 1)) * 62 + 1},${18 - up * 16}`;
  }).join(' ');
  return <svg className="task-spark" width="64" height="20" aria-hidden="true"><polyline points={points} /></svg>;
}


/** The tasks as tabs in the header, beside the campaign's name: one is shown at a time. */
function TaskTabs({ tasks, all, current, filter, setFilter, onPick }: { tasks: CampaignTask[]; all: CampaignTask[]; current: string | null;
  filter: string; setFilter: (f: string) => void; onPick: (name: string) => void }) {
  const { t } = useLang();
  return <nav className="campaign-tabs" aria-label={t('Tasks')}>
    {all.length > 6 && <input type="search" value={filter} onChange={e => setFilter(e.target.value)} placeholder={t('Filter tasks')} aria-label={t('Filter tasks')} />}
    <div className="campaign-tabs-row" role="tablist">{tasks.map(x => {
      const best = bestNode(x), status = taskStatus(x);
      return <button key={x.name} role="tab" className={x.name === current ? 'active' : ''} onClick={() => onPick(x.name)} title={x.name} aria-selected={x.name === current}>
        <span className="task-row"><i className={`task-dot ${status}`} title={statusLabel(t, status)} /><span className="task-name">{x.name}</span></span>
        <span className="task-row task-meta"><span>{t('{n} sessions', { n: x.nodes.filter(n => n.parent).length })}{best ? <> · <b>{best.display}</b></> : null}</span><Sparkline task={x} /></span>
      </button>;
    })}</div>
  </nav>;
}

/** "4 tasks · ● 1 running · ● 3 waiting": how many tasks, and how many in each state, with the tabs' dots. */
function TaskCounts({ tasks }: { tasks: CampaignTask[] }) {
  const { t } = useLang();
  const counts = tasks.reduce<Record<TaskStatus, number>>((c, x) => { c[taskStatus(x)] += 1; return c; }, { running: 0, paused: 0, stopped: 0, waiting: 0 });
  return <>
    <span>{tasks.length === 1 ? t('1 task') : t('{n} tasks', { n: tasks.length })}</span>
    {(Object.keys(counts) as TaskStatus[]).filter(k => counts[k]).map(k =>
      <span key={k} className="campaign-count"><i className={`task-dot ${k}`} />{counts[k]} {countLabel(t, k).toLowerCase()}</span>)}
  </>;
}

/** One task: its lineage above, the best value after each session below. */
function TaskPanel({ task, selected, onPick, onGrab }: { task: CampaignTask; selected: string | null; onPick: Pick; onGrab?: Grab }) {
  const { t } = useLang();
  const best = bestNode(task), reference = referenceNode(task), status = taskStatus(task);
  return <section className="task-panel" id={`task-${task.name}`}>
    <header className="task-panel-head">
      <i className={`task-dot ${status}`} title={statusLabel(t, status)} />
      <h2 title={task.name}>{task.name}</h2>
      <span>{statusLabel(t, status)}{task.stopped ? `: ${task.stopped}` : task.paused ? `: ${task.paused}` : ''}</span>
      <span className="task-panel-facts">{task.metric.name ? <>{t('Ranked by')} <b>{task.metric.name}</b> ({task.metric.direction === 'max' ? t('higher is better') : t('lower is better')})</> : null}
        {best && <> · {t('best')} <b>{best.display}</b> {t('at')} {best.id}</>}{reference && best && reference.id !== best.id && <> · {t('reference')} {reference.display}</>}
        {task.metric.marks.map(m => <span key={m.label}> · {m.label} {plain(m.value)}</span>)}</span>
    </header>
    <div className="task-panel-tree"><Tree task={task} selected={selected} onPick={onPick} onGrab={onGrab} /></div>
    <div className="task-panel-curve"><Curve task={task} selected={selected} onSelect={id => onPick(task.name, id)} /></div>
  </section>;
}

function Campaign() {
  const { t } = useLang();
  const [token] = useState(readToken);
  const [data, setData] = useState<CampaignSnapshot | null>(null);
  const [chosen, setChosen] = useState<{ task: string; id: string | null } | null>(null);
  const [hidden, setHidden] = useState(false);   // the details closed; they show again on the next pick
  const [current, setCurrent] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState('');
  const [changed, setChanged] = useState<Date | null>(null);

  // The server pushes a snapshot when the campaign changes (a session submitted, archived, re-measured or
  // flagged), and nothing otherwise. The browser reconnects by itself after a dropped connection.
  useEffect(() => {
    if (paused) return;
    if (!token) { setError(t('Open the address the watch command printed.')); return; }
    const stream = new EventSource(`/api/campaign/events?token=${encodeURIComponent(token)}`);
    stream.addEventListener('snapshot', event => {
      try {
        setData(parseSnapshot(JSON.parse((event as MessageEvent<string>).data)));
        setChanged(new Date()); setError('');
      } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    });
    stream.onopen = () => setError('');
    stream.onerror = () => setError(t('Disconnected; reconnecting. The last view is kept.'));
    return () => stream.close();
  }, [paused, token, t]);

  const tasks = data?.tasks ?? [];
  const shown = tasks.filter(x => x.name.toLowerCase().includes(filter.trim().toLowerCase()));
  const many = tasks.length > 1;
  const shownTask = tasks.find(x => x.name === current) ?? (data ? firstTask(data) : null);
  const chosenTask = tasks.find(x => x.name === chosen?.task) ?? null;
  const chosenNode = chosenTask?.nodes.find(n => n.id === chosen?.id) ?? null;
  const [place, setPlace] = useState<Place | null>(null);
  const dragged = useRef(false);   // a drag ends with a click on the circle; that click is not a pick
  const pick: Pick = (task, id) => {
    if (dragged.current) { dragged.current = false; return; }
    setChosen(id ? { task, id } : null); setCurrent(task); setHidden(false); if (id && !chosen) setPlace(null);
    // the session the pane follows anyway, clicked, keeps the pane following
    const shown = tasks.find(x => x.name === task), target = id && shown && id === followed(shown)?.id ? null : id;
    if (id && (panes[task]?.id ?? null) !== target) setPane(task, { id: target, mode: 'log' });
  };
  // The pane beside the lineage: per task, the session the reader put there (by a click, or a drop that also
  // opens its analysis), or none to follow the one running; and whether it shows the log or the analysis.
  const api = useMemo(() => vistaApi(token), [token]);
  const [panes, setPanes] = useState<Record<string, PaneState>>({});
  const setPane = (task: string, next: Partial<PaneState>) => setPanes(all => ({ ...all, [task]: { ...(all[task] ?? { id: null, mode: 'log' }), ...next } }));
  const [drag, setDrag] = useState<{ task: string; id: string; x: number; y: number; over: boolean } | null>(null);
  const grab: Grab = (task, id, down) => {
    const from = { x: down.clientX, y: down.clientY };
    let moving = false;
    const overArea = (x: number, y: number) => !!document.elementFromPoint(x, y)?.closest('.task-vista');
    const move = (e: PointerEvent) => {
      if (!moving && Math.hypot(e.clientX - from.x, e.clientY - from.y) < 6) return;
      moving = true;
      setDrag({ task, id, x: e.clientX, y: e.clientY, over: overArea(e.clientX, e.clientY) });
    };
    const up = (e: PointerEvent) => {
      removeEventListener('pointermove', move); removeEventListener('pointerup', up); removeEventListener('pointercancel', up);
      if (!moving) return;
      dragged.current = true; setTimeout(() => { dragged.current = false; }, 0);
      if (e.type === 'pointerup' && overArea(e.clientX, e.clientY)) setPane(task, { id, mode: 'analysis' });
      setDrag(null);
    };
    addEventListener('pointermove', move); addEventListener('pointerup', up); addEventListener('pointercancel', up);
  };
  const close = useCallback(() => { setChosen(null); setPlace(null); setHidden(true); }, []);
  const stage = useRef<HTMLDivElement>(null);
  // Until a circle is picked, the details are those of the session the pane beside shows.
  const paneId = shownTask ? panes[shownTask.name]?.id : null;
  const paneNode = shownTask ? (paneId ? shownTask.nodes.find(n => n.id === paneId) : null) ?? followed(shownTask) : null;
  const detailTask = chosenNode ? chosenTask : shownTask, detailNode = chosenNode ?? (hidden ? null : paneNode);
  const details = detailTask && detailNode && <Details task={detailTask} node={detailNode} onSelect={id => pick(detailTask.name, id)}
    onClose={close} place={place} onMove={setPlace} reveal={!!chosenNode} />;
  const switchTo = (name: string) => {
    if (name !== shownTask?.name) { setChosen(null); setPlace(null); stage.current?.scrollTo({ top: 0 }); }
    setCurrent(name);
  };
  // Open on a running task when there is one, and stay on it.
  useEffect(() => { if (current == null && data) setCurrent(firstTask(data)?.name ?? null); }, [current, data]);

  const stopped = tasks.length > 0 && tasks.every(x => x.stopped);
  return <div className="reader campaign-view">
    <header>
      <div className="campaign-title">
        <div className="campaign-name"><a className="brand" href="./">EvolveVista</a><span aria-hidden="true">/</span>
          <h1>{data?.campaign ?? t('InfiniEvolve campaign')}</h1></div>
        <p className="campaign-meta">{data ? <><TaskCounts tasks={tasks} />
          {error ? <span className="campaign-alert bad" title={error}>{t('Disconnected')}</span>
            : paused ? <span className="campaign-alert">{t('View frozen')}</span>
            : changed && <small>{t('updated {time}', { time: changed.toLocaleTimeString() })}</small>}</>
          : t('Waiting for the first snapshot.')}</p>
      </div>
      {many && <TaskTabs tasks={shown} all={tasks} current={shownTask?.name ?? null} filter={filter} setFilter={setFilter} onPick={switchTo} />}
      <div className="header-actions">
        {/* Freezing stops this page's updates, not the campaign; a stopped campaign has none to stop. */}
        {(paused || !stopped) && <button onClick={() => setPaused(p => !p)} title={t('The campaign keeps running; only this page stops updating.')}>
          {paused ? t('Resume updates') : t('Freeze view')}</button>}
        <LangToggle />
      </div>
    </header>
    {error && <div className="campaign-error">{error}</div>}
    <div className="campaign-legend">
      <span><i className="dot" style={{ background: soft('better'), borderColor: ink('better') }} />{t('Better than its parent')}</span>
      <span><i className="dot" style={{ background: soft('worse'), borderColor: ink('worse') }} />{t('Worse than its parent')}</span>
      <span><i className="dot" style={{ background: soft('first'), borderColor: ink('first') }} />{t('First measured / running')}</span>
      <span><i className="hollow" />{t('Not valid')}</span>
      <span><i className="hollow flagged" />{t('Flagged (e.g. hacking), not ranked')}</span>
      <span><i className="ring" />{t('Best')}</span>
      <span title={t('The ring round a session fills, from twelve o’clock, as it uses its time limit; the dashed rest is unused.')}>
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" className="tree-budget" />
          <path d={arc(8, 8, 6, 0, 0.45)} className="tree-time develop" /><path d={arc(8, 8, 6, 0.45, 0.6)} className="tree-time check" /></svg>
        {t('Time used of its limit')}</span>
      <span><i className="phase develop" />{t('Development')}</span>
      <span><i className="phase check" />{t('Validation')}</span>
      <span><i className="line" />{t('Best so far')}</span>
    </div>
    <main>
      <div className="campaign-stage" ref={stage}>
        {shownTask ? <>
          <div className="task-column" style={{ width: `min(100%, ${panelWidth(shownTask, t)}px)` }}>
            <TaskPanel key={shownTask.name} task={shownTask} selected={detailTask?.name === shownTask.name ? detailNode?.id ?? null : null} onPick={pick} onGrab={grab} />
            {!place && details}
          </div>
          <SessionPane task={shownTask} api={api} state={panes[shownTask.name] ?? { id: null, mode: 'log' }}
            onChange={next => setPane(shownTask.name, next)} over={!!drag?.over} dragging={drag?.task === shownTask.name ? drag.id : null}
            mark={n => <SessionMark task={shownTask} node={n} />} />
        </> : <div className="empty-state">{t('Waiting for the first snapshot.')}</div>}
      </div>
      {place && details}
      {drag && <div className={`drag-ghost${drag.over ? ' over' : ''}`} style={{ left: drag.x, top: drag.y }} aria-hidden="true">{drag.id}</div>}
    </main>
  </div>;
}

export default function CampaignLive() {
  return <LangProvider><Campaign /></LangProvider>;
}
