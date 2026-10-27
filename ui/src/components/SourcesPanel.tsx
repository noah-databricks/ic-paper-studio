import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, FilePlus2, FileText, Table2 } from "lucide-react";
import { api, DOC_TYPE, shortDate, viewLabel, type Citation, type DealDetail, type FileJob, type Paper, type PdfCitation, type TableSource } from "../api";
import { docAt, SourceCard, TableCard } from "./Source";

type Ref = { c: Citation; n: number; tag: string; slot: string; text?: string; edited?: { text: string; original?: string } };
type Group = { key: string; kind: "pdf" | "metric"; title: string; sub: string; refs: Ref[]; superseded?: boolean; doc?: string; table?: TableSource };

/** Every source of the paper once (a document or a metric view), with the paper's references to it underneath; then
 * what the deal's chats looked at without citing it (documents read, the deal record, metric views queried). */
export function SourcesPanel({ paper, nums, deal, dealId, sel, hover, focus, onPick, onHover, onGoto, onOpenDoc, jobs, onFilesChanged, workspace, width }: {
  paper: Paper | null; nums: Map<string, number>; deal: DealDetail | null; dealId: string; sel: { id: string } | null; hover: string | null;
  focus: { key: string; at: number } | null;
  onPick: (c: Citation, text?: string) => void; onHover: (id: string | null) => void; onGoto: (tag: string) => void; onOpenDoc: (c: PdfCitation) => void;
  jobs: FileJob[]; onFilesChanged: () => void; workspace: string; width: number;
}) {
  const [closed, setClosed] = useState<Set<string>>(new Set()), [err, setErr] = useState<string | null>(null), [looking, setLooking] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const groups = useMemo(() => {
    const refs: Ref[] = [];
    for (const spec of paper?.template?.slots ?? []) {
      const s = paper?.slots[spec.tag];
      if (!s || s.status === "empty") continue;
      const segs = (s.paragraphs ?? []).flatMap((p) => p.segments).filter((x) => x.citation);
      const texts = Object.fromEntries(segs.map((x) => [x.citation!, x.text]));
      const edits = Object.fromEntries(segs.filter((x) => x.mode === "analyst_edited").map((x) => [x.citation!, { text: x.text, original: x.original }]));
      for (const c of s.citations ?? []) {
        const n = nums.get(c.id);
        if (n) refs.push({ c, n, tag: spec.tag, slot: spec.alias || spec.tag.replace(/_/g, " "), text: texts[c.id] ?? (s.kind === "metric" ? s.text : undefined), edited: edits[c.id] });
      }
    }
    refs.sort((a, b) => a.n - b.n);
    const byKey = new Map<string, Group>();
    for (const r of refs) {
      const key = r.c.kind === "pdf" ? `doc:${r.c.doc_id}` : `mv:${r.c.view}`;
      if (!byKey.has(key)) byKey.set(key, r.c.kind === "pdf"
        ? { key, kind: "pdf", title: r.c.doc_title, sub: [DOC_TYPE[r.c.doc_type ?? ""], r.c.issuer, shortDate(r.c.as_of)].filter(Boolean).join(" · "), refs: [], superseded: !!r.c.superseded_by }
        : { key, kind: "metric", title: viewLabel(r.c), sub: "Governed figures", refs: [] });
      byKey.get(key)!.refs.push(r);
    }
    const cited = [...byKey.values()];
    // documents the chats read or the analyst attached that nothing in the paper cites yet
    const uncited: Group[] = [
      ...(deal?.tables ?? []).filter((t) => !byKey.has(t.key))
        .map((t): Group => ({ key: t.key, kind: "metric", title: t.title, refs: [], table: t,
                              sub: `${t.sub} · ${t.kind === "record" ? "read" : `queried${t.queries.length > 1 ? ` ${t.queries.length} times` : ""}`}` })),
      ...(deal?.documents ?? []).filter((d) => !byKey.has(`doc:${d.doc_id}`))
        .map((d): Group => ({ key: `doc:${d.doc_id}`, kind: "pdf", title: d.title, refs: [], superseded: !!d.superseded_by, doc: d.doc_id,
                              sub: [d.how.includes("attached") ? "Attached" : DOC_TYPE[d.doc_type ?? ""], d.issuer, shortDate(d.as_of)].filter(Boolean).join(" · ") })),
    ];
    return { cited, uncited };
  }, [paper, nums, deal]);
  const add = async (files: FileList | null) => {
    setErr(null);
    for (const f of Array.from(files ?? [])) { try { await api.addFile(dealId, f); } catch (e) { setErr((e as Error).message); } }
    onFilesChanged();
  };
  const toggle = (k: string) => setClosed((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; });
  // the chat asked to show a source: open it here and bring it into view
  useEffect(() => {
    if (!focus) return;
    setClosed((s) => { const n = new Set(s); n.delete(focus.key); return n; });
    if (groups.uncited.some((g) => g.key === focus.key)) setLooking(focus.key);
    window.setTimeout(() => document.getElementById(`src-${focus.key}`)?.scrollIntoView({ behavior: "smooth", block: "start" }), 40);
  }, [focus]); // eslint-disable-line react-hooks/exhaustive-deps
  const total = groups.cited.reduce((a, g) => a + g.refs.length, 0);
  return (
    <aside className="sources" aria-label="Sources">
      <div className="src-head"><h3>Sources</h3><span className="dim">{total ? `${total} references` : "Nothing cited yet"}</span></div>
      <div className="src-scroll">
        {groups.cited.length > 0 && <div className="src-label first">Cited in the paper</div>}
        {groups.cited.map((g) => {
          const open = !closed.has(g.key);
          return (
            <section key={g.key} id={`src-${g.key}`} className={`src-group ${focus?.key === g.key ? "focus" : ""}`}>
              <button className="src-title" onClick={() => toggle(g.key)} aria-expanded={open}>
                {g.kind === "pdf" ? <FileText size={14} /> : <Table2 size={14} />}
                <span className="t"><b>{g.title}</b><span>{g.sub} · {g.refs.length} reference{g.refs.length > 1 ? "s" : ""}</span>
                  {g.superseded && <span className="src-flag">A later version replaces this document</span>}</span>
                {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </button>
              {open && g.refs.map((r) => {
                const on = sel?.id === r.c.id;

                return (
                  <div key={r.c.id} className={`src-ref ${on ? "on" : ""} ${hover === r.c.id ? "hot" : ""}`}
                    onMouseEnter={() => onHover(r.c.id)} onMouseLeave={() => onHover(null)}>
                    <button className="src-ref-row" onClick={() => { onGoto(r.tag); onPick(r.c, r.text); }} aria-expanded={on}>
                      <span className="num">{r.n}</span>
                      <span className="t">
                        {r.c.kind === "pdf"
                          ? <q className="quote-line">{r.c.quote ?? (r.c.type === "table" ? "Table" : r.c.type === "figure" ? "Figure" : r.c.source_text)}</q>
                          : <span className="fig-line"><span>{r.c.measure}</span><b className="num">{r.c.formatted ?? r.c.value}</b></span>}
                        <span className="where">{r.edited ? "Edited by you · " : ""}In {r.slot.toLowerCase()}{r.c.kind === "pdf" ? ` · page ${r.c.page}` : ""}</span>
                      </span>
                    </button>
                    {on && <div className="src-preview"><SourceCard c={r.c} width={width - 44} workspace={workspace} onOpenDoc={onOpenDoc} edited={r.edited} /></div>}
                  </div>
                );
              })}
            </section>
          );
        })}
        {groups.uncited.length > 0 && <div className="src-label">Looked at, not cited</div>}
        {groups.uncited.map((g) => {
          const open = looking === g.key;
          return (
            <section key={g.key} id={`src-${g.key}`} className={`src-group uncited ${focus?.key === g.key ? "focus" : ""}`}>
              <button className="src-title" aria-expanded={g.table ? open : undefined}
                onClick={() => g.doc ? onOpenDoc(docAt(g.doc, 1, "", g.title)) : setLooking(open ? null : g.key)}>
                {g.kind === "pdf" ? <FileText size={14} /> : <Table2 size={14} />}
                <span className="t"><b>{g.title}</b><span>{g.sub}</span>{g.superseded && <span className="src-flag">Superseded</span>}</span>
                {g.table ? (open ? <ChevronDown size={14} /> : <ChevronRight size={14} />) : null}
              </button>
              {open && g.table && <div className="src-preview"><TableCard t={g.table} workspace={workspace} /></div>}
            </section>
          );
        })}
        {!groups.cited.length && !groups.uncited.length && <p className="src-empty">Documents the agent reads or cites from the library show here.</p>}
        {jobs.filter((j) => j.state !== "ready").map((j) => <div key={j.id} className="src-job">{j.state === "parsing" ? <span className="spin" /> : null}<span>{j.name} {j.state === "failed" ? `(failed: ${j.error})` : "is being parsed"}</span></div>)}
        <button className="src-add" onClick={() => input.current?.click()}><FilePlus2 size={14} /> Add a file</button>
        <input ref={input} type="file" hidden multiple accept=".pdf,.docx,.png,.jpg,.jpeg" onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
        {err && <p className="err-line">{err}</p>}
      </div>
    </aside>
  );
}
