import { useEffect, useMemo, useRef, useState } from 'react';
import type { CampaignNode, CampaignTask } from './campaign';
import { campaignFacts, generate, LOOSE_AFTER, OUTPUT_TOKENS, SYSTEM, userMessage, type AgentJob, type Analyst, type LogEntry, type Progress, type VistaApi, type VistaStatus, type VistaTimeline } from './campaign-vista';
import { useLang } from './i18n';
import { parseGraph } from '@tracevista/model';
import ProviderPicker from '@tracevista/ProviderPicker';
import { canListModels, changeEndpoint, listModels, presetOf, requestReady, restoreSettings, safeError, selectedPreset, switchPreset, type ChatSettings, type ModelInfo } from '@tracevista/providers';

// The trajectory chat's settings: a key entered there works here, and the other way round.
const SETTINGS_KEY = 'atlas-chat-settings';
function loadSettings(): ChatSettings {
  try { return restoreSettings(JSON.parse(localStorage.getItem(SETTINGS_KEY) ?? '{}')); } catch { return restoreSettings({}); }
}
function saveSettings(s: ChatSettings) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s.remember ? s : { ...s, apiKey: '' })); } catch { /* kept for this page only */ }
}
const estimate = (characters: number) => Math.round(characters / 3.5 / 1000);
// Which writer the reader picked last: an agent's id, or BROWSER for a model asked from this page.
const METHOD_KEY = 'tracevista-campaign-analyst', BROWSER = 'browser';
function loadMethod(): string { try { return localStorage.getItem(METHOD_KEY) ?? ''; } catch { return ''; } }
function saveMethod(m: string) { try { localStorage.setItem(METHOD_KEY, m); } catch { /* kept for this page only */ } }
// The model and effort the reader set for each agent, kept for next time.
const OPTIONS_KEY = 'tracevista-campaign-analyst-options';
type AgentOptions = Record<string, { model: string; effort: string }>;
function loadOptions(): AgentOptions { try { return JSON.parse(localStorage.getItem(OPTIONS_KEY) ?? '{}') ?? {}; } catch { return {}; } }
function saveOptions(o: AgentOptions) { try { localStorage.setItem(OPTIONS_KEY, JSON.stringify(o)); } catch { /* kept for this page only */ } }
const OTHER = '\u0000other';   // the model menu's entry for typing a name
const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

/** Provider, key and model, as in the trajectory chat; requests go from this browser to the provider. */
function ModelSettings({ settings, onChange, disabled }: { settings: ChatSettings; onChange: (s: ChatSettings) => void; disabled: boolean }) {
  const { t } = useLang();
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [note, setNote] = useState<{ text: string; error?: boolean }>({ text: '' });
  const [asked, setAsked] = useState(0);
  const preset = presetOf(settings), info = selectedPreset(settings);
  const current = useRef(settings);
  current.current = settings;
  // As in the trajectory chat: once a key (or a keyless endpoint) is in place, ask the provider for its models.
  useEffect(() => {
    setModels([]);
    if (!canListModels(settings) || preset.startsWith('custom') && asked === 0) {
      setNote({ text: info.manualModels ? t('This provider does not list its models. Type the model ID from its console.') : preset.startsWith('custom')
        ? t('Enter the base URL and key, then click ↻ to fetch models.') : t('Models are fetched automatically after you enter a key.') });
      return;
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => { controller.abort(); setNote({ error: true, text: t('Fetching models timed out after 20 seconds. Retry or enter a model ID manually.') }); }, 20_000);
    setNote({ text: t('Fetching models…') });
    const timer = window.setTimeout(() => {
      listModels(settings, controller.signal).then(found => {
        if (controller.signal.aborted) return;
        setModels(found);
        if (found.length && !current.current.model.trim()) onChange({ ...current.current, model: found[0].id });
        setNote({ text: found.length ? t('Found {n} models', { n: found.length }) : t('This API returned no models.') });
      }).catch(e => {
        if (controller.signal.aborted) return;
        setNote({ error: true, text: t('Could not fetch models: {message}. You can enter a model ID manually.', { message: t(safeError(e, settings)) }) });
      }).finally(() => window.clearTimeout(timeout));
    }, 500);
    return () => { window.clearTimeout(timer); window.clearTimeout(timeout); controller.abort(); };
  }, [settings.provider, settings.baseUrl, settings.apiKey, asked]);   // eslint-disable-line react-hooks/exhaustive-deps
  return <fieldset className="vista-settings" disabled={disabled}>
    <div className="vista-field"><span>{t('Provider')}</span><ProviderPicker value={preset} disabled={disabled} onChange={id => { setModels([]); onChange(switchPreset(settings, id)); }} /></div>
    <label className="vista-field"><span>{t('Base URL')}</span><input value={settings.baseUrl} placeholder="https://…/v1" onChange={e => onChange(changeEndpoint(settings, e.target.value))} /></label>
    <label className="vista-field"><span>{t('Key')}</span><input type="password" autoComplete="off" value={settings.apiKey}
      placeholder={info.noKey ? t('Optional if authentication is disabled') : t('Enter the API key for this provider')} onChange={e => onChange({ ...settings, apiKey: e.target.value })} /></label>
    <label className="vista-field"><span>{t('Model')}</span><span className="vista-model">
      {models.length ? <select value={settings.model} onChange={e => onChange({ ...settings, model: e.target.value })}>
        {!models.some(m => m.id === settings.model) && <option value={settings.model}>{settings.model || '—'}</option>}
        {models.map(m => <option key={m.id} value={m.id}>{m.name && m.name !== m.id ? `${m.name} · ${m.id}` : m.id}</option>)}</select>
        : <input value={settings.model} placeholder={t('Enter the model ID from your provider console')} onChange={e => onChange({ ...settings, model: e.target.value })} />}
      {!info.manualModels && <button type="button" onClick={() => setAsked(n => n + 1)} disabled={!canListModels(settings)} title={t('Fetch models')} aria-label={t('Fetch models')}>↻</button>}</span></label>
    {note.text && <small className={note.error ? 'vista-error' : 'vista-note'} role="status">{note.text}</small>}
    <label className="vista-remember"><input type="checkbox" checked={settings.remember} onChange={e => onChange({ ...settings, remember: e.target.checked })} />{t('Remember the key in this browser')}</label>
  </fieldset>;
}

/** TraceVista's own trajectory viewer, handed the compiled graph by its local-file handoff. */
function VistaFrame({ api, task, session }: { api: VistaApi; task: string; session: string }) {
  const { t } = useLang();
  const frame = useRef<HTMLIFrameElement>(null);
  const [error, setError] = useState('');
  const handoff = useMemo(() => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join(''), [task, session]);
  useEffect(() => {
    let graph: Promise<string> | null = null;
    const receive = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.origin !== location.origin) return;
      const data = event.data as { type?: string; token?: string } | null;
      if (data?.token !== handoff) return;
      if (data.type === 'tracevista:ready') {
        // Check here first, so a refusal says what is wrong instead of the handoff's general message.
        graph ??= api.graph(task, session).then(json => { parseGraph(JSON.parse(json)); return json; });
        graph.then(json => frame.current?.contentWindow?.postMessage({ type: 'tracevista:import', token: handoff, json }, location.origin))
          .catch(e => setError(t('The saved analysis does not open in TraceVista: {reason} Analyse again to replace it.', { reason: e instanceof Error ? e.message : String(e) })));
      }
    };
    addEventListener('message', receive);
    return () => removeEventListener('message', receive);
  }, [api, task, session, handoff]);
  const src = `trajectory.html#${new URLSearchParams({ 'tracevista-import': handoff, source: location.origin })}`;
  // A refused graph would leave the viewer waiting for its handoff; show only the reason.
  if (error) return <div className="vista-panel"><div className="vista-error">{error}</div></div>;
  return <>
    <iframe ref={frame} className="vista-frame" src={src} title={`TraceVista · ${session}`} /></>;
}

type Stage = { kind: 'idle' } | { kind: 'loading' } | { kind: 'nolog' } | { kind: 'ready'; loose?: boolean }
  | { kind: 'setup'; timeline: VistaTimeline; job?: AgentJob | null } | { kind: 'running'; timeline: VistaTimeline; progress: Progress }
  | { kind: 'agent'; job: AgentJob } | { kind: 'error'; message: string; timeline?: VistaTimeline; job?: AgentJob | null };

/** What the agent did lately, newest last; it follows the end unless the reader scrolled up. */
function AgentLog({ lines }: { lines: string[] }) {
  const { t } = useLang();
  const log = useRef<HTMLUListElement>(null);
  const following = useRef(true);
  useEffect(() => { const el = log.current; if (el && following.current) el.scrollTop = el.scrollHeight; }, [lines]);
  if (!lines.length) return <div className="vista-log vista-log-empty">{t('Waiting for the agent’s first step…')}</div>;
  return <ul ref={log} className="vista-log" onScroll={e => {
    const el = e.currentTarget; following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24; }}>
    {lines.map((line, i) => <li key={i}>{line}</li>)}</ul>;
}

const MAX_ENTRIES = 2000;   // a two-hour session writes thousands of steps; the page keeps the latest

/** A session's agent log as it is written: what the agent says, the tools it calls and what they return,
 * newest last. It follows the end unless the reader scrolled up. */
export function LiveLog({ api, task, session, running }: { api: VistaApi; task: string; session: string; running: boolean }) {
  const { t } = useLang();
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [dropped, setDropped] = useState(0);
  const [state, setState] = useState<'connecting' | 'live' | 'lost' | 'refused'>('connecting');
  const [following, setFollowing] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setEntries([]); setDropped(0); setState('connecting'); setFollowing(true);
    const source = api.agentLog(task, session);
    source.addEventListener('entries', event => {
      setState('live');
      const batch = (JSON.parse((event as MessageEvent).data).entries ?? []) as LogEntry[];
      if (!batch.length) return;
      setEntries(old => {
        const all = old.concat(batch);
        if (all.length <= MAX_ENTRIES) return all;
        setDropped(d => d + all.length - MAX_ENTRIES);
        return all.slice(-MAX_ENTRIES);
      });
    });
    // EventSource reconnects by itself, from the last offset it was sent; a refusal closes it for good.
    source.onerror = () => setState(source.readyState === EventSource.CLOSED ? 'refused' : 'lost');
    return () => source.close();
  }, [api, task, session]);
  useEffect(() => { const el = box.current; if (el && following) el.scrollTop = el.scrollHeight; }, [entries, following]);
  return <div className="live-log-wrap">
    <div ref={box} className="live-log" onScroll={e => {
      const el = e.currentTarget; setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight < 24); }}>
      {dropped > 0 && <div className="live-system">{t('{n} earlier steps are no longer kept on this page', { n: dropped.toLocaleString() })}</div>}
      {!entries.length && <div className="live-empty">{state === 'refused' ? t('The server would not send this session’s log.')
        : state === 'connecting' ? t('Reading the session…') : t('Waiting for the agent’s first step…')}</div>}
      {entries.map((e, i) => <div key={i} className={`live-entry live-${e.kind}${e.sub ? ' live-sub' : ''}`}>
        {e.kind === 'tool' ? <><span className="live-tool">{e.tool}</span><span className="live-input">{e.text}</span></>
          : e.kind === 'result' || e.kind === 'error' ? <pre>{e.text}</pre> : e.text}</div>)}
    </div>
    <div className="live-foot">
      <span className={`live-dot live-dot-${state}`} />
      <span>{state === 'live' ? (running ? t('Live: new steps appear as the agent writes them') : t('The session has ended; this is the end of its log')) : state === 'lost' ? t('Connection lost; reconnecting…')
        : state === 'refused' ? t('Not connected') : t('Connecting…')}</span>
      {!following && entries.length > 0 && <button type="button" onClick={() => setFollowing(true)}>{t('Jump to the latest')}</button>}
    </div>
  </div>;
}

/** One session's TraceVista analysis, in the pane beside the lineage: the analysis when there is one, else
 * the choice of who writes it, then their progress. It waits for the session to end: the trace is not complete
 * before then. */
export default function SessionVista({ task, node, api }: { task: CampaignTask; node: CampaignNode; api: VistaApi }) {
  const { t } = useLang();
  const [stage, setStage] = useState<Stage>({ kind: 'idle' });
  const [settings, setSettings] = useState<ChatSettings>(loadSettings);
  const [again, setAgain] = useState(0);
  const running = useRef<AbortController | null>(null);
  const sessionLive = node.status === 'running' || node.status === 'prepared';
  useEffect(() => saveSettings(settings), [settings]);
  // The agents the serving machine offers; the reader picks one, or a model asked from this browser.
  const [analysts, setAnalysts] = useState<Analyst[] | null>(null);
  const [method, setMethod] = useState(loadMethod);
  useEffect(() => { api.analysts().then(setAnalysts).catch(() => setAnalysts([])); }, [api]);
  const chosen = analysts?.find(a => a.id === method) ?? (method === BROWSER ? null : analysts?.[0] ?? null);
  const pick = (m: string) => { setMethod(m); saveMethod(m); setTyping(false); };
  const [options, setOptions] = useState<AgentOptions>(loadOptions);
  const chosenOptions = chosen ? options[chosen.id] ?? { model: '', effort: '' } : { model: '', effort: '' };
  // A model typed by hand: its name is not among those the agent offers.
  const [typing, setTyping] = useState(false);
  const offered = chosen?.models ?? [];
  const pickedModel = offered.find(m => m.id === chosenOptions.model);
  const typed = typing || (!!chosenOptions.model && !pickedModel);
  const efforts = pickedModel?.efforts?.length ? pickedModel.efforts : chosen?.efforts ?? [];
  const setOption = (key: 'model' | 'effort', value: string) => {
    if (!chosen) return;
    const next = { ...options, [chosen.id]: { ...chosenOptions, [key]: value } };
    setOptions(next); saveOptions(next);
  };

  useEffect(() => {
    running.current?.abort(); running.current = null;
    if (sessionLive) { setStage({ kind: 'idle' }); return; }
    let live = true;
    setStage({ kind: 'loading' });
    api.status(task.name, node.id).then(async (s: VistaStatus) => {
      if (!live) return;
      if (!s.log) { setStage({ kind: 'nolog' }); return; }
      if (s.job?.state === 'running') { setStage({ kind: 'agent', job: s.job }); return; }
      if (s.ready && again === 0) { setStage({ kind: 'ready', loose: s.job?.state === 'done' && s.job.loose }); return; }
      const timeline = await api.timeline(task.name, node.id);
      if (live) setStage({ kind: 'setup', timeline, job: again === 0 ? s.job : null });
    }).catch(e => live && setStage({ kind: 'error', message: t(e instanceof Error ? e.message : String(e)) }));
    return () => { live = false; };
  }, [api, task.name, node.id, again, sessionLive]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => running.current?.abort(), []);
  // An agent runs on the server; ask how it is doing every two seconds, also after a reload.
  const agentRunning = stage.kind === 'agent' && stage.job.state === 'running';
  useEffect(() => {
    if (!agentRunning) return;
    const timer = window.setInterval(() => {
      api.status(task.name, node.id).then(s => {
        const job = s.job;
        if (!job) return;
        if (job.state === 'running') setStage(st => st.kind === 'agent' ? { kind: 'agent', job } : st);
        else if (job.state === 'done') { setAgain(0); setStage({ kind: 'ready', loose: job.loose }); }
        else api.timeline(task.name, node.id).then(timeline => setStage({ kind: 'setup', timeline, job }));
      }).catch(() => { /* the next tick tries again */ });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [agentRunning, api, task.name, node.id]);   // eslint-disable-line react-hooks/exhaustive-deps

  const startAgent = (timeline: VistaTimeline, analyst: Analyst) => {
    api.startAgent(task.name, node.id, analyst.id, campaignFacts(task, node), chosenOptions.model.trim(), chosenOptions.effort)
      .then(job => setStage({ kind: 'agent', job }))
      .catch(e => setStage({ kind: 'error', timeline, message: t(e instanceof Error ? e.message : String(e)) }));
  };
  const stopAgent = () => {
    api.stopAgent(task.name, node.id).then(job => api.timeline(task.name, node.id).then(timeline => setStage({ kind: 'setup', timeline, job })))
      .catch(e => setStage({ kind: 'error', message: t(e instanceof Error ? e.message : String(e)) }));
  };

  const start = (timeline: VistaTimeline) => {
    const controller = new AbortController();
    running.current = controller;
    setStage({ kind: 'running', timeline, progress: { round: 1, phase: 'waiting', characters: 0, sent: 0, received: 0 } });
    // The last objection stays in view while the model answers it.
    generate(settings, api, task, node, timeline, progress => setStage(s => ({ kind: 'running', timeline,
      progress: { ...progress, note: progress.note ?? (s.kind === 'running' && progress.round > 1 ? s.progress.note : undefined) } })), controller.signal)
      .then(({ loose }) => { if (!controller.signal.aborted) { setAgain(0); setStage({ kind: 'ready', loose }); } })
      .catch(e => { if (!controller.signal.aborted) setStage({ kind: 'error', timeline, message: t(safeError(e, settings)) }); });
  };
  const stop = () => { running.current?.abort(); running.current = null; setStage(s => s.kind === 'running' ? { kind: 'setup', timeline: s.timeline } : s); };

  const inputTokens = (timeline: VistaTimeline) => estimate(SYSTEM.length + userMessage(task, node, timeline).length);
  const phase = (p: Progress) => p.phase === 'compiling' ? t('Compiling the analysis…') : p.phase === 'fixing' ? t('The compiler objected; asking the model to fix it…')
    : p.phase === 'answering' ? t('The model is writing the analysis…') : p.phase === 'thinking' ? t('The model is thinking…') : t('Waiting for the model…');

  return <div className="vista-body">
    {sessionLive ? <div className="task-vista-empty">{t('{session} is still running. The analysis can be started once the session ends, when its trace is complete.', { session: node.id })}</div>
      : stage.kind === 'loading' ? <div className="task-vista-empty">{t('Reading the session…')}</div>
      : stage.kind === 'nolog' ? <div className="task-vista-empty">{t('This session has no agent log, so there is no trace to analyse. The baseline runs no agent.')}</div>
      : stage.kind === 'ready' ? <>
        <div className="vista-toolbar">{stage.loose ? <span className="vista-warning">{t('The analysis compiled, but the model left reading gaps that the compiler reports. Analyse again for a fuller reading.')}</span> : <span />}
          <button className="vista-again" onClick={() => setAgain(n => n + 1)}>{t('Analyse again')}</button></div>
        <VistaFrame api={api} task={task.name} session={node.id} /></>
      : stage.kind === 'agent' ? <div className="vista-agent">
        <div className="vista-agent-head">
          <div>
            <p className="vista-progress" role="status" aria-live="polite">{t('{agent} is analysing the session…', { agent: stage.job.label })}
              <span className="vista-clock">{clock(stage.job.elapsed)}</span></p>
            <p className="vista-meta">{t('It follows the TraceVista skill and runs the page’s checks until they pass; a long session can take tens of minutes. You can close this page and come back.')}</p>
          </div>
          <button className="vista-stop" onClick={stopAgent}>{t('Stop')}</button>
        </div>
        <AgentLog lines={stage.job.activity} /></div>
      : stage.kind === 'running' ? <div className="vista-panel">
        <p className="vista-progress" role="status" aria-live="polite">{phase(stage.progress)}</p>
        <p className="vista-meta">{t('Answer {round}', { round: stage.progress.round })}{(stage.progress.part ?? 1) > 1 && <> · {t('continued after the output limit, part {part}', { part: stage.progress.part! })}</>} · {t('{n} characters written', { n: stage.progress.characters.toLocaleString() })}</p>
        <p className="vista-meta">{t('So far about {sent}k tokens sent and {received}k received.', { sent: estimate(stage.progress.sent), received: estimate(stage.progress.received) })}</p>
        {stage.progress.note && <pre className="vista-objection">{stage.progress.note}</pre>}
        <button onClick={stop}>{t('Stop')}</button></div>
      : <div className="vista-panel">
        {stage.kind === 'error' && <div className="vista-error">{stage.message}</div>}
        {'job' in stage && stage.job && stage.job.state !== 'running' && stage.job.state !== 'done' && <div className="vista-error">
          {stage.job.state === 'stopped' ? t('{agent} was stopped.', { agent: stage.job.label }) : t('{agent} did not finish the analysis: {reason}', { agent: stage.job.label, reason: t(stage.job.error ?? stage.job.note ?? '') })}
          {stage.job.activity.length > 0 && <ol className="vista-activity">{stage.job.activity.map((line, i) => <li key={i}>{line}</li>)}</ol>}</div>}
        {'timeline' in stage && stage.timeline && <>
          <label className="vista-field vista-method"><span>{t('Written by')}</span>
            <select value={chosen?.id ?? BROWSER} onChange={e => pick(e.target.value)}>
              {(analysts ?? []).map(a => <option key={a.id} value={a.id}>{t('{agent} on this machine', { agent: a.label })}{a.untested ? ` · ${t('untested')}` : ''}</option>)}
              <option value={BROWSER}>{t('A model, asked from this browser with an API key')}</option>
            </select></label>
          {analysts !== null && analysts.length === 0 && <p className="vista-meta">{t('This server offers no agents. Install Claude Code or Codex, or start the server with --analyst <name>=<command> for another agent.')}</p>}
          {chosen ? <>
            <p className="vista-meta">{t('{agent} runs on the machine that serves this page, in a working directory of its own, with its own login and quota. It follows the TraceVista skill: it reads the {events} events, writes the analysis and runs the page’s checks until they pass. A long session can take tens of minutes.', { agent: chosen.label, events: stage.timeline.events })}</p>
            {(chosen.takesModel || (chosen.efforts ?? []).length > 0) && <div className="vista-settings">
              {chosen.takesModel && <label className="vista-field"><span>{t('Model')}</span>
                {offered.length > 0 && !typed ? <select value={chosenOptions.model} onChange={e => {
                    if (e.target.value === OTHER) { setTyping(true); setOption('model', ''); return; }
                    const model = offered.find(m => m.id === e.target.value);
                    // an effort the new model lacks would be refused
                    if (model?.efforts?.length && chosenOptions.effort && !model.efforts.includes(chosenOptions.effort)) setOption('effort', '');
                    setOption('model', e.target.value);
                  }}>
                    <option value="">{chosen.model ? t('{model} (as the server is set)', { model: chosen.model }) : t('The agent’s own default')}</option>
                    {offered.map(m => <option key={m.id} value={m.id}>{m.name !== m.id ? `${m.name} · ${m.id}` : m.id}</option>)}
                    <option value={OTHER}>{t('Another model…')}</option>
                  </select>
                  : <span className="vista-model"><input value={chosenOptions.model} autoFocus={typing} onChange={e => setOption('model', e.target.value)}
                      placeholder={chosen.model || t('Type the model name')} />
                    {offered.length > 0 && <button type="button" onClick={() => { setTyping(false); setOption('model', ''); }}>{t('List')}</button>}</span>}
                </label>}
              {efforts.length > 0 && <label className="vista-field"><span>{t('Effort')}</span>
                <select value={chosenOptions.effort} onChange={e => setOption('effort', e.target.value)}>
                  <option value="">{pickedModel?.effort ? t('{effort} (the model’s default)', { effort: pickedModel.effort })
                    : chosen.effort ? t('{effort} (as the server is set)', { effort: chosen.effort }) : t('The agent’s own default')}</option>
                  {efforts.map(level => <option key={level} value={level}>{level}</option>)}
                </select></label>}
            </div>}
            <p className="vista-meta vista-command"><code>{chosen.detail}</code></p>
            {chosen.untested && <p className="vista-meta">{t('How this agent runs unattended follows its documentation and has not been tried here. If it stalls or does nothing, stop it and pick another, or start the server with --analyst <name>=<command>.')}</p>}
            <button className="vista-go" onClick={() => startAgent(stage.timeline!, chosen)}>{t('Start the agent')}</button>
          </> : <>
          <p className="vista-meta">{t('{events} events · about {tokens}k input tokens per request, up to {output}k output tokens. While the compiler objects, the model is asked to fix the analysis, until it compiles or you stop it; from answer {loose} on, an analysis that compiles with reading gaps is accepted.',
            { events: stage.timeline.events, tokens: inputTokens(stage.timeline), output: Math.round(OUTPUT_TOKENS / 1000), loose: LOOSE_AFTER })}</p>
          <ModelSettings settings={settings} onChange={setSettings} disabled={false} />
          <p className="vista-meta">{t('The session’s redacted log and the TraceVista skill’s instructions are sent from this browser to the provider you choose. The key stays in this browser.')}</p>
          <button className="vista-go" disabled={!requestReady(settings)} onClick={() => start(stage.timeline!)}>{t('Generate the analysis')}</button>
          </>}
        </>}
      </div>}
  </div>;
}
