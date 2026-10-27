import { useEffect, useMemo, useState } from "react";
import { AlignLeft, ArrowLeft, FileUp, Image, Pencil, Plus, RotateCcw, Table, TextCursorInput, Trash2 } from "lucide-react";
import { api, type SlotSpec, type TemplateDetail as Detail, type TemplateRow } from "../api";
import { DocxPreview } from "./Paper";

const KIND = { text: AlignLeft, table: Table, image: Image, metric: TextCursorInput } as const;
const KIND_LABEL = { text: "Text", table: "Table", image: "Chart or figure", metric: "Single value" } as const;
const FIXED = "Cover"; // not a Word heading: its values can go, the section can't be renamed or removed

const title = (section: string) => section.replace(/^\d+\.\s+/, "");
function bySection(slots: SlotSpec[]) {
  const out: { name: string; slots: SlotSpec[] }[] = [];
  for (const s of slots) {
    if (!out.length || out[out.length - 1].name !== s.section) out.push({ name: s.section, slots: [] });
    out[out.length - 1].slots.push(s);
  }
  return out;
}

/** The firm's paper templates, picked the way Word shows its templates: a page thumbnail with what it is for. */
export function TemplatesView({ templates, onChanged }: { templates: TemplateRow[]; onChanged: () => Promise<void> }) {
  const [id, setId] = useState<string | null>(null), [adding, setAdding] = useState(false);
  if (adding) return <NewTemplate onCancel={() => setAdding(false)} onSaved={async (n) => { await onChanged(); setAdding(false); setId(n); }} />;
  if (!id) {
    const names = Object.fromEntries(templates.map((t) => [t.template_id, t.name]));
    return (
      <div className="tv-grid-wrap">
        <div className="tv-grid-head"><button className="btn primary small" onClick={() => setAdding(true)}><Plus size={14} /> New template</button></div>
        <div className="tv-grid">
          {templates.map((t) => (
            <button key={t.template_id} className="tv-card" onClick={() => setId(t.template_id)}>
              <span className="tv-thumb"><img src={api.templateThumbUrl(t.template_id)} alt={`First page of the ${t.name}`} /></span>
              <b>{t.name}</b>
              {t.custom && <span className="tv-made">{t.based_on ? `Made from the ${names[t.based_on] ?? t.based_on}` : "Uploaded Word document"}</span>}
              <span>{t.purpose}</span>
            </button>
          ))}
        </div>
      </div>
    );
  }
  return <TemplateDetail key={id} id={id} onBack={() => setId(null)} onSaved={async (n) => { await onChanged(); setId(n); }} />;
}

function TemplateDetail({ id, onBack, onSaved }: { id: string; onBack: () => void; onSaved: (id: string) => Promise<void> }) {
  const [detail, setDetail] = useState<Detail | null>(null), [editing, setEditing] = useState(false), [v, setV] = useState(0);
  useEffect(() => { api.templateDetail(id).then(setDetail).catch(() => setDetail(null)); }, [id, v]);
  const sections = useMemo(() => bySection(detail?.slots ?? []), [detail]);
  if (!detail) return <div className="tv-grid-wrap"><p className="muted">Opening the template…</p></div>;
  return (
    <div className="tv-body">
      {editing ? (
        <TemplateEditor detail={detail} onCancel={() => setEditing(false)}
          onSaved={async (n) => { setEditing(false); if (n === id) setV((x) => x + 1); else await onSaved(n); }} />
      ) : (
        <div className="tv-spec">
          <div className="tv-top">
            <button className="link tv-back" onClick={onBack}><ArrowLeft size={13} /> All templates</button>
            <button className="btn small" onClick={() => setEditing(true)}><Pencil size={13} /> {detail.custom ? "Edit" : "Edit a copy"}</button>
          </div>
          <h1>{detail.name}</h1>
          {detail.purpose && <p className="tv-purpose">{detail.purpose}</p>}
          {detail.use_when && <><div className="tv-label">Use when</div><p className="tv-when">{detail.use_when}</p></>}
          {sections.map((sec) => (
            <section key={sec.name} className="tv-sec">
              <h2>{sec.name}</h2>
              {sec.slots.map((s) => {
                const Ic = KIND[s.kind] ?? AlignLeft;
                return (
                  <div key={s.tag} className="tv-slot">
                    <Ic size={14} aria-label={KIND_LABEL[s.kind]} />
                    <div><b>{s.alias || s.tag.replace(/_/g, " ")}</b><span>{s.guidance}</span>
                      <span className="tv-src">{KIND_LABEL[s.kind]}</span></div>
                  </div>
                );
              })}
            </section>
          ))}
        </div>
      )}
      <div className="tv-doc"><DocxPreview url={api.templateFileUrl(detail.template_id, v)} version={v} /></div>
    </div>
  );
}

type SlotEdit = { alias: string; guidance: string; preferred_source: string };

/** Change a template: name and when to use it, section names, each slot's label, guidance and preferred source, and
 * which sections and slots it has. The firm's own templates are never changed; edits to them save as a new template. */
function TemplateEditor({ detail, onCancel, onSaved }: { detail: Detail; onCancel: () => void; onSaved: (id: string) => Promise<void> }) {
  const [name, setName] = useState(detail.custom ? detail.name : `${detail.name} (copy)`);
  const [purpose, setPurpose] = useState(detail.purpose ?? ""), [useWhen, setUseWhen] = useState(detail.use_when ?? "");
  const [secNames, setSecNames] = useState<Record<string, string>>({});
  const [slots, setSlots] = useState<Record<string, SlotEdit>>(
    () => Object.fromEntries(detail.slots.map((s) => [s.tag, { alias: s.alias ?? "", guidance: s.guidance ?? "", preferred_source: s.preferred_source ?? "" }])));
  const [removed, setRemoved] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false), [err, setErr] = useState<string | null>(null);
  const sections = useMemo(() => bySection(detail.slots), [detail]);
  const kept = detail.slots.length - removed.size;

  const toggle = (tags: string[], off: boolean) => setRemoved((r) => { const n = new Set(r); tags.forEach((t) => (off ? n.add(t) : n.delete(t))); return n; });
  const setSlot = (tag: string, k: keyof SlotEdit, val: string) => setSlots((s) => ({ ...s, [tag]: { ...s[tag], [k]: val } }));
  const save = async (asNew: boolean) => {
    setBusy(true); setErr(null);
    const changed = Object.fromEntries(detail.slots.filter((s) => !removed.has(s.tag)).map((s) => [s.tag, slots[s.tag]]));
    const edits = { remove: [...removed], sections: Object.fromEntries(Object.entries(secNames).filter(([k, v]) => v.trim() && v.trim() !== title(k))), slots: changed };
    try {
      const body = { base_id: detail.template_id, name, purpose, use_when: useWhen, edits };
      const r = asNew ? await api.createTemplate(body) : await api.updateTemplate(detail.template_id, body);
      await onSaved(r.template_id);
    } catch (e) { setErr((e as Error).message); setBusy(false); }
  };

  return (
    <div className="tv-spec tv-edit">
      <div className="tv-top">
        <button className="link tv-back" onClick={onCancel}><ArrowLeft size={13} /> Back without saving</button>
      </div>
      <p className="tv-note">{detail.custom ? "Changes apply to new drafts. " : "The firm's template stays as it is: this saves a new template. "}
        Removed slots come out of the Word file too. The agent can choose the template as soon as it's saved.</p>
      <label className="tv-field"><span className="tv-label">Name</span><input value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label className="tv-field"><span className="tv-label">What it is for</span><textarea rows={2} value={purpose} onChange={(e) => setPurpose(e.target.value)} /></label>
      <label className="tv-field"><span className="tv-label">Use when (the agent reads this to pick a template)</span><textarea rows={3} value={useWhen} onChange={(e) => setUseWhen(e.target.value)} /></label>

      {sections.map((sec) => {
        const tags = sec.slots.map((s) => s.tag), gone = tags.every((t) => removed.has(t)), fixed = sec.name === FIXED;
        return (
          <section key={sec.name} className={`tv-sec${gone ? " is-removed" : ""}`}>
            <div className="tv-sec-head">
              {fixed ? <h2>{sec.name}</h2> : (
                <input className="tv-sec-name" aria-label={`Name of section ${sec.name}`} disabled={gone}
                  value={secNames[sec.name] ?? title(sec.name)} onChange={(e) => setSecNames((n) => ({ ...n, [sec.name]: e.target.value }))} />
              )}
              {!fixed && (gone
                ? <button className="btn small ghost" onClick={() => toggle(tags, false)}><RotateCcw size={13} /> Restore section</button>
                : <button className="btn small ghost" onClick={() => toggle(tags, true)}><Trash2 size={13} /> Remove section</button>)}
            </div>
            {!gone && sec.slots.map((s) => {
              const Ic = KIND[s.kind] ?? AlignLeft, off = removed.has(s.tag), e = slots[s.tag];
              return (
                <div key={s.tag} className={`tv-slot tv-slot-edit${off ? " is-removed" : ""}`}>
                  <Ic size={14} aria-label={KIND_LABEL[s.kind]} />
                  {off ? (
                    <div className="tv-slot-off"><b>{e.alias || s.tag.replace(/_/g, " ")}</b><span>Removed</span>
                      <button className="link" onClick={() => toggle([s.tag], false)}><RotateCcw size={12} /> Restore</button></div>
                  ) : (
                    <div className="tv-slot-fields">
                      <div className="tv-slot-row">
                        <input aria-label="Label" value={e.alias} placeholder={s.tag.replace(/_/g, " ")} onChange={(x) => setSlot(s.tag, "alias", x.target.value)} />
                        <span className="tv-src">{KIND_LABEL[s.kind]}</span>
                        <button className="btn small ghost" aria-label={`Remove ${e.alias || s.tag}`} title="Remove slot" onClick={() => toggle([s.tag], true)}><Trash2 size={13} /></button>
                      </div>
                      <textarea aria-label="Guidance" rows={2} value={e.guidance} onChange={(x) => setSlot(s.tag, "guidance", x.target.value)} />
                      <label className="tv-pref"><span>Draw from</span>
                        <input aria-label="Preferred source" value={e.preferred_source} onChange={(x) => setSlot(s.tag, "preferred_source", x.target.value)} /></label>
                    </div>
                  )}
                </div>
              );
            })}
          </section>
        );
      })}

      <div className="tv-save">
        {err && <span className="tv-err" role="alert">{err}</span>}
        <span className="muted">{kept} of {detail.slots.length} slots</span>
        <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
        {detail.custom && <button className="btn" onClick={() => save(false)} disabled={busy || !kept}>Save changes</button>}
        <button className="btn primary" onClick={() => save(true)} disabled={busy || !kept || !name.trim()}>{busy ? "Saving…" : "Save as new template"}</button>
      </div>
    </div>
  );
}

/** A new template from the firm's own Word document: its headings become sections, and the text, tables and pictures
 * under each heading become slots the agent fills. */
function NewTemplate({ onCancel, onSaved }: { onCancel: () => void; onSaved: (id: string) => Promise<void> }) {
  const [file, setFile] = useState<File | null>(null), [name, setName] = useState(""), [purpose, setPurpose] = useState(""), [useWhen, setUseWhen] = useState("");
  const [busy, setBusy] = useState(false), [err, setErr] = useState<string | null>(null), [over, setOver] = useState(false);
  const pick = (f: File | undefined) => {
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".docx")) { setErr("Choose a Word document (.docx)."); return; }
    setErr(null); setFile(f); if (!name) setName(f.name.replace(/\.docx$/i, "").replace(/[_-]+/g, " "));
  };
  const save = async () => {
    if (!file) return;
    setBusy(true); setErr(null);
    try { const r = await api.uploadTemplate(file, { name, purpose, use_when: useWhen }); await onSaved(r.template_id); }
    catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  return (
    <div className="tv-grid-wrap">
      <div className="tv-new">
        <button className="link tv-back" onClick={onCancel}><ArrowLeft size={13} /> All templates</button>
        <h1>New template</h1>
        <p className="tv-note">Upload one of the firm's Word templates. Each heading becomes a section; the text, tables and pictures under it
          become slots the agent fills, and any instructions written there become the slot's guidance. Content controls already in the file are kept.
          You can review and edit the slots after it's uploaded. To change one of the existing templates instead, open it and choose Edit a copy.</p>
        <label className={`tv-drop${over ? " is-over" : ""}${file ? " has-file" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0]); }}>
          <input type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={(e) => pick(e.target.files?.[0])} />
          <FileUp size={22} />
          {file ? <><b>{file.name}</b><span>Choose a different file</span></> : <><b>Upload a Word document</b><span>Drop a .docx here, or click to choose one</span></>}
        </label>
        <div className="tv-edit">
          <label className="tv-field"><span className="tv-label">Name</span><input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Short form credit paper" /></label>
          <label className="tv-field"><span className="tv-label">What it is for</span><textarea rows={2} value={purpose} onChange={(e) => setPurpose(e.target.value)} /></label>
          <label className="tv-field"><span className="tv-label">Use when (the agent reads this to pick a template)</span><textarea rows={3} value={useWhen} onChange={(e) => setUseWhen(e.target.value)} /></label>
        </div>
        <div className="tv-new-actions">
          {err && <span className="tv-err" role="alert">{err}</span>}
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn primary" onClick={save} disabled={busy || !file || !name.trim()}>{busy ? "Uploading…" : "Upload template"}</button>
        </div>
      </div>
    </div>
  );
}
