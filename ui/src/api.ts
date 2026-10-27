async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const b = await r.json(); msg = typeof b.detail === "string" ? b.detail : JSON.stringify(b.detail ?? b); } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

export type Me = { email: string; obo: boolean; workspace: string; catalog_schema: string };
export type PaperStatus = { template_id: string | null; slots: number; filled: number; accepted: number; citations: number; source_wording: number | null };
export type DealRow = { deal_id: string; deal_name: string; borrower: string; sponsor: string; sector: string; sub_sector: string; state: string;
  status: string; lifecycle_stage?: string; request_summary?: string; facility_limit?: string; documents: number; paper: PaperStatus | null;
  /** an earlier project on the deal (its chats and paper kept); updated = when its paper last changed (epoch s) */
  earlier?: boolean; updated?: number | null };
/** A library document the deal's chats used: read, attached or cited. */
export type Doc = { doc_id: string; title: string; issuer: string; pages: number; doc_type?: string; as_of?: string; superseded_by?: string; how: string[] };
export type Counterparty = { role: string; counterparty_id: string; name: string };
export type MetricView = { name: string; fq: string; comment: string; measures: string[]; dimensions: string[] };
/** A table the deal's chats looked at: the deal record, or a metric view queried (its last few queries, newest first). */
export type TableSource = { key: string; kind: "record" | "metric"; title: string; sub: string; fq: string; catalog_path: string; first: number;
  queries: { sql: string; at: number; table: { columns: string[]; rows: string[][] } }[] };
export type DealDetail = { deal: Record<string, string>; counterparties: Counterparty[]; documents: Doc[]; tables?: TableSource[]; metric_views: MetricView[]; workspace: string };
export type TemplateRow = { template_id: string; name: string; purpose?: string; use_when?: string; slots: number; custom?: boolean; based_on?: string };
export type TemplateDetail = Omit<TemplateRow, "slots"> & { docx: string; slots: SlotSpec[] };
export type TemplateEdit = { base_id: string; name: string; purpose: string; use_when: string;
  edits: { remove: string[]; sections: Record<string, string>; slots: Record<string, { alias: string; guidance: string; preferred_source: string }> } };
export type SlotSpec = { tag: string; section: string; kind: "text" | "table" | "image" | "metric"; alias?: string; guidance: string; preferred_source: string };

export type PdfCitation = {
  id: string; kind: "pdf"; element_id: string; doc_id: string; doc_title: string; issuer: string; page: number; type: string;
  quote: string | null; source_text: string; rects: number[][]; box: number[]; page_width: number; page_height: number;
  doc_type?: string; as_of?: string; superseded_by?: string | null;
};
export type MetricCitation = {
  id: string; kind: "metric"; view: string; label?: string; fq: string; measure: string; definition: string; value: string | null; formatted: string | null;
  dimensions: Record<string, string>; filters: Record<string, string>; sql: string; as_of: string; catalog_path: string;
  table?: { columns: string[]; rows: string[][] };
};
export type Citation = PdfCitation | MetricCitation;
export type Segment = { text: string; mode: "verbatim" | "edited" | "metric" | "agent" | "analyst" | "analyst_edited"; similarity?: number; citation: string | null;
  original?: string; bold?: boolean; italic?: boolean };
export type Slot = {
  tag: string; kind: SlotSpec["kind"]; status: "empty" | "filled" | "accepted";
  paragraphs?: { bullet: boolean; segments: Segment[] }[]; rows?: string[][]; caption?: string | null;
  image?: { uri: string; source: "figure" | "chart" }; text?: string; citations?: Citation[]; row_citations?: Record<string, string>;
  source_wording?: number; filled_at?: number; history?: unknown[];
};
export type Paper = { sid: string; deal_id: string; version: number; updated: number; template_id: string | null; template_reason: string | null;
  slots: Record<string, Slot>; focus?: { tag: string; at: number }; status: PaperStatus;
  template: { template_id: string; name: string; slots: SlotSpec[] } | null };

export type AgentItem = { id: string; type: string; status?: string; created_at: number;
  data: { role?: string; content?: { type: string; text?: string }[]; name?: string; arguments?: string; output?: string; call_id?: string } };
export type Elicitation = { elicitation_id?: string; id?: string; params?: { message?: string; tool_name?: string; arguments?: unknown; [k: string]: unknown }; [k: string]: unknown };
export type Snapshot = { id: string; title: string; status: string; items: AgentItem[]; mode: string; deal_id: string; labels?: Record<string, string>;
  pending_elicitations?: Elicitation[]; last_task_error?: unknown };
export type ThreadRow = { id: string; title: string; status: string; created_at: number; updated_at: number; model: string; deal_id: string };
export type HarnessOpt = { id: string; label: string; models: string[]; default_model: string };
export type Options = { harnesses: HarnessOpt[]; default_harness: string; models: string[]; default_model: string; modes: { id: string; label: string }[];
  runtime: { up: boolean; error: string | null } };
export type FileJob = { id: string; name: string; deal_id: string; state: "parsing" | "ready" | "failed"; error?: string };
export type SourceDoc = Doc & { deal_id: string; page_images: { page: number; width: number; height: number }[];
  elements: { element_id: string; page: number; type: string; text: string; x0: number; y0: number; x1: number; y1: number; page_width: number; page_height: number }[] };

export const api = {
  me: () => req<Me>("/api/me"),
  deals: () => req<DealRow[]>("/api/deals"),
  deal: (id: string) => req<DealDetail>(`/api/deals/${id}`),
  fresh: (id: string) => req<{ deal_id: string }>(`/api/deals/${id}/fresh`, { method: "POST" }),
  templates: () => req<TemplateRow[]>("/api/templates"),
  paper: (deal: string) => req<Paper>(`/api/deals/${deal}/paper`),
  review: (deal: string, tag: string, action: string, text?: string) =>
    req(`/api/deals/${deal}/slots/${tag}/review`, { method: "POST", body: JSON.stringify({ action, text }) }),
  edit: (deal: string, tag: string, paragraphs: { bullet: boolean; segments: { text: string; citation: string | null; bold?: boolean; italic?: boolean }[] }[]) =>
    req(`/api/deals/${deal}/slots/${tag}/edit`, { method: "POST", body: JSON.stringify({ paragraphs }) }),
  templateThumbUrl: (id: string) => `/api/templates/${id}/thumb.png`,
  redo: (deal: string, tag: string, thread_id: string, note?: string) =>
    req(`/api/deals/${deal}/slots/${tag}/redo`, { method: "POST", body: JSON.stringify({ note, thread_id }) }),
  exportUrl: (deal: string) => `/api/deals/${deal}/export.docx`,
  pageUrl: (doc: string, page: number, size: "thumb" | "full" = "thumb") => `/api/sources/${doc}/pages/${page}.jpg${size === "full" ? "?size=full" : ""}`,
  warm: (deal: string) => req(`/api/deals/${deal}/warm`, { method: "POST" }),
  templateDetail: (id: string) => req<TemplateDetail>(`/api/templates/${id}`),
  templateFileUrl: (id: string, v = 0) => `/api/templates/${id}/file.docx${v ? `?v=${v}` : ""}`,
  createTemplate: (body: TemplateEdit) => req<{ template_id: string }>("/api/templates", { method: "POST", body: JSON.stringify(body) }),
  uploadTemplate: async (f: File, meta: { name: string; purpose: string; use_when: string }) => {
    const fd = new FormData();
    fd.append("file", f);
    Object.entries(meta).forEach(([k, v]) => fd.append(k, v));
    const r = await fetch("/api/templates/upload", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
    return r.json() as Promise<{ template_id: string; slots: number; sections: number }>;
  },
  updateTemplate: (id: string, body: TemplateEdit) => req<{ template_id: string }>(`/api/templates/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  fileUrl: (uri: string) => `/api/file?uri=${encodeURIComponent(uri)}`,
  source: (doc: string) => req<SourceDoc>(`/api/sources/${doc}`),
  files: (deal: string) => req<FileJob[]>(`/api/deals/${deal}/files`),
  addFile: async (deal: string, f: File) => {
    const fd = new FormData();
    fd.append("file", f);
    const r = await fetch(`/api/deals/${deal}/files`, { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
    return (await r.json()) as FileJob;
  },
  options: () => req<Options>("/api/agent/options"),
  threads: () => req<ThreadRow[]>("/api/agent/threads"),
  thread: (sid: string) => req<Snapshot>(`/api/agent/threads/${sid}`),
  streamUrl: (sid: string) => `/api/agent/threads/${sid}/stream`,
  start: (deal_id: string, message: string, harness: string, model: string, mode: string, attachments: string[]) =>
    req<{ thread_id: string }>("/api/agent/threads", { method: "POST", body: JSON.stringify({ deal_id, message, harness, model, mode, attachments }) }),
  send: (sid: string, text: string, attachments: string[]) =>
    req(`/api/agent/threads/${sid}/messages`, { method: "POST", body: JSON.stringify({ text, attachments }) }),
  setMode: (sid: string, mode: string) => req(`/api/agent/threads/${sid}/mode`, { method: "PUT", body: JSON.stringify({ mode }) }),
  resolve: (sid: string, eid: string, action: "accept" | "decline") =>
    req(`/api/agent/threads/${sid}/elicitations/${encodeURIComponent(eid)}`, { method: "POST", body: JSON.stringify({ action }) }),
  stop: (sid: string) => req(`/api/agent/threads/${sid}/interrupt`, { method: "POST" }),
  remove: (sid: string) => req(`/api/agent/threads/${sid}`, { method: "DELETE" }),
  upload: async (deal: string, f: File) => {
    const fd = new FormData();
    fd.append("file", f);
    const r = await fetch(`/api/uploads?deal_id=${encodeURIComponent(deal)}`, { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? r.statusText);
    return (await r.json()) as { path: string; name: string; bytes: number };
  },
};

export function parse(s?: string): Record<string, unknown> | null {
  if (!s) return null;
  try {
    const v = JSON.parse(s);
    if (v && typeof v === "object" && "result" in v && typeof (v as { result: unknown }).result === "string") return parse((v as { result: string }).result) ?? v;
    if (Array.isArray(v) && v[0]?.type === "text" && typeof v[0].text === "string") return parse(v[0].text) ?? { text: v[0].text };
    return v;
  } catch { return null; }
}

/** mcp__studio__fill_text → fill_text */
export const toolName = (n?: string) => String(n ?? "").split(/__|\.|:|\//).pop() ?? "";  // harnesses prefix MCP tools differently

export const prettyModel = (m: string) =>
  m.replace(/^databricks-/, "").replace(/^claude-/, "Claude ").replace(/^gpt-(\d+)-(\d+)$/, "GPT-$1.$2").replace(/^gpt-(\d+)$/, "GPT-$1")
    .replace(/-(\d+)-(\d+)$/, " $1.$2").replace(/-(\d+)$/, " $1").replace(/(^|\s)(\w)/g, (_, a, b) => a + b.toUpperCase());

/** Citations in paper order: id → 1-based number, so the paper, its margin and the export all agree. */
export function citationNumbers(paper: Paper | null): Map<string, number> {
  const out = new Map<string, number>();
  for (const s of paper?.template?.slots ?? []) {
    const slot = paper?.slots[s.tag];
    if (!slot || slot.status === "empty") continue;
    const ids = [...(slot.paragraphs ?? []).flatMap((p) => p.segments.map((x) => x.citation)), ...(slot.citations ?? []).map((c) => c.id)];
    for (const id of ids) if (id && !out.has(id)) out.set(id, out.size + 1);
  }
  return out;
}

/** What a reader calls each governed source; the technical name stays in the query. */
export const VIEW_LABELS: Record<string, string> = { mv_deal_metrics: "Quillwood deal records", mv_portfolio_exposure: "Quillwood loan book", mv_corporate_credit: "Corporate credit metrics" };
export const viewLabel = (c: { view: string; label?: string }) => c.label ?? VIEW_LABELS[c.view] ?? c.view;

/** How the library labels a document's type, for people. */
export const DOC_TYPE: Record<string, string> = {
  valuation: "Valuation", qs_report: "QS report", builder_assessment: "Builder assessment", information_memorandum: "Information memorandum",
  deal_intake: "Deal intake note", term_sheet: "Term sheet", financials: "Financials", compliance_certificate: "Compliance certificate",
  facility_summary: "Facility summary", variation_request: "Variation request", ic_paper: "IC paper", committee_minutes: "Committee minutes",
  credit_policy: "Credit policy", research: "Research", leasing_report: "Leasing report", correspondence: "Correspondence", other: "Document",
};
export const shortDate = (iso?: string | null) => {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  return isNaN(+d) ? iso : d.toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" });
};
