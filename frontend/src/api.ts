export interface Company { ticker: string; cik: string; legal_name: string }
export interface Source {
  provider?: string; filing_url?: string; source_url?: string; snapshot_url?: string;
  accession_number?: string; content_hash?: string; first_observed_at?: string;
  last_successful_fetch?: string; concept?: string;
}
export interface Fact {
  ticker: string; metric: string; value: string | null; unit: string; basis?: string;
  status: string; period_start?: string; period_end?: string; reason?: string;
  source?: Source; formula?: string; operands?: Record<string, Fact>;
}
export interface Filing {
  accession_number: string; form_type: string; filing_date: string;
  report_period: string | null; source_url: string;
}
export interface Evidence {
  source_id: string; chunk_id: string; text: string; source_url: string;
  filing_type: string; filing_date: string; accession_number: string;
  section?: string; page?: number | null; chunk_hash?: string;
}
export interface ResearchResult {
  status: string; retrieved_facts: { result: Fact }[];
  calculated_values: { result: Fact; step: { ticker: string; metric: string } }[];
  document_evidence: { result: { status: string; reason?: string; retrieved_evidence: Evidence[] } }[];
  unavailable: { reason: string }[];
  comparisons: { metric: string; status: string; reason?: string }[];
}
export interface Job {
  job_id: string; status: string; result: ResearchResult | null;
  clarifications: string[]; error?: string | null;
}
export interface Freshness { status: string; last_snapshot_observed_at: string | null }

export const apiBase = ((import.meta as ImportMeta & { env: Record<string, string> }).env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');

export async function request<T>(path: string, key: string, options: RequestInit = {}): Promise<T> {
  const signal = options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(120000)]) : AbortSignal.timeout(120000);
  let response: Response;
  try {
    response = await fetch(`${apiBase}/api${path}`, { ...options, signal, headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(key ? { Authorization: `Bearer ${key}` } : {}), ...options.headers,
    } });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new Error(error instanceof DOMException && error.name === 'TimeoutError'
      ? 'The request timed out. Research may still be running on the server.'
      : 'Cannot reach MarketIQ. Check that the API server is running and the connection address is correct.', { cause: error });
  }
  if (!response.ok) {
    const messages: Record<number, string> = {
      401: 'An API key is required or incorrect. Open Connection to update it.',
      404: 'This company or research job was not found.', 409: 'This research job is still running.',
      410: 'This research job has expired.', 422: 'Check the selected companies, dates, and question.',
      429: 'The request limit was reached. Wait a minute before trying again.', 503: 'This service is currently unavailable.',
    };
    throw new Error(messages[response.status] || 'The server could not complete this request.');
  }
  return response.json() as Promise<T>;
}
export function sourceUrl(url?: string): string | undefined {
  if (!url) return undefined;
  try { const parsed = new URL(url); return ['https:', 'http:'].includes(parsed.protocol) ? parsed.href : undefined; }
  catch { return undefined; }
}
