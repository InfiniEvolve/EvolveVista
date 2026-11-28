import { type ReactNode, useEffect, useState } from 'react';
import { clock, timeUse, type CampaignNode, type CampaignTask } from './campaign';
import type { VistaApi } from './campaign-vista';
import ApiCharts from './ApiCharts';
import SessionVista, { LiveLog } from './CampaignVista';
import { useLang } from './i18n';

/** What the pane beside the lineage shows: the session the reader put there, or none to follow the running one;
 * its log, or its TraceVista analysis. */
export interface PaneState { id: string | null; mode: 'log' | 'analysis' }

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

/** A session's log in the pane: its time, how fast its model answered (when the server can tell) and its agent
 * log as it is written, followed while the session runs. */
function SessionLog({ api, task, node }: { api: VistaApi; task: string; node: CampaignNode }) {
  const { t } = useLang();
  return <div className="pane-log">
    <TimeBar node={node} />
    {!node.parent ? <div className="task-vista-empty">{t('The baseline runs no agent, so it has no log.')}</div>
      : <div className="vista-agent">
        <ApiCharts api={api} task={task} session={node.id} />
        <LiveLog api={api} task={task} session={node.id} running={node.status === 'running' || node.status === 'prepared'} />
      </div>}
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
  const unended = node?.status === 'running' || node?.status === 'prepared';
  const phase = node && (node.status === 'running' ? (node.phase === 'check' ? 'check' : 'develop') : node.status === 'remeasuring' ? 'check' : null);
  return <aside className={`task-vista${over ? ' over' : ''}${dragging ? ' armed' : ''}`} aria-label={state.mode === 'log' ? t('Session log') : t('Session analysis')}>
    <header>
      {node && (state.mode === 'log'
        ? <button className="pane-switch vista-go" onClick={() => onChange({ id: node.id, mode: 'analysis' })} disabled={unended}
            title={unended ? t('The analysis can be started once the session ends, when its trace is complete.') : t('Read this session’s log into a TraceVista state map')}>{t('Analyse with TraceVista')}</button>
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
