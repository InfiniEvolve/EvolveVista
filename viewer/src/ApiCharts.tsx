import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ApiPoint, ApiStats, VistaApi } from './campaign-vista';
import { useLang } from './i18n';

const MAX_POINTS = 5000;   // two hours at a call every second or two
const WINDOW = 15;         // calls in the rolling line
const PAD = { left: 44, right: 12, top: 8, bottom: 20 };

// 'absent': the stream itself was refused, by a server older than this page
type Shown = (Omit<ApiStats, 'points' | 'state'> & { state: ApiStats['state'] | 'absent' }) | null;

/** One measure of the agent's model calls over time: a dot per call (bigger for more tokens) and a rolling line. */
interface Measure { key: 'ttft' | 'tps'; title: string; value: (p: ApiPoint) => number | null; format: (v: number) => string;
  /** the rolling line's value over a window of calls, and how the header says its latest value */
  roll: (window: ApiPoint[]) => number | null; rollNote: (value: string, n: number) => string }

const median = (xs: number[]) => { if (!xs.length) return null; const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const seconds = (ms: number) => ms >= 10_000 ? `${(ms / 1000).toFixed(0)} s` : `${(ms / 1000).toFixed(1)} s`;
const timeOf = (p: ApiPoint, i: number) => { const t = p.time ? Date.parse(p.time) : NaN; return Number.isFinite(t) ? t : i; };
const clock = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

/** A rounded scale top and its ticks, so the axis reads 0, 50, 100 rather than 0, 47.3, 94.6. */
function ticks(top: number): number[] {
  if (!(top > 0)) return [0, 1];
  const step0 = top / 3, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0)!;
  return Array.from({ length: Math.ceil(top / step) + 1 }, (_, i) => +(i * step).toPrecision(6));
}

function useSize() {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setSize({ w: entry.contentRect.width, h: entry.contentRect.height }));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, size] as const;
}

function MetricChart({ points, measure }: { points: ApiPoint[]; measure: Measure }) {
  const { t } = useLang();
  const [box, { w, h }] = useSize();
  const [hover, setHover] = useState<number | null>(null);
  const shown = useMemo(() => points.map((p, i) => ({ p, x: timeOf(p, i), v: measure.value(p) })).filter(d => d.v !== null) as { p: ApiPoint; x: number; v: number }[], [points, measure]);
  const rolling = useMemo(() => shown.map((d, i) => ({ x: d.x, v: measure.roll(shown.slice(Math.max(0, i - WINDOW + 1), i + 1).map(s => s.p)) })), [shown, measure]);
  const latest = rolling.length ? rolling[rolling.length - 1].v : null;
  const header = <div className="api-chart-head"><b>{measure.title}</b>
    {latest !== null && <span>{measure.rollNote(measure.format(latest), WINDOW)}</span>}</div>;
  if (!shown.length) return <div className="api-chart">{header}<div ref={box} className="api-plot api-plot-empty">{t('No calls measured yet')}</div></div>;

  // the top of the scale leaves out the rare outlier, which is drawn at the edge instead of flattening the rest
  const sorted = shown.map(d => d.v).sort((a, b) => a - b);
  const ys = ticks(Math.max(sorted[Math.floor((sorted.length - 1) * 0.98)], ...rolling.map(r => r.v ?? 0)) * 1.05);
  const top = ys[ys.length - 1];
  const x0 = shown[0].x, x1 = Math.max(shown[shown.length - 1].x, x0 + 1);
  const plotW = Math.max(w - PAD.left - PAD.right, 10), plotH = Math.max(h - PAD.top - PAD.bottom, 10);
  const X = (x: number) => PAD.left + (x - x0) / (x1 - x0) * plotW;
  const Y = (v: number) => PAD.top + plotH - Math.min(v, top) / top * plotH;
  const maxTokens = Math.max(1, ...shown.map(d => d.p.tokens ?? 0));
  const r = (p: ApiPoint) => 2.5 + 4.5 * Math.sqrt((p.tokens ?? 0) / maxTokens);
  const line = rolling.filter(d => d.v !== null).map((d, i) => `${i ? 'L' : 'M'}${X(d.x).toFixed(1)},${Y(d.v!).toFixed(1)}`).join('');
  const near = (clientX: number, el: SVGSVGElement) => {
    const at = clientX - el.getBoundingClientRect().left;
    let best = 0;
    shown.forEach((d, i) => { if (Math.abs(X(d.x) - at) < Math.abs(X(shown[best].x) - at)) best = i; });
    return best;
  };
  const held = hover !== null ? shown[hover] : null;
  return <div className="api-chart">{header}
    <div ref={box} className="api-plot">
      {w > 0 && <svg width={w} height={h} role="img" aria-label={measure.title}
        onPointerMove={e => setHover(near(e.clientX, e.currentTarget))} onPointerLeave={() => setHover(null)}>
        {ys.map(v => <g key={v}><line className="api-grid" x1={PAD.left} x2={PAD.left + plotW} y1={Y(v)} y2={Y(v)} />
          <text className="api-tick" x={PAD.left - 6} y={Y(v) + 3} textAnchor="end">{measure.format(v)}</text></g>)}
        {typeof shown[0].p.time === 'string' && <>
          <text className="api-tick" x={PAD.left} y={h - 5}>{clock(x0)}</text>
          <text className="api-tick" x={PAD.left + plotW} y={h - 5} textAnchor="end">{clock(x1)}</text></>}
        {held && <line className="api-cross" x1={X(held.x)} x2={X(held.x)} y1={PAD.top} y2={PAD.top + plotH} />}
        {shown.map((d, i) => <circle key={d.p.id} className={`api-dot${d.v > top ? ' api-clipped' : ''}${i === hover ? ' on' : ''}`}
          cx={X(d.x)} cy={Y(d.v)} r={r(d.p)} />)}
        <path className="api-roll" d={line} />
      </svg>}
      {held && <div className="api-tip" style={{ left: Math.min(X(held.x) + 10, Math.max(w - 190, 0)), top: 4 }}>
        <div>{held.p.time ? new Date(timeOf(held.p, 0)).toLocaleTimeString() : held.p.id}{held.p.sub ? ` · ${t('subagent')}` : ''}</div>
        <div>{t('TTFT')} <b>{held.p.ttft !== null ? seconds(held.p.ttft) : '—'}</b> · {t('TPS')} <b>{held.p.tps ?? '—'}</b></div>
        <div>{t('{n} tokens in {time}', { n: (held.p.tokens ?? 0).toLocaleString(), time: held.p.gen !== null ? seconds(held.p.gen) : '—' })}</div>
        <div className="api-tip-meta">{[held.p.provider, held.p.finish].filter(Boolean).join(' · ')}</div>
      </div>}
    </div></div>;
}

/** How fast the session's agent got answers from its model, call by call, while the session runs and after. */
export default function ApiCharts({ api, task, session }: { api: VistaApi; task: string; session: string }) {
  const { t } = useLang();
  const [points, setPoints] = useState<ApiPoint[]>([]);
  const [info, setInfo] = useState<Shown>(null);
  useEffect(() => {
    setPoints([]); setInfo(null);
    const source = api.apiStats(task, session);
    source.addEventListener('stats', event => {
      const { points: fresh, ...rest } = JSON.parse((event as MessageEvent).data) as ApiStats;
      setInfo(rest);
      if (fresh.length) setPoints(old => {
        const known = new Set(old.map(p => p.id));
        // lookups finish in any order; the charts read in call order
        const all = old.concat(fresh.filter(p => !known.has(p.id)));
        all.sort((a, b) => (a.time ?? '').localeCompare(b.time ?? ''));
        return all.slice(-MAX_POINTS);
      });
    });
    source.onerror = () => { if (source.readyState === EventSource.CLOSED) setInfo(i => i ?? { state: 'absent', calls: 0, waiting: 0, missing: 0 }); };
    return () => source.close();
  }, [api, task, session]);
  const measures = useMemo<Measure[]>(() => [
    { key: 'ttft', title: t('Time to first token'), value: p => p.ttft, format: seconds, rollNote: (value, n) => t('median {value} over the last {n} calls', { value, n }),
      roll: w => median(w.map(p => p.ttft).filter((v): v is number => v !== null)) },
    { key: 'tps', title: t('Tokens per second'), value: p => p.tps, format: v => `${Math.round(v)}`, rollNote: (value, n) => t('{value} tok/s over the last {n} calls, weighted by tokens', { value, n }),
      roll: w => { const ok = w.filter(p => p.tokens && p.gen); const ms = ok.reduce((s, p) => s + p.gen!, 0);
        return ms ? ok.reduce((s, p) => s + p.tokens!, 0) / (ms / 1000) : null; } },
  ], [t]);
  if (info === null) return <p className="api-note">{t('Looking up the model’s answer times…')}</p>;
  if (info.state !== 'ok') return <p className="api-note">{
    info.state === 'no-key' ? t('No API stats for this session: the server has no OPENROUTER_API_KEY.')
    : info.state === 'not-openrouter' ? t('No API stats for this session: its agent does not reach its model through OpenRouter.')
    : info.state === 'refused' ? t('No API stats for this session: OpenRouter refused the server’s key.')
    : t('This server does not look up API stats. Restart it with the current version to see them.')}</p>;
  return <section className="api-charts" aria-label={t('Model answer times')}>
    {measures.map(m => <MetricChart key={m.key} points={points} measure={m} />)}
    <p className="api-foot">{t('{calls} model calls; each dot is one, larger for more tokens. Measured by OpenRouter.', { calls: info.calls.toLocaleString() })}
      {info.waiting > 0 && <> {t('{n} waiting for their numbers.', { n: info.waiting })}</>}</p>
  </section>;
}
