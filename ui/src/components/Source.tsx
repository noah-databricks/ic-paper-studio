import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, ExternalLink, FileText, Table2, X } from "lucide-react";
import { api, shortDate, viewLabel, type Citation, type MetricCitation, type PdfCitation, type SourceDoc, type TableSource } from "../api";
import { Sql } from "../lib/sql";

/** A cited region of a page: enough of the page to recognise it, with the cited lines under a highlighter. */
export function PagePreview({ doc, page, rects, pw, ph, width, share = 0.42 }: {
  doc: string; page: number; rects: number[][]; pw: number; ph: number; width: number; share?: number;
}) {
  const s = width / pw;
  const top = Math.min(...rects.map((r) => r[1])), bottom = Math.max(...rects.map((r) => r[3]));
  const h = Math.min(ph, Math.max(ph * share, bottom - top + 120));
  const y0 = Math.max(0, Math.min(ph - h, (top + bottom) / 2 - h / 2));
  return (
    <div className="pp" style={{ width, height: h * s }}>
      <img src={api.pageUrl(doc, page)} alt={`Page ${page}`} style={{ width, top: -y0 * s }} loading="eager" decoding="async" />
      {rects.map((r, i) => <span key={i} className="mark" style={{ left: r[0] * s - 2, top: (r[1] - y0) * s - 1, width: (r[2] - r[0]) * s + 4, height: (r[3] - r[1]) * s + 2 }} />)}
    </div>
  );
}

type Edited = { text: string; original?: string };

function PdfCard({ c, width, onOpen, edited }: { c: PdfCitation; width: number; onOpen: () => void; edited?: Edited }) {
  return (
    <>
      <div className="sc-head"><FileText size={14} /><div><b>{c.doc_title}</b><span>{[c.issuer, shortDate(c.as_of), `page ${c.page}`].filter(Boolean).join(" · ")}</span></div></div>
      {c.superseded_by && <div className="sc-flag">A later version of this document replaces it.</div>}
      {edited && <div className="sc-edited"><b>You edited this passage.</b> The source says: <q>{c.quote ?? edited.original}</q></div>}
      <button className="sc-page" onClick={onOpen} aria-label={`Open ${c.doc_title} at page ${c.page}`}>
        <PagePreview doc={c.doc_id} page={c.page} rects={c.rects.length ? c.rects : [c.box]} pw={c.page_width} ph={c.page_height} width={width} />
      </button>
      <div className="sc-foot"><button className="link" onClick={onOpen}>Open document</button></div>
    </>
  );
}

function MetricCard({ c, workspace }: { c: MetricCitation; workspace: string }) {
  const [q, setQ] = useState(false);
  const dims = Object.entries(c.dimensions ?? {}).filter(([k]) => k !== c.measure);
  const t = c.table ?? { columns: [c.measure, ...dims.map(([k]) => k)], rows: [[c.formatted ?? c.value ?? "", ...dims.map(([, v]) => v)]] };
  const col = t.columns.indexOf(c.measure);
  return (
    <>
      <div className="sc-head"><Table2 size={14} /><div><b>{c.measure} <span className="sc-val">{c.formatted ?? c.value}</span></b><span>{viewLabel(c)} · governed figure</span></div></div>
      <div className="sc-table">
        <table>
          <thead><tr>{t.columns.map((h, i) => <th key={i} className={i === col ? "hit" : ""}>{h}</th>)}</tr></thead>
          <tbody>{t.rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j} className={j === col ? "hit" : ""}>{v}</td>)}</tr>)}</tbody>
        </table>
      </div>
      <div className="sc-foot">
        <button className="link" onClick={() => setQ((v) => !v)} aria-expanded={q}>{q ? "Hide query" : "Show query"} <ChevronDown size={12} style={{ transform: q ? "rotate(180deg)" : undefined }} /></button>
        <a className="link" href={`${workspace}${c.catalog_path}`} target="_blank" rel="noreferrer"><ExternalLink size={12} /> Catalog Explorer</a>
      </div>
      {q && <><div className="sc-tech">Unity Catalog metric view <code>{c.fq}</code></div><Sql text={c.sql} /></>}
    </>
  );
}

/** A table the agent looked at: what each of its queries returned, and the query itself. */
export function TableCard({ t, workspace }: { t: TableSource; workspace: string }) {
  const [q, setQ] = useState<number | null>(null);
  return (
    <div className="sc">
      {t.queries.map((x, i) => (
        <div key={i} className="sc-query">
          {t.queries.length > 1 && <div className="sc-tech">Query {i + 1} of {t.queries.length}</div>}
          <div className="sc-table">
            <table>
              <thead><tr>{x.table.columns.map((h, j) => <th key={j}>{h}</th>)}</tr></thead>
              <tbody>{x.table.rows.map((r, j) => <tr key={j}>{r.map((v, k) => <td key={k}>{v}</td>)}</tr>)}</tbody>
            </table>
          </div>
          <div className="sc-foot">
            <button className="link" onClick={() => setQ(q === i ? null : i)} aria-expanded={q === i}>{q === i ? "Hide query" : "Show query"} <ChevronDown size={12} style={{ transform: q === i ? "rotate(180deg)" : undefined }} /></button>
            {i === 0 && <a className="link" href={`${workspace}${t.catalog_path}`} target="_blank" rel="noreferrer"><ExternalLink size={12} /> Catalog Explorer</a>}
          </div>
          {q === i && <><div className="sc-tech">{t.kind === "metric" ? "Unity Catalog metric view" : "Unity Catalog table"} <code>{t.fq}</code></div><Sql text={x.sql} /></>}
        </div>
      ))}
    </div>
  );
}

export function SourceCard({ c, width, workspace, onOpenDoc, edited }: { c: Citation; width: number; workspace: string; onOpenDoc: (c: PdfCitation) => void; edited?: Edited }) {
  return <div className="sc">{c.kind === "pdf" ? <PdfCard c={c} width={width} onOpen={() => onOpenDoc(c)} edited={edited} /> : <MetricCard c={c} workspace={workspace} />}</div>;
}

/** A document to open in the viewer without a citation: at a page, optionally highlighting one passage. */
export function docAt(doc_id: string, page = 1, element_id = "", title = "", issuer = ""): PdfCitation {
  return { id: `open:${doc_id}:${page}:${element_id}`, kind: "pdf", element_id, doc_id, doc_title: title, issuer, page, type: "", quote: null,
    source_text: "", rects: [], box: [], page_width: 0, page_height: 0 };
}

/** A parsed element (a chat citation) as a citation the card can show. */
export function elementCitation(d: SourceDoc, id: string): PdfCitation | null {
  const e = d.elements.find((x) => x.element_id === id);
  if (!e) return null;
  return { id, kind: "pdf", element_id: id, doc_id: d.doc_id, doc_title: d.title, issuer: d.issuer, page: e.page, type: e.type, quote: null,
    source_text: e.text, rects: [[e.x0, e.y0, e.x1, e.y1]], box: [e.x0, e.y0, e.x1, e.y1], page_width: e.page_width, page_height: e.page_height };
}

// ---- hover cards: one floating card, shown from the chat, the paper or the Word view -------------------------------
type Target = { c: Citation; edited?: Edited } | { element: string };
type HoverApi = { show: (t: Target, el: HTMLElement) => void; hide: () => void };
const Ctx = createContext<HoverApi>({ show: () => {}, hide: () => {} });
export const useSourceHover = () => useContext(Ctx);

export function SourceHoverProvider({ children, workspace, onOpenDoc }: { children: React.ReactNode; workspace: string; onOpenDoc: (c: PdfCitation) => void }) {
  const [state, setState] = useState<{ c: Citation | null; rect: DOMRect; loading?: boolean; edited?: Edited } | null>(null);
  const [docs, setDocs] = useState<Record<string, SourceDoc>>({});
  const timer = useRef(0), over = useRef(false);
  const show = useCallback(async (t: Target, el: HTMLElement) => {
    window.clearTimeout(timer.current);
    const rect = el.getBoundingClientRect();
    if ("c" in t) { setState({ c: t.c, rect, edited: t.edited }); return; }
    const doc = t.element.split("#")[0];
    let d = docs[doc];
    if (!d) {
      setState({ c: null, rect, loading: true });
      try { d = await api.source(doc); setDocs((x) => ({ ...x, [doc]: d })); } catch { setState(null); return; }
    }
    setState({ c: elementCitation(d, t.element), rect });
  }, [docs]);
  const hide = useCallback(() => { window.clearTimeout(timer.current); timer.current = window.setTimeout(() => { if (!over.current) setState(null); }, 220); }, []);
  const W = 340;
  const pos = state ? { left: Math.max(8, Math.min(state.rect.left - 24, window.innerWidth - W - 34)), top: state.rect.bottom + 8 } : null;
  const flip = state && pos && pos.top > window.innerHeight - 340;
  return (
    <Ctx.Provider value={{ show, hide }}>
      {children}
      {state && pos && (
        <div className="hovercard" style={flip ? { left: pos.left, bottom: window.innerHeight - state.rect.top + 8 } : pos}
          onMouseEnter={() => { over.current = true; window.clearTimeout(timer.current); }} onMouseLeave={() => { over.current = false; hide(); }}>
          {state.c ? <SourceCard c={state.c} width={W} workspace={workspace} edited={state.edited} onOpenDoc={(c) => { setState(null); onOpenDoc(c); }} /> : <div className="muted">Loading the source…</div>}
        </div>
      )}
    </Ctx.Provider>
  );
}

/** The whole document, page by page, opened at the cited page with the cited lines highlighted. */
export function DocViewer({ c, onClose }: { c: PdfCitation; onClose: () => void }) {
  const [d, setD] = useState<SourceDoc | null>(null), [page, setPage] = useState(c.page);
  // opened from a search hit: highlight the passage once the document is loaded; opened from a document: no highlight
  const el = !c.rects.length && c.element_id ? d?.elements.find((x) => x.element_id === c.element_id) : undefined;
  const marked = c.rects.length ? c.rects : el ? [[el.x0, el.y0, el.x1, el.y1]] : c.box.length === 4 ? [c.box] : [];
  const box = useRef<HTMLDivElement>(null), [w, setW] = useState(700);
  useEffect(() => { api.source(c.doc_id).then(setD).catch(() => setD(null)); }, [c.doc_id]);
  useEffect(() => { const el = box.current; if (!el) return; const ro = new ResizeObserver(() => setW(Math.min(860, el.clientWidth - 48))); ro.observe(el); return () => ro.disconnect(); }, []);
  useEffect(() => { if (d) window.setTimeout(() => document.getElementById(`dv-${c.page}`)?.scrollIntoView({ block: "start" }), 60); }, [d, c.page]);
  useEffect(() => { const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); }; window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k); }, [onClose]);
  const go = (n: number) => { setPage(n); document.getElementById(`dv-${n}`)?.scrollIntoView({ behavior: "smooth", block: "start" }); };
  return (
    <div className="docviewer" role="dialog" aria-label={c.doc_title}>
      <div className="dv-head">
        <FileText size={15} /><div className="dv-t"><b>{c.doc_title || d?.title}</b><span>{c.issuer || d?.issuer}</span></div>
        {d && <span className="dv-nav">
          <button className="icon-btn" aria-label="Previous page" disabled={page <= 1} onClick={() => go(page - 1)}><ChevronLeft size={16} /></button>
          <span className="num">Page {page} of {d.pages}</span>
          <button className="icon-btn" aria-label="Next page" disabled={page >= d.pages} onClick={() => go(page + 1)}><ChevronRight size={16} /></button>
        </span>}
        <button className="icon-btn" aria-label="Close document" onClick={onClose}><X size={16} /></button>
      </div>
      <div className="dv-body" ref={box}>
        {d?.page_images.map((p) => {
          const s = w / p.width, marks = p.page === c.page ? marked : [];
          return (
            <div key={p.page} id={`dv-${p.page}`} className="dv-page" style={{ width: w, height: p.height * s }}>
              <img src={api.pageUrl(c.doc_id, p.page)} alt={`Page ${p.page}`} loading={Math.abs(p.page - c.page) <= 1 ? "eager" : "lazy"} />
              {marks.map((r, i) => <span key={i} className="mark" style={{ left: r[0] * s - 2, top: r[1] * s - 1, width: (r[2] - r[0]) * s + 4, height: (r[3] - r[1]) * s + 2 }} />)}
            </div>
          );
        })}
        {!d && <p className="muted">Opening the document…</p>}
      </div>
    </div>
  );
}
