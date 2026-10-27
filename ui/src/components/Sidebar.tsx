import { ChevronRight, Folder, FolderOpen, LayoutTemplate, MoreHorizontal, RotateCcw, SquarePen } from "lucide-react";
import { useEffect, useState } from "react";
import type { DealRow, ThreadRow } from "../api";

export function Sidebar({ deals, threads, dealId, sid, templatesOpen, onDeal, onThread, onNew, onTemplates, onFresh, email }: {
  deals: DealRow[]; threads: ThreadRow[]; dealId: string | null; sid: string | null; templatesOpen: boolean;
  onDeal: (id: string) => void; onThread: (deal: string, id: string) => void; onNew: () => void; onTemplates: () => void;
  onFresh: (d: DealRow) => void; email?: string;
}) {
  // a deal's menu (⋯): Start fresh, for running the demo again
  const [menu, setMenu] = useState<string | null>(null);
  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent | KeyboardEvent) => { if (e instanceof KeyboardEvent ? e.key === "Escape" : !(e.target as HTMLElement).closest(".proj-menu")) setMenu(null); };
    window.addEventListener("mousedown", close); window.addEventListener("keydown", close);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", close); };
  }, [menu]);
  const current = deals.filter((d) => !d.earlier), earlier = deals.filter((d) => d.earlier);
  const [showEarlier, setShowEarlier] = useState(false), openEarlier = showEarlier || earlier.some((d) => d.deal_id === dealId);
  const project = (d: DealRow, tag: string) => {
    const on = d.deal_id === dealId && !templatesOpen, mine = threads.filter((t) => t.deal_id === d.deal_id);
    return (
      <div key={d.deal_id} className={`proj ${on ? "on" : ""}`}>
        <button className="proj-row" onClick={() => onDeal(d.deal_id)} aria-expanded={on}>
          {on ? <FolderOpen size={15} /> : <Folder size={15} />}
          <span className="proj-name">{d.deal_name}</span>
          <span className="proj-stage">{tag}</span>
        </button>
        {!d.earlier && (
          <div className={`proj-menu ${menu === d.deal_id ? "open" : ""}`}>
            <button className="proj-more" onClick={() => setMenu(menu === d.deal_id ? null : d.deal_id)} aria-label={`${d.deal_name} options`} aria-haspopup="menu" aria-expanded={menu === d.deal_id}><MoreHorizontal size={15} /></button>
            {menu === d.deal_id && (
              <div className="menu" role="menu">
                <button role="menuitem" onClick={() => { setMenu(null); onFresh(d); }}><RotateCcw size={13} /> Start fresh…</button>
              </div>
            )}
          </div>
        )}
        {mine.map((t) => (
          <button key={t.id} className={`chat-row ${t.id === sid && !templatesOpen ? "on" : ""}`} onClick={() => onThread(d.deal_id, t.id)}>
            <span className="t">{t.title}</span>{t.status === "running" && <span className="spin" aria-label="Working" />}
          </button>
        ))}
        {!mine.length && on && <div className="chat-row empty">No chats yet</div>}
      </div>
    );
  };
  return (
    <nav className="side" aria-label="Deals and chats">
      <div className="side-top">
        <img className="logo" src="/studio-logo.svg" alt="Quillwood Credit Partners" />
        <span className="product">IC Paper Studio</span>
      </div>
      <button className={`side-new ${templatesOpen ? "on" : ""}`} onClick={onTemplates} aria-pressed={templatesOpen}><LayoutTemplate size={15} /> Templates</button>
      <button className="side-new" onClick={onNew} disabled={!dealId}><SquarePen size={15} /> New chat</button>
      <div className="side-scroll">
        <div className="side-label">Deals</div>
        {current.map((d) => project(d, d.sub_sector))}
        {earlier.length > 0 && (
          <>
            <button className={`side-label side-toggle ${openEarlier ? "open" : ""}`} onClick={() => setShowEarlier(!openEarlier)} aria-expanded={openEarlier}>
              <ChevronRight size={13} /> Earlier work
            </button>
            {openEarlier && earlier.map((d) => project(d, d.updated ? shortDay(d.updated) : ""))}
          </>
        )}
      </div>
      <div className="side-foot">{email}</div>
    </nav>
  );
}

function shortDay(epoch: number) {
  return new Date(epoch * 1000).toLocaleDateString("en-AU", { day: "numeric", month: "short" });
}
