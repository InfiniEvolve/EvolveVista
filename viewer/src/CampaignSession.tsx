import { type ReactNode, useEffect, useRef, useState } from 'react';
import { clock, timeUse, type CampaignNode, type CampaignTask } from './campaign';
import type { LogEntry, VistaApi } from './campaign-vista';
import SessionVista from './CampaignVista';
import { useLang } from './i18n';

/** What the pane beside the lineage shows: the session the reader put there, or none to follow the running one;
 * its log, or its TraceVista analysis. */
export interface PaneState { id: string | null; mode: 'log' | 'analysis' }

const KEEP = 3000;   // entries kept in view; older ones fall off the top
const live = (n: CampaignNode) => n.status === 'running' || n.status === 'remeasuring';

/** The session to follow when the reader has put none here: the newest one whose agent is at work, else the
 * newest running or re-measured, else the newest session. */
export function followed(task: CampaignTask): CampaignNode | null {
  const byIndex = [...task.nodes].sort((a, b) => b.index - a.index || b.id.localeCompare(a.id));
  return byIndex.find(n => n.status === 'running' && n.phase !== 'check') ?? byIndex.find(live) ?? byIndex.find(n => n.parent) ?? byIndex[0] ?? null;
}

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

/** The session's time as a bar: development, then validation, against its limit (dashed where unused). */
function TimeBar({ node }: { node: CampaignNode }) {
  const { t } = useLang();
  const use = timeUse(node, useNow(live(node)));
  const scale = Math.max(use.limit ?? 0, use.develop + use.check) || 1;
  const width = (s: number) => `${Math.min(100, (s / scale) * 100)}%`;
  return <div className="pane-time">
    <div className="pane-time-bar" aria-hidden="true">
      <i className={`develop${use.live === 'develop' ? ' live' : ''}`} style={{ width: width(use.develop) }} />
      <i className={`check${use.live === 'check' ? ' live' : ''}`} style={{ width: width(use.check) }} />
    </div>
    <div className="pane-time-words">
      <span><i className="phase-dot develop" />{t('Development')} <b>{clock(use.develop)}</b></span>
      <span><i className="phase-dot check" />{t('Validation')} <b>{clock(use.check)}</b></span>
      {use.limit != null && <span className="pane-time-limit">{t('limit {time}', { time: clock(use.limit) })}</span>}
    </div>
  </div>;
}

/** One entry of the log: a call as its tool and argument, its result folded to a few lines until clicked. */
function Entry({ entry }: { entry: LogEntry }) {
  const [open, setOpen] = useState(false);
  if (entry.k === 'tool') {
    const at = entry.t.indexOf(': ');
    return <li className="log-tool"><span className="log-tag">{at > 0 ? entry.t.slice(0, at) : entry.t}</span>
      {at > 0 && <code>{entry.t.slice(at + 2)}</code>}</li>;
  }
  if (entry.k === 'out' || entry.k === 'think') {
    const long = entry.t.split('\n').length > 4 || entry.t.length > 360;
    return <li className={`log-${entry.k}${entry.err ? ' err' : ''}${long && !open ? ' folded' : ''}`}
      onClick={long ? () => setOpen(o => !o) : undefined}><pre>{entry.t}</pre></li>;
  }
  return <li className={`log-${entry.k}`}>{entry.t}</li>;
}

/** A session's agent log, as it is written: read from its end, then followed every two seconds while the session
 * runs; it keeps to the bottom unless the reader scrolled up. */
function SessionLog({ api, task, node }: { api: VistaApi; task: string; node: CampaignNode }) {
  const { t } = useLang();
  const [log, setLog] = useState<{ loaded: boolean; exists: boolean; cut: boolean; entries: LogEntry[]; error?: string }>({ loaded: false, exists: false, cut: false, entries: [] });
  const running = live(node);
  const runningRef = useRef(running);
  runningRef.current = running;
  const look = useRef<() => void>(() => {});
  useEffect(() => {
    let alive = true, offset = -1, timer = 0, busy = false;
    setLog({ loaded: false, exists: false, cut: false, entries: [] });
    const read = async () => {
      if (busy) return;
      busy = true; window.clearTimeout(timer);
      let more = false;
      try {
        const r = await api.log(task, node.id, offset);
        if (!alive) return;
        offset = r.offset; more = r.more;
        setLog(l => ({ loaded: true, exists: r.exists, cut: l.loaded ? l.cut : r.cut, entries: [...l.entries, ...r.entries].slice(-KEEP) }));
      } catch (e) {
        if (alive) setLog(l => ({ ...l, loaded: true, error: t(e instanceof Error ? e.message : String(e)) }));
      } finally { busy = false; }
      if (alive && (more || runningRef.current)) timer = window.setTimeout(read, more ? 0 : 2000);
    };
    look.current = read;
    read();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [api, task, node.id]);   // eslint-disable-line react-hooks/exhaustive-deps
  // When the session ends, read what it wrote last.
  useEffect(() => { if (!running) look.current(); }, [running]);

  const list = useRef<HTMLOListElement>(null);
  const following = useRef(true);
  useEffect(() => { const el = list.current; if (el && following.current) el.scrollTop = el.scrollHeight; }, [log.entries]);

  const empty = !log.loaded ? t('Reading the log…')
    : log.error ? log.error
    : !log.exists ? (!node.parent ? t('The baseline runs no agent, so it has no log.')
      : running || node.status === 'prepared' ? t('The agent has not written its log yet. Some adapters deliver it only when the session ends.')
      : t('This session has no agent log.'))
    : !log.entries.length ? t('Waiting for the agent’s first step…') : '';
  return <div className="pane-log">
    <TimeBar node={node} />
    {empty ? <div className={`task-vista-empty${log.error ? ' vista-error' : ''}`}>{empty}</div>
      : <ol ref={list} className="session-log" onScroll={e => {
        const el = e.currentTarget; following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32; }}>
        {log.cut && <li className="log-meta">{t('The log is long; it is shown from near its end.')}</li>}
        {log.entries.map((entry, i) => <Entry key={i} entry={entry} />)}
      </ol>}
    {node.status === 'running' && node.phase === 'check' && <div className="pane-phase check">{t('The agent has ended; its work is being validated.')}</div>}
    {node.status === 'remeasuring' && <div className="pane-phase check">{t('The session is being measured again.')}</div>}
  </div>;
}

/** The pane beside the lineage: the running session's live log by default, any session's when its circle is
 * clicked, and its TraceVista analysis when asked for (or when a circle is dropped here). */
export default function SessionPane({ task, api, state, onChange, over, dragging, mark }: { task: CampaignTask; api: VistaApi; state: PaneState;
  onChange: (s: Partial<PaneState>) => void; over: boolean; dragging: string | null; mark: (n: CampaignNode) => ReactNode }) {
  const { t } = useLang();
  const picked = state.id ? task.nodes.find(n => n.id === state.id) ?? null : null;
  const node = picked ?? followed(task);
  const phase = node && (node.status === 'running' ? (node.phase === 'check' ? 'check' : 'develop') : node.status === 'remeasuring' ? 'check' : null);
  return <aside className={`task-vista${over ? ' over' : ''}${dragging ? ' armed' : ''}`} aria-label={state.mode === 'log' ? t('Session log') : t('Session analysis')}>
    <header>
      {node && (state.mode === 'log'
        ? <button className="pane-switch vista-go" onClick={() => onChange({ id: node.id, mode: 'analysis' })}
            title={t('Read this session’s log into a TraceVista state map')}>{t('Analyse with TraceVista')}</button>
        : <button className="pane-switch" onClick={() => onChange({ mode: 'log' })}>{t('Back to the log')}</button>)}
      {node && mark(node)}
      {phase && <span className={`pane-badge ${phase}`}>{phase === 'develop' ? t('Developing') : t('Validating')}</span>}
    </header>
    {dragging ? <div className="vista-drop">{t('Drop {session} here to analyse its trace', { session: dragging })}</div>
      : !node ? <div className="task-vista-empty">{t('Waiting for the first snapshot.')}</div>
      : state.mode === 'analysis' ? <SessionVista key={node.id} task={task} node={node} api={api} />
      : <SessionLog api={api} task={task.name} node={node} />}
  </aside>;
}
