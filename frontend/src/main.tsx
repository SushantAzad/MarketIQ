import { StrictMode, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { apiBase, request, sourceUrl } from './api';
import type { Company, Fact, Filing, Freshness, Job, Source } from './api';
import './styles.css';

type Page = 'overview' | 'research' | 'compare' | 'filings' | 'risk';
type IconName = Page | 'search' | 'arrow' | 'external' | 'close' | 'refresh' | 'settings' | 'sun' | 'moon' | 'check';
const paths: Record<IconName, string> = {
  overview: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  research: 'M4 4h16v12H9l-5 4z M8 8h8 M8 12h5',
  compare: 'M5 4v16 M12 9v11 M19 2v18', filings: 'M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6',
  risk: 'M12 2l8 4v6c0 5-8 10-8 10S4 17 4 12V6z M12 8v5 M12 16h.01',
  search: 'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6',
  arrow: 'M5 12h14 M13 6l6 6-6 6', external: 'M14 3h7v7 M21 3l-11 11 M10 3H3v18h18v-7',
  close: 'M6 6l12 12 M6 18L18 6', refresh: 'M20 7v5h-5 M4 17v-5h5 M5 7a8 8 0 0 1 14-1l1 6 M4 12l1 6a8 8 0 0 0 14-1',
  settings: 'M4 7h16 M4 17h16 M8 4v6 M16 14v6', sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 1v3 M12 20v3 M1 12h3 M20 12h3 M4 4l2 2 M18 18l2 2 M4 20l2-2 M18 6l2-2',
  moon: 'M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11', check: 'M5 12l4 4L19 6',
};
const nav: { id: Page; label: string }[] = [
  { id: 'overview', label: 'Overview' }, { id: 'research', label: 'Research' },
  { id: 'compare', label: 'Compare' }, { id: 'filings', label: 'SEC filings' }, { id: 'risk', label: 'Risk analysis' },
];
const metrics = [{ id: 'revenue', label: 'Revenue', basis: 'annual' }, { id: 'net_income', label: 'Net income', basis: 'annual' },
  { id: 'operating_cash_flow', label: 'Operating cash flow', basis: 'annual' }, { id: 'assets', label: 'Total assets', basis: 'instant' }];
function Icon({ name, size = 19 }: { name: IconName; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}
function date(value?: string | null) { return value ? new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(new Date(value)) : 'Not available'; }
function label(value: string) { return value.replaceAll('_', ' ').replace(/^./, s => s.toUpperCase()); }
function number(value?: string | null, unit = 'USD', compact = true) {
  if (value == null || !Number.isFinite(Number(value))) return 'â€”';
  const amount = Number(value);
  if (unit === 'USD') return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', notation: compact ? 'compact' : 'standard', maximumFractionDigits: compact ? 2 : 0 }).format(amount);
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(amount)}${unit === '%' || unit === 'percent' ? '%' : ` ${unit}`}`;
}
function Badge({ status }: { status: string }) {
  return <span className={`badge ${['RECENT', 'COMPLETED', 'AVAILABLE'].includes(status) ? 'good' : 'quiet'}`}><span />{label(status.toLowerCase())}</span>;
}
function Empty({ icon = 'research', title, children }: { icon?: IconName; title: string; children: ReactNode }) {
  return <div className="empty"><div className="empty-icon"><Icon name={icon} size={25} /></div><h3>{title}</h3><p>{children}</p></div>;
}
function ErrorNotice({ message, retry }: { message: string; retry?: () => void }) {
  return <div className="error" role="alert"><span>{message}</span>{retry && <button onClick={retry}>Try again <Icon name="refresh" size={15} /></button>}</div>;
}
function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return <dialog ref={ref} onClose={onClose} className="dialog" aria-label={title}><header><h2>{title}</h2><button className="icon-button" onClick={() => ref.current?.close()} aria-label="Close dialog"><Icon name="close" /></button></header>{children}</dialog>;
}
type Citation = { title: string; source: Source; text?: string; section?: string; page?: number | null; period?: string; exact?: string | null };
function SourceDialog({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  const url = sourceUrl(citation.source.filing_url || citation.source.source_url || citation.source.snapshot_url);
  return <Dialog title="Source details" onClose={onClose}><div className="dialog-body"><p className="eyebrow">SEC EDGAR Â· PRIMARY SOURCE</p><h3>{citation.title}</h3><dl className="details">
    {citation.exact != null && <><dt>Reported value</dt><dd className="mono">{citation.exact}</dd></>}
    {citation.period && <><dt>Period ending</dt><dd>{date(citation.period)}</dd></>}
    <dt>Accession</dt><dd className="mono">{citation.source.accession_number || 'Not supplied'}</dd>
    {citation.section && <><dt>Section</dt><dd>{citation.section}</dd></>}
    {citation.text && <><dt>Page</dt><dd>{citation.page ?? 'Not available for this HTML filing'}</dd></>}
    {citation.source.last_successful_fetch && <><dt>Last checked</dt><dd>{date(citation.source.last_successful_fetch)}</dd></>}
    {citation.source.concept && <><dt>XBRL concept</dt><dd>{citation.source.concept}</dd></>}
  </dl>{citation.text && <blockquote>{citation.text}</blockquote>}{citation.source.content_hash && <details><summary>Source fingerprint</summary><code>{citation.source.content_hash}</code></details>}
  {url && <a className="button primary" href={url} target="_blank" rel="noreferrer">Open original source <Icon name="external" size={16} /></a>}</div></Dialog>;
}
function RevenueChart({ facts, onSource }: { facts: Fact[]; onSource: (fact: Fact) => void }) {
  const [selected, setSelected] = useState<number | null>(null);
  const points = facts.filter(f => f.value !== null).sort((a, b) => (a.period_end || '').localeCompare(b.period_end || ''));
  if (points.length < 2) return <Empty icon="compare" title="More history is needed">At least two reported annual periods are needed to draw a revenue trend.</Empty>;
  const values = points.map(p => Number(p.value)), max = Math.max(...values, 1), min = Math.min(...values, 0);
  const x = (i: number) => 70 + (i / (points.length - 1)) * 630;
  const y = (v: number) => 220 - ((v - min) / (max - min)) * 175;
  const line = values.map((v, i) => `${i ? 'L' : 'M'}${x(i)},${y(v)}`).join(' ');
  const active = Math.min(selected ?? points.length - 1, points.length - 1), fact = points[active];
  return <><div className="chart-summary"><div><strong>{number(fact.value)}</strong><span>Fiscal period ending {date(fact.period_end)}</span></div><button className="text-button" onClick={() => onSource(fact)}>View source <Icon name="external" size={14} /></button></div>
    <svg className="chart" viewBox="0 0 740 268" role="img" aria-label={`Annual revenue: ${points.map(p => `${date(p.period_end)} ${number(p.value, 'USD', false)}`).join('; ')}`}>
      <defs><linearGradient id="revenue-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="var(--accent)" stopOpacity=".17" /><stop offset="100%" stopColor="var(--accent)" stopOpacity="0" /></linearGradient></defs>
      {[0, .25, .5, .75, 1].map(t => <g key={t}><line x1="70" x2="700" y1={y(min + (max - min) * t)} y2={y(min + (max - min) * t)} className="gridline" /><text x="55" y={y(min + (max - min) * t) + 4} textAnchor="end">{number(String(min + (max - min) * t))}</text></g>)}
      <path d={`${line} L700,220 L70,220 Z`} fill="url(#revenue-fill)" /><path d={line} className="chart-line" />
      {points.map((p, i) => <g key={p.period_end}><circle cx={x(i)} cy={y(values[i])} r={active === i ? 5 : 3} className="chart-dot" /><text x={x(i)} y="250" textAnchor="middle">{p.period_end?.slice(0, 4)}</text></g>)}
    </svg><div className="chart-periods" aria-label="Select revenue period">{points.map((p, i) => <button key={p.period_end} className={active === i ? 'selected' : ''} onClick={() => setSelected(i)} aria-pressed={active === i}>{date(p.period_end)}</button>)}</div></>;
}
function FilingsTable({ filings, onSource }: { filings: Filing[]; onSource: (c: Citation) => void }) {
  if (!filings.length) return <Empty icon="filings" title="No filings available">Imported SEC filings will appear here.</Empty>;
  return <div className="table-scroll"><table><thead><tr><th>Document</th><th>Filed</th><th>Reporting period</th><th><span className="sr-only">Source</span></th></tr></thead><tbody>{filings.map(f => <tr key={f.accession_number}><td><span className="document-cell"><span className="document-icon"><Icon name="filings" size={17} /></span><span><strong>{f.form_type}</strong><small>{f.accession_number}</small></span></span></td><td>{date(f.filing_date)}</td><td>{date(f.report_period)}</td><td><button className="text-button" aria-label={`View ${f.form_type} filed ${f.filing_date}`} onClick={() => onSource({ title: `${f.form_type} Â· ${date(f.filing_date)}`, source: f, period: f.report_period || undefined })}>View <Icon name="arrow" size={16} /></button></td></tr>)}</tbody></table></div>;
}
function JobResult({ job, onSource }: { job: Job; onSource: (c: Citation) => void }) {
  const result = job.result;
  const rows = [...(result?.retrieved_facts || []), ...(result?.calculated_values || []).map(r => ({ result: { ...r.result, ticker: r.step.ticker, metric: r.step.metric } }))];
  const hits = result?.document_evidence.flatMap(d => d.result.retrieved_evidence) || [];
  const alignedRevenue = result?.comparisons.some(c => c.metric === 'revenue' && c.status === 'ALIGNED')
    ? rows.map(r => r.result).filter(f => f.metric === 'revenue' && f.value !== null) : [];
  const barMax = Math.max(...alignedRevenue.map(f => Math.abs(Number(f.value))), 1);
  return <div className="job-result"><div className="section-title"><h3>Research results</h3><Badge status={result?.status || job.status} /></div>
    {job.clarifications.length > 0 && <div className="notice"><strong>A little more context is needed</strong><ul>{job.clarifications.map(c => <li key={c}>{c}</li>)}</ul></div>}
    {job.error && <ErrorNotice message={`Research could not finish: ${label(job.error)}.`} />}
    {!!result?.unavailable.length && <div className="notice">{[...new Set(result.unavailable.map(u => u.reason))].map(reason => <p key={reason}>{label(reason)}.</p>)}</div>}
    {result?.comparisons.filter(c => c.status !== 'ALIGNED').map(c => <div className="notice" key={c.metric}>{label(c.metric)}: reporting periods or available values do not align. No ranking is shown.</div>)}
    {alignedRevenue.length > 1 && <div className="comparison-bars" aria-label="Revenue comparison"><h4>Revenue · aligned reporting periods</h4>{alignedRevenue.map(f => <div key={f.ticker}><span>{f.ticker}</span><div className="bar-track"><i style={{ width: `${Math.abs(Number(f.value)) / barMax * 100}%` }} /></div><strong>{number(f.value, f.unit)}</strong></div>)}</div>}{!!rows.length && <div className="table-scroll"><table><thead><tr><th>Company / metric</th><th>Value</th><th>Period ending</th><th>Evidence</th></tr></thead><tbody>{rows.map(({ result: f }, i) => <tr key={`${f.ticker}-${f.metric}-${i}`}><td><strong>{label(f.metric)}</strong><small>{f.ticker} Â· {f.source ? 'Reported' : 'Calculated'}</small></td><td className="numeric" title={f.value ?? undefined}>{number(f.value, f.unit)}</td><td>{date(f.period_end)}</td><td>{f.source ? <button className="text-button" onClick={() => onSource({ title: label(f.metric), source: f.source!, period: f.period_end, exact: `${f.value} ${f.unit}` })}>Source <Icon name="external" size={14} /></button> : f.operands ? <details><summary>Calculation inputs</summary>{Object.entries(f.operands).map(([name, input]) => <div className="operand" key={name}>{label(name)}: {input.value ?? 'Unavailable'} {input.unit}{input.source && <button className="text-button" onClick={() => onSource({ title: label(name), source: input.source!, period: input.period_end, exact: input.value })}>Source</button>}</div>)}</details> : <span className="muted">{f.reason || f.status}</span>}</td></tr>)}</tbody></table></div>}
    {!!result?.document_evidence.length && !hits.length && <div className="notice">No filing passages matched this scope. A filing must be indexed before research can retrieve its text.</div>}{!!hits.length && <><div className="section-title evidence-heading"><h3>Filing evidence</h3><span className="muted">{hits.length} passages Â· source text</span></div><p className="evidence-note">These are retrieved source passages, not an AI-generated conclusion.</p><div className="evidence-grid">{hits.map(hit => <button className="evidence-card" key={hit.chunk_id} onClick={() => onSource({ title: `${hit.filing_type} Â· ${date(hit.filing_date)}`, source: { ...hit, content_hash: hit.chunk_hash }, text: hit.text, section: hit.section, page: hit.page })}><span className="eyebrow">{hit.filing_type} Â· {date(hit.filing_date)}</span><h4>{hit.section || 'Filing passage'}</h4><p>{hit.text.slice(0, 240)}{hit.text.length > 240 ? 'â€¦' : ''}</p><span className="text-button">Inspect source <Icon name="arrow" size={15} /></span></button>)}</div></>}
    {!rows.length && !hits.length && !job.clarifications.length && !job.error && <Empty title="No matching evidence">Try a specific financial metric or a question about an indexed filing.</Empty>}
    <p className="job-id">Research ID <span className="mono">{job.job_id}</span></p>
  </div>;
}
function Research({ company, companies, period, apiKey, compare, onSource }: { company: Company; companies: Company[]; period: string; apiKey: string; compare: boolean; onSource: (c: Citation) => void }) {
  const [question, setQuestion] = useState(compare ? 'Compare revenue, net income, net margin and operating margin' : '');
  const [other, setOther] = useState(companies.find(c => c.cik !== company.cik)?.ticker || '');
  const [end, setEnd] = useState(period), [form, setForm] = useState('10-K');
  const [otherEnd, setOtherEnd] = useState(''), [otherLoading, setOtherLoading] = useState(false);
  useEffect(() => {
    if (!compare || !other) return;
    const controller = new AbortController(); setOtherLoading(true); setOtherEnd('');
    request<Fact>(`/companies/${encodeURIComponent(other)}/financials?metric=revenue&basis=annual`, apiKey, { signal: controller.signal })
      .then(f => { if (!controller.signal.aborted) setOtherEnd(f.period_end || ''); })
      .catch(e => { if (!controller.signal.aborted) setError((e as Error).message); })
      .finally(() => { if (!controller.signal.aborted) setOtherLoading(false); });
    return () => controller.abort();
  }, [other, apiKey, compare]);
  const [job, setJob] = useState<Job | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [resumeId, setResumeId] = useState('');
  async function recover(run = false) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(resumeId)) { setError('Enter a valid research ID.'); return; }
    setBusy(true); setError('');
    try { const recovered = await request<Job>(`/research/${resumeId}${run ? '/run' : ''}`, apiKey, run ? { method: 'POST' } : {}); if (alive.current) setJob(recovered); }
    catch (e) { if (alive.current) setError((e as Error).message); }
    finally { if (alive.current) setBusy(false); }
  }
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setJob(null);
    try {
      const response = await request<Job>(compare ? '/compare' : '/research', apiKey, { method: 'POST', body: JSON.stringify({ question, tickers: compare ? [company.ticker, other] : [company.ticker], ...(compare ? { issuer_periods: { [company.ticker]: { basis: 'annual', period_end: end }, [other]: { basis: 'annual', period_end: otherEnd } } } : end ? { period: { basis: 'annual', period_end: end } } : {}), form }) });
      if (alive.current) { setJob(response); setResumeId(response.job_id); }
    } catch (e) { if (alive.current) setError((e as Error).message); }
    finally { if (alive.current) setBusy(false); }
  }
  return <><div className="panel research-panel"><div className="section-title"><div><p className="eyebrow">{compare ? 'SIDE BY SIDE' : 'YOUR RESEARCH WORKSPACE'}</p><h2>{compare ? 'Compare the fundamentals' : 'Ask a better financial question.'}</h2></div><span className="soft-icon"><Icon name={compare ? 'compare' : 'research'} size={27} /></span></div>
    <p className="muted">{compare ? 'Compare reported values and calculated ratios. Different fiscal periods are flagged before you draw conclusions.' : 'Explore reported financials and passages from SEC filings, with evidence you can inspect.'}</p>
    <form onSubmit={submit}><div className="form-row">{compare && <label>Compare {company.ticker} with<select value={other} onChange={e => setOther(e.target.value)} required disabled={busy}>{companies.filter(c => c.cik !== company.cik).map(c => <option value={c.ticker} key={c.ticker}>{c.ticker} Â· {c.legal_name}</option>)}</select></label>}<label>{compare ? `${company.ticker} period ending` : 'Annual period ending'}<input type="date" value={end} onChange={e => setEnd(e.target.value)} disabled={busy} required={compare} /></label>{compare && <label>{other || 'Other company'} period ending<input type="date" value={otherEnd} onChange={e => setOtherEnd(e.target.value)} disabled={busy || otherLoading} required /></label>}<label>Filing type<select value={form} onChange={e => setForm(e.target.value)} disabled={busy}><option>10-K</option><option>10-Q</option><option>8-K</option></select></label></div>
      <label className="question-label" htmlFor="question">Research question</label><textarea id="question" placeholder={`What are the main business risks disclosed by ${company.ticker}?`} minLength={3} maxLength={2000} required value={question} onChange={e => setQuestion(e.target.value)} disabled={busy} />
      <div className="form-footer"><span>{question.length}/2,000 Â· {company.ticker}</span><button className="button primary" disabled={busy || (compare && (!other || !end || !otherEnd || otherLoading))}>{busy ? 'Researchingâ€¦' : compare ? 'Run comparison' : 'Run research'}<Icon name="arrow" size={17} /></button></div>
    </form>{!compare && <div className="suggestions">{['Revenue and net margin', 'Operating cash flow and free cash flow', 'What are the main business risks?'].map(q => <button key={q} onClick={() => setQuestion(q)} disabled={busy}>{q} <span>â†—</span></button>)}</div>}
  <details className="recover-research"><summary>Reopen a previous research job</summary><div className="recover-row"><label>Research ID<input value={resumeId} onChange={e => setResumeId(e.target.value)} maxLength={36} placeholder="Paste the ID from your results" disabled={busy} /></label><button className="button secondary" onClick={() => void recover()} disabled={busy || !resumeId}>Open result</button>{job && ['FAILED', 'QUEUED', 'RUNNING'].includes(job.status) && <button className="button primary" onClick={() => void recover(true)} disabled={busy}>Resume job</button>}</div></details></div>{error && <ErrorNotice message={error} />}{busy && <div className="panel loading-research" role="status"><span className="spinner" /><div><strong>Following the evidence</strong><p>Reading financial records and checking available filing sources. This can take a moment.</p></div></div>}{job && <div className="panel"><JobResult job={job} onSource={onSource} /></div>}
  {!job && !busy && <div className="research-guide"><div><span>01</span><h3>Start with a question</h3><p>Choose a company and an explicit reporting period for financial metrics.</p></div><div><span>02</span><h3>Separate facts from interpretation</h3><p>Reported values and calculated ratios are labeled in the results.</p></div><div><span>03</span><h3>Inspect the evidence</h3><p>Open the original SEC filing behind each reported value or passage.</p></div></div>}</>;
}
function readRoute(): { page: Page; ticker: string } {
  const parts = window.location.hash.replace(/^#\/?/, '').split('/');
  return { ticker: parts[0] || 'NVDA', page: nav.some(n => n.id === parts[1]) ? parts[1] as Page : 'overview' };
}
function App() {
  const [route, setRoute] = useState(readRoute), [companies, setCompanies] = useState<Company[]>([]);
  const [search, setSearch] = useState(''), [apiKey, setApiKey] = useState(''), [keyDraft, setKeyDraft] = useState('');
  const [connection, setConnection] = useState(false), [error, setError] = useState(''), [loading, setLoading] = useState(true);
  const [facts, setFacts] = useState<Fact[]>([]), [history, setHistory] = useState<Fact[]>([]), [filings, setFilings] = useState<Filing[]>([]);
  const [freshness, setFreshness] = useState<Freshness | null>(null), [refresh, setRefresh] = useState(0), [citation, setCitation] = useState<Citation | null>(null);
  const [companyError, setCompanyError] = useState(''), [companyLoading, setCompanyLoading] = useState(true);
  const [theme, setTheme] = useState(() => { try { return localStorage.getItem('marketiq-theme') || 'light'; } catch { return 'light'; } });
  const [filingFilter, setFilingFilter] = useState('all');
  const [dataTicker, setDataTicker] = useState('');
  useEffect(() => { const update = () => setRoute(readRoute()); window.addEventListener('hashchange', update); return () => window.removeEventListener('hashchange', update); }, []);
  useEffect(() => { document.documentElement.dataset.theme = theme; try { localStorage.setItem('marketiq-theme', theme); } catch { /* Theme still works without storage. */ } }, [theme]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError('');
    Promise.all([request<{ items: Company[] }>('/companies?limit=100', apiKey, { signal: controller.signal }), request<Freshness>('/data-freshness', apiKey, { signal: controller.signal })])
      .then(([list, fresh]) => { if (controller.signal.aborted) return; setCompanies(list.items); setFreshness(fresh); if (list.items.length && !list.items.some(c => c.ticker === readRoute().ticker)) window.location.hash = `/${list.items[0].ticker}/overview`; })
      .catch(e => { if (!controller.signal.aborted) { setError((e as Error).message); setCompanies([]); setFreshness(null); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [apiKey, refresh]);
  useEffect(() => {
    if (!companies.some(c => c.ticker === route.ticker)) return;
    const controller = new AbortController(); const signal = controller.signal;
    setCompanyLoading(true); setCompanyError(''); setFacts([]); setHistory([]); setFilings([]);
    const base = `/companies/${encodeURIComponent(route.ticker)}`;
    async function load() {
      try {
        const [revenue, list] = await Promise.all([request<Fact>(`${base}/financials?metric=revenue&basis=annual`, apiKey, { signal }), request<{ items: Filing[] }>(`${base}/filings?limit=100`, apiKey, { signal })]);
        const period = revenue.period_end ? `&period_end=${revenue.period_end}` : '';
        const rest = await Promise.all(metrics.slice(1).map(m => request<Fact>(`${base}/financials?metric=${m.id}&basis=${m.basis}${period}`, apiKey, { signal })));
        const ends = [...new Set(list.items.filter(f => f.form_type.startsWith('10-K') && f.report_period && (!revenue.period_end || f.report_period <= revenue.period_end)).map(f => f.report_period!))].sort().slice(-5);
        const past = await Promise.all(ends.map(end => end === revenue.period_end ? Promise.resolve(revenue) : request<Fact>(`${base}/financials?metric=revenue&basis=annual&period_end=${end}`, apiKey, { signal })));
        if (!signal.aborted) { setFacts([revenue, ...rest]); setFilings(list.items); setHistory(past); setDataTicker(route.ticker); }
      } catch (e) { if (!signal.aborted) setCompanyError((e as Error).message); }
      finally { if (!signal.aborted) setCompanyLoading(false); }
    }
    void load(); return () => controller.abort();
  }, [route.ticker, companies, apiKey, refresh]);
  const company = companies.find(c => c.ticker === route.ticker);
  const revenue = facts[0];
  const navigate = (page: Page, ticker = route.ticker) => { window.location.hash = `/${ticker}/${page}`; };
  const factSource = (fact: Fact) => { if (fact.source) setCitation({ title: `${fact.ticker} Â· ${label(fact.metric)}`, source: fact.source, period: fact.period_end, exact: `${fact.value} ${fact.unit}` }); };
  const titles: Record<Page, string> = { overview: 'Company overview', research: 'Financial research', compare: 'Company comparison', filings: 'SEC filing library', risk: 'Financial risk analysis' };
  return <div className="app-shell"><a href="#main-content" className="skip-link" onClick={e => { e.preventDefault(); document.getElementById('main-content')?.focus(); }}>Skip to content</a>
    <aside className="sidebar"><a className="brand" href={`#/${route.ticker}/overview`}><span className="brand-mark"><i /><i /><i /></span>Market<span>IQ</span><small>INTELLIGENCE WORKSPACE</small></a>
      <div className="sidebar-label">WORKSPACE</div><nav aria-label="Main navigation">{nav.map(n => <a key={n.id} title={n.label} aria-label={n.label} className={route.page === n.id ? 'nav-link active' : 'nav-link'} href={`#/${route.ticker}/${n.id}`} aria-current={route.page === n.id ? 'page' : undefined}><Icon name={n.id} /><span>{n.label}</span>{route.page === n.id && <span className="nav-dot" />}</a>)}</nav>
      <div className="sidebar-label company-label">COMPANIES <span>{companies.length}</span></div><div className="company-list">{companies.map(c => <button className={`company-item ${c.ticker === route.ticker ? 'selected' : ''}`} key={c.ticker} onClick={() => navigate(route.page, c.ticker)}><span className="mini-avatar">{c.ticker.slice(0, 1)}</span><span><strong>{c.ticker}</strong><small>{c.legal_name}</small></span>{c.ticker === route.ticker && <span className="company-dot" />}</button>)}{!loading && !companies.length && <p className="sidebar-empty">No companies loaded</p>}</div>
      <div className="sidebar-bottom"><div className="source-label"><span className="source-dot" /><div><strong>Built on primary sources</strong><small>SEC EDGAR financial filings</small></div></div><button className="nav-link" onClick={() => { setKeyDraft(apiKey); setConnection(true); }}><Icon name="settings" /><span>Connection</span></button><div className="sidebar-footer"><span>MarketIQ workspace</span><span>v0.1</span></div></div>
    </aside><div className="workspace"><header className="topbar"><div className="breadcrumb">Workspace <span>/</span><strong>{titles[route.page]}</strong></div><div className="top-actions"><div className="company-search"><Icon name="search" size={17} /><input aria-label="Search companies" placeholder="Find a companyâ€¦" value={search} onChange={e => setSearch(e.target.value)} onKeyDown={e => { if (e.key === 'Escape') setSearch(''); }} />{search && <div className="search-results">{companies.filter(c => `${c.ticker} ${c.legal_name}`.toLowerCase().includes(search.toLowerCase())).map(c => <button key={c.ticker} onClick={() => { navigate(route.page, c.ticker); setSearch(''); }}><strong>{c.ticker}</strong><span>{c.legal_name}</span></button>)}{!companies.some(c => `${c.ticker} ${c.legal_name}`.toLowerCase().includes(search.toLowerCase())) && <p>No imported companies match.</p>}</div>}</div><button className="icon-button" aria-label="Connection settings" onClick={() => { setKeyDraft(apiKey); setConnection(true); }}><Icon name="settings" /></button><button className="icon-button" aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`} onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}><Icon name={theme === 'light' ? 'moon' : 'sun'} /></button><span className="user-avatar" title="Local workspace">MI</span></div></header>
    <main id="main-content" tabIndex={-1}>
      <div className="page-heading"><div><p className="eyebrow">{route.page === 'overview' ? 'THE COMPANY, IN CONTEXT' : 'EVIDENCE BEFORE ASSUMPTIONS'}</p><h1>{titles[route.page]}</h1><p className="subtitle">{route.page === 'overview' ? 'A clearer view of the business behind the ticker.' : 'Reported data. Traceable sources. Clear limitations.'}</p></div><button className="button secondary refresh-button" aria-label="Reload stored data" onClick={() => setRefresh(n => n + 1)} disabled={loading || companyLoading && !!company}><Icon name="refresh" size={16} />Reload data</button></div>
      {error && <ErrorNotice message={error} retry={() => setRefresh(n => n + 1)} />}
      {loading && <div className="skeleton-grid" role="status" aria-label="Loading companies">{[0, 1, 2, 3].map(i => <div key={i} className="skeleton" />)}</div>}
      {!loading && !error && !company && <div className="panel"><Empty icon="overview" title="Your workspace is ready for data">Import company financials to populate the dashboard.</Empty></div>}
      {company && <><section className="company-heading"><div className={`company-avatar avatar-${company.ticker}`}>{company.ticker.slice(0, 1)}</div><div className="company-heading-text"><div className="company-name"><h2>{company.legal_name}</h2><span className="ticker">{company.ticker}</span></div><p>CIK {company.cik} <span>Â·</span> US public company filings <span>Â·</span> USD</p></div><div className="company-freshness"><Badge status={dataTicker === route.ticker ? revenue?.status || 'UNAVAILABLE' : 'LOADING'} /><small>Company data checked {date(dataTicker === route.ticker ? revenue?.source?.last_successful_fetch : null)}</small></div></section>
      {companyError && <ErrorNotice message={companyError} retry={() => setRefresh(n => n + 1)} />}
      {companyLoading || (dataTicker !== route.ticker && !companyError) ? <div className="skeleton-grid" role="status" aria-label="Loading financials">{[0, 1, 2, 3].map(i => <div key={i} className="skeleton" />)}</div> : <>
        {route.page === 'overview' && <><div className="metrics-grid">{metrics.map((m, i) => <button className="metric-card" key={m.id} onClick={() => facts[i] && factSource(facts[i])} disabled={!facts[i]?.source}><span>{m.label}<Icon name="external" size={14} /></span><strong>{number(facts[i]?.value)}</strong><small>{facts[i]?.value == null ? 'No matching reported value' : `${m.basis === 'instant' ? 'As of' : 'FY ended'} ${date(facts[i]?.period_end)}`}</small></button>)}</div>
          <div className="overview-grid"><section className="panel revenue-panel"><div className="section-title"><div><h3>Revenue performance</h3><p>Annual reported revenue Â· USD</p></div><span className="subtle-tag">Up to 5 periods</span></div><RevenueChart facts={history} onSource={factSource} /></section><div className="insights-column"><section className="research-callout"><span className="callout-icon"><Icon name="research" size={24} /></span><p className="eyebrow">GO BEYOND THE NUMBERS</p><h3>Turn a question<br />into understanding.</h3><p>Explore financials and find the filing evidence behind them.</p><button className="button" onClick={() => navigate('research')}>Open research <Icon name="arrow" size={16} /></button></section><section className="panel market-card"><div className="section-title"><h3>Market data</h3><span className="subtle-tag">Not connected</span></div><p>Live quotes and price history are not available. Financial figures here come from SEC filings.</p></section></div></div>
          <section className="panel filings-panel"><div className="section-title"><div><h3>Latest filings</h3><p>Primary documents, directly from the source</p></div><button className="text-button" onClick={() => navigate('filings')}>View all filings <Icon name="arrow" size={16} /></button></div><FilingsTable filings={filings.slice(0, 4)} onSource={setCitation} /></section></>}
        {(route.page === 'research' || route.page === 'compare') && <Research key={`${company.ticker}-${route.page}-${apiKey}`} company={company} companies={companies} period={revenue?.period_end || ''} apiKey={apiKey} compare={route.page === 'compare'} onSource={setCitation} />}
        {route.page === 'filings' && <section className="panel filings-panel"><div className="section-title"><div><h3>Company filings</h3><p>{filings.length} latest imported documents</p></div><label className="inline-filter"><span className="sr-only">Filter filings by form</span><select value={filingFilter} onChange={e => setFilingFilter(e.target.value)}><option value="all">All forms</option><option value="10-K">10-K Â· Annual</option><option value="10-Q">10-Q Â· Quarterly</option><option value="8-K">8-K Â· Current</option></select></label></div><FilingsTable filings={filings.filter(f => filingFilter === 'all' || f.form_type.startsWith(filingFilter))} onSource={setCitation} /></section>}
        {route.page === 'risk' && <section className="panel risk-panel"><div className="risk-intro"><div className="empty-icon"><Icon name="risk" size={32} /></div><span className="subtle-tag">MODEL NOT APPROVED</span><h2>Reliable predictions need<br />a reliable foundation.</h2><p>No approved risk model is available for {company.ticker}. Probability, category, and SHAP contributions are unavailable until the training data and model pass validation.</p><button className="button primary" onClick={() => navigate('research')}>Explore financial evidence <Icon name="arrow" size={17} /></button></div><div className="risk-facts"><div><small>MODEL TARGET</small><h3>Next fiscal yearâ€™s negative operating cash flow</h3><p>This is distinct from bankruptcy probability or an investment recommendation.</p></div><div><small>CURRENT LIMITATION</small><h3>Historical data coverage</h3><p>Eligible forward annual labels and verified historical issuer coverage are still needed.</p></div><div><small>EXPLAINABILITY</small><h3>SHAP, when a model is approved</h3><p>Feature contributions will explain model output in log-odds, with source-backed inputs.</p></div></div></section>}
      </>}<footer className="content-footer"><span><Icon name="check" size={14} /> SEC EDGAR Â· Reported financial data</span><span>Workspace snapshot {date(freshness?.last_snapshot_observed_at)} Â· Reload does not refresh SEC sources.</span></footer></>}
    </main></div>
    {citation && <SourceDialog citation={citation} onClose={() => setCitation(null)} />}
    {connection && <Dialog title="Workspace connection" onClose={() => setConnection(false)}><form className="dialog-body" onSubmit={e => { e.preventDefault(); setApiKey(keyDraft.trim()); setRefresh(n => n + 1); setConnection(false); }}><p className="muted">Connect to your local MarketIQ API. Your API key stays in memory and is cleared when this page reloads.</p><label>API address<input readOnly value={apiBase} /></label><p className="field-help">The address is configured with VITE_API_BASE_URL.</p><label>API key<input type="password" autoComplete="off" value={keyDraft} onChange={e => setKeyDraft(e.target.value)} placeholder="Optional for local development" /></label><button className="button primary" type="submit">Connect <Icon name="arrow" size={16} /></button></form></Dialog>}
  </div>;
}
const root = document.getElementById('root');
if (!root) throw new Error('Application root is missing');
createRoot(root).render(<StrictMode><App /></StrictMode>);
