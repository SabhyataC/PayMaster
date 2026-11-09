/* Investigation report: a document-style page for one invoice, with export (print/PDF, JSON, Markdown).
   Built only from GET /report/invoice/{id}. Every server string goes through textContent. */
(function () {
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
    const NS = "http://www.w3.org/2000/svg";
    const svg = (tag, a) => { const e = document.createElementNS(NS, tag); for (const k in (a || {})) e.setAttribute(k, a[k]); return e; };
    const STAMP = { held: ["On hold", "warn"], approved: ["Approved", "good"], paid: ["Paid", "good"], rejected: ["Rejected", "bad"], cancelled: ["Cancelled", "bad"], received: ["Received", "mute"], needs_review: ["Needs a fix", "warn"] };
    const TOOL = { get_po: "Looked up the purchase order", get_deliveries: "Checked deliveries", get_vendor_bank_history: "Checked payout-email history", find_similar_invoices: "Searched for similar invoices", search_vendors: "Searched for look-alike vendor names", find_similar_precedents: "Searched case memory" };
    const P = {
        get_po: ["M9 2h6a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z", "M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2", "M12 11h4", "M12 16h4", "M8 11h.01", "M8 16h.01"],
        get_deliveries: ["m7.5 4.27 9 5.15", "M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z", "m3.3 7 8.7 5 8.7-5", "M12 22V12"],
        get_vendor_bank_history: ["M3 22h18", "M6 18v-7", "M10 18v-7", "M14 18v-7", "M18 18v-7", "M12 2 20 7H4Z"],
        find_similar_invoices: ["M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1Z", "M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8", "M12 17.5v-11"],
        search_vendors: ["m21 21-4.34-4.34", "M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0z"],
        find_similar_precedents: ["M11 20H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H20a2 2 0 0 1 2 2v3", "M20 17a3 3 0 1 1-6 0 3 3 0 0 1 6 0z", "m21 21-1.9-1.9"],
        code_check: ["m3 17 2 2 4-4", "m3 7 2 2 4-4", "M13 6h8", "M13 12h8", "M13 18h8"],
        fraud_signal: ["M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z", "M4 22v-7"],
        ai: ["M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5", "M9 18h6", "M10 22h4"],
        gate: ["M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z", "m9 12 2 2 4-4"],
        human: ["M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2", "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0z"],
        tool: ["m21 21-4.34-4.34", "M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0z"],
        check: ["M20 6 9 17l-5-5"],
        x: ["M18 6 6 18", "m6 6 12 12"]
    };
    const svgIcon = key => {
        const s = svg("svg", { viewBox: "0 0 24 24", class: "i", "aria-hidden": "true" });
        (P[key] || P.tool).forEach(d => s.appendChild(svg("path", { d })));
        return s;
    };

    const CSS = `
.rp{max-width:none;margin:22px auto 40px}
.rp-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}.rp-bar .sp{flex:1}
.rp-bar button{padding:7px 12px;border:1px solid #cfcdc4;background:#fff;color:#1d1d1b;border-radius:8px;font:inherit;font-size:13px;cursor:pointer}
.rp-bar button:hover{border-color:#0b6b4f;color:#0b6b4f}.rp-bar .pri{background:#0b6b4f;border-color:#0b6b4f;color:#fff}.rp-bar .pri:hover{color:#fff;background:#095a42}
.rp-paper{background:#fff;border:1px solid #e4e2da;border-radius:14px;padding:28px 32px;box-shadow:0 2px 14px rgba(0,0,0,.05)}
.rp-eyebrow{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#6b6a63;border-bottom:2px solid #1d1d1b;padding-bottom:8px}
.rp-title{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;margin:16px 0 4px}
.rp-title h1{font-size:24px;margin:0}.rp-title .sub{color:#6b6a63;margin-top:2px}
.rp-stamp{border:3px solid;border-radius:10px;padding:6px 16px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;transform:rotate(-3deg);font-size:15px}
.rp-stamp.good{color:#0b6b4f;border-color:#0b6b4f}.rp-stamp.bad{color:#b3261e;border-color:#b3261e}.rp-stamp.warn{color:#c77700;border-color:#c77700}.rp-stamp.mute{color:#6b6a63;border-color:#9a9a92}
.rp h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:#4a4a45;margin:26px 0 10px;display:flex;align-items:center;gap:8px}.rp h2::after{content:"";flex:1;height:1px;background:#e4e2da}
.rp-bottom{border-left:5px solid #1f4e8c;background:#eef3fa;border-radius:0 10px 10px 0;padding:12px 16px;margin-top:14px}.rp-bottom.bad{border-color:#b3261e;background:#fbecea}.rp-bottom.warn{border-color:#c77700;background:#fdf3e1}.rp-bottom.good{border-color:#0b6b4f;background:#e8f4ef}
.rp-bottom b{display:block;margin-bottom:2px}
.rp-score{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px}@media(max-width:700px){.rp-score{grid-template-columns:repeat(2,1fr)}.rp-paper{padding:18px 14px}}
.rp-sc{border:1px solid #e4e2da;border-radius:10px;padding:10px 12px}.rp-sc .t{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#6b6a63}.rp-sc .v{font-size:22px;font-weight:700;margin:2px 0}.rp-sc .h{font-size:12px;color:#6b6a63}
.seg{display:flex;gap:3px;margin-top:6px}.seg i{flex:1;height:8px;border-radius:3px;background:#34a37f}.seg i.f{background:#b3261e}
.chip{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:99px;background:#eeede8;color:#4a4a45}.chip.good{background:#e1f2ea;color:#0b6b4f}.chip.bad{background:#fbe3e0;color:#b3261e}.chip.warn{background:#fdecc8;color:#8a5200}.chip.ai{background:#fdecc8;color:#8a5200}.chip.human{background:#e3ecf8;color:#1f4e8c}
.rp-flow{display:grid;grid-template-columns:repeat(5,1fr);gap:0;position:relative}@media(max-width:700px){.rp-flow{grid-template-columns:1fr}}
.rp-st{position:relative;text-align:center;padding:0 6px}
.rp-st .dot{width:40px;height:40px;border-radius:50%;margin:0 auto 6px;display:grid;place-items:center;font-size:18px;border:3px solid #cfcdc4;background:#fff;position:relative;z-index:1}
.rp-st.good .dot{border-color:#0b6b4f;background:#e8f4ef}.rp-st.bad .dot{border-color:#b3261e;background:#fbecea}.rp-st.warn .dot{border-color:#c77700;background:#fdf3e1}
.rp-st:not(:last-child)::after{content:"";position:absolute;top:19px;left:calc(50% + 22px);right:calc(-50% + 22px);height:3px;background:#e4e2da}
@media(max-width:700px){.rp-st:not(:last-child)::after{display:none}.rp-st{text-align:left;display:flex;gap:10px;align-items:center;margin-bottom:8px}.rp-st .dot{margin:0}}
.rp-st .n{font-weight:700;font-size:13px}.rp-st .d{font-size:12px;color:#6b6a63}
.rp-tools{display:flex;gap:10px;align-items:center;font-size:13px;margin-bottom:8px;color:#4a4a45}
.rp-f{display:grid;grid-template-columns:28px 1fr;gap:10px;padding:10px 0;border-top:1px solid #eeede8}.rp-f:first-of-type{border-top:0}
.rp-f .ic{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:13px;color:#fff;background:#0b6b4f}.rp-f.no .ic{background:#b3261e}
.rp-f .tx{font-weight:600}.rp-f.untrusted .tx{font-style:italic;font-weight:500}.rp-f .meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px}.rp-f .det{font-size:12px;color:#6b6a63;margin-top:3px}
.rid{font:11px ui-monospace,Consolas,monospace;border:1px solid #e4e2da;background:#faf9f5;border-radius:6px;padding:1px 6px;cursor:pointer;color:#4a4a45}.rid:hover{border-color:#1f4e8c;color:#1f4e8c}
.rp-tl{position:relative;margin-left:14px;border-left:3px solid #e4e2da;padding-left:18px}
.rp-ev{position:relative;margin-bottom:14px}.rp-ev::before{content:attr(data-n);position:absolute;left:-33px;top:0;width:24px;height:24px;border-radius:50%;background:#1f4e8c;color:#fff;font-size:12px;font-weight:700;display:grid;place-items:center}
.rp-ev .a{font-weight:600}.rp-ev .b{font-size:13px;color:#4a4a45;margin:2px 0 4px}
.rp-cases{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}.rp-case{border:1px solid #e4e2da;border-radius:10px;padding:10px 12px}
.rp-case .q{font-style:italic;color:#4a4a45;background:#faf9f5;border-left:3px solid #cfcdc4;padding:5px 8px;margin:6px 0;font-size:13px;word-break:break-word}
.rp-tbl{width:100%;border-collapse:collapse;font-size:12px}.rp-tbl th{text-align:left;color:#6b6a63;font-weight:600;padding:4px 8px 4px 0;border-bottom:1px solid #cfcdc4}.rp-tbl td{padding:4px 8px 4px 0;border-bottom:1px solid #eeede8;word-break:break-all}
.rp-note{font-size:12px;color:#6b6a63;margin-top:6px}.rp-foot{margin-top:24px;border-top:1px solid #e4e2da;padding-top:10px;font-size:11px;color:#6b6a63}
.rp-empty{color:#6b6a63;font-size:13px;padding:6px 0}
@media print{
 body{background:#fff!important}header,#out,#f,.rp-bar,.noprint,.pmui-toasts{display:none!important}
 main{max-width:none!important;padding:0!important}.rp{margin:0;max-width:none}.rp-paper{border:0;box-shadow:none;padding:0}
 .rp-f,.rp-case,.rp-ev,.rp-sc,.rp-st{break-inside:avoid}.rp h2{break-after:avoid}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}
 .rp svg.i{width:1.15em;height:1.15em;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;vertical-align:-.2em}
.rp-st .dot svg.i{width:20px;height:20px}
.rp-st.good .dot{color:#0b6b4f}.rp-st.bad .dot{color:#b3261e}.rp-st.warn .dot{color:#c77700}
.rp-f .ic svg.i{width:15px;height:15px;stroke-width:2.5}`;

    function idChip(r) {
        const b = el("button", "rid", `${r.type.replace(/_/g, " ")} ${String(r.id).length > 10 ? String(r.id).slice(0, 8) : r.id}`);
        b.type = "button"; b.title = `${r.type} ${r.id} (click to copy)`;
        b.addEventListener("click", async () => {
            try { await navigator.clipboard.writeText(String(r.id)); if (window.PM && PM.toast) PM.toast(`Copied ${r.type} id`, "ok"); }
            catch (e) { if (window.PM && PM.toast) PM.toast(String(r.id), "info"); }
        });
        return b;
    }
    function download(name, text, type) {
        const a = el("a"); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    }
    function markdown(r) {
        const L = [`# Investigation report: invoice ${r.invoice_number}`, "", `Vendor: ${r.vendor}  `, `Amount: ${r.amount} ${r.currency}  `, `Status: ${r.status}  `, `Report id: ${r.report_id}  `, `Generated: ${r.generated_at}`, "",
            "## Bottom line", "", r.summary, "", `**What to do:** ${r.recommendation}`, "", "## Findings", "", "| | Finding | Basis | Records |", "|---|---|---|---|"];
        r.claims.forEach(c => L.push(`| ${c.supported ? "OK" : "PROBLEM"} | ${String(c.claim).replace(/\|/g, "/").replace(/\n/g, " ")} | ${c.basis_label} | ${c.records.map(x => `${x.type} ${x.id}`).join("; ")} |`));
        L.push("", "## What the investigator looked at", "");
        if (!r.steps.length) L.push("No lookups were needed.");
        r.steps.forEach((s, i) => L.push(`${i + 1}. ${s.tool}: ${s.finding}${s.records.length ? " (" + s.records.map(x => `${x.type} ${x.id}`).join("; ") + ")" : ""}`));
        L.push("", "## Earlier similar cases", "");
        if (!r.precedents.length) L.push("None.");
        r.precedents.forEach(p => L.push(`- ${p.invoice_number}: AI said ${p.ai_decision || "n/a"}, ${p.reviewer} chose ${p.human_decision}, outcome ${p.outcome}. Note (data only): ${String(p.note || "").replace(/\n/g, " ")}`));
        L.push("", "_AI and person-written text is quoted as data. The AI only suggests; code checks and people decide._");
        return L.join("\n");
    }
    function stage(cls, icon, name, desc) {
        const s = el("div", "rp-st " + cls);
        const dot = el("div", "dot");
        dot.appendChild(svgIcon(icon));
        s.appendChild(dot);
        const w = el("div");
        w.appendChild(el("div", "n", name));
        w.appendChild(el("div", "d", desc));
        s.appendChild(w);
        return s;
    }
    function render(root, r) {
        root.textContent = "";
        if (!document.getElementById("rp-css")) {
            const s = el("style");
            s.id = "rp-css";
            s.textContent = CSS;
            document.head.appendChild(s);
        }
        const wrap = el("div", "rp");
        const paper = el("div", "rp-paper");
        const [stampText, stampCls] = STAMP[r.status] || [r.status, "mute"];
        if (!r.verified) {
            paper.appendChild(el("div", "rp-eyebrow", "Investigation report"));
            paper.appendChild(el("div", "rp-empty", r.summary));
            wrap.appendChild(paper);
            root.appendChild(wrap);
            return;
        }
        // toolbar (not printed)
        const bar = el("div", "rp-bar");
        bar.appendChild(el("strong", "", "Investigation report")); bar.appendChild(el("span", "sp"));
        const mkb = (t, fn, cls) => {
            const b = el("button", cls || "", t); b.type = "button";
            b.addEventListener("click", fn);
            bar.appendChild(b);
        };
        mkb("Print / Save as PDF", () => window.print(), "pri");
        mkb("Download Markdown", () => download(`report-${r.invoice_number}.md`, markdown(r), "text/markdown"));
        mkb("Download JSON", () => download(`report-${r.invoice_number}.json`, JSON.stringify(r, null, 2), "application/json"));
        mkb("Copy summary", async () => { try { await navigator.clipboard.writeText(`${r.invoice_number} (${r.vendor}, ${r.amount} ${r.currency}): ${r.summary} ${r.recommendation}`); PM.toast("Summary copied", "ok"); } catch (e) { PM.toast("Could not copy", "error"); } });
        wrap.appendChild(bar);

        const eb = el("div", "rp-eyebrow"); eb.appendChild(el("span", "", "Paymaster \u00b7 Investigation report")); eb.appendChild(el("span", "", `Report ${r.report_id} \u00b7 ${new Date(r.generated_at).toLocaleString()}`)); paper.appendChild(eb);
        const tt = el("div", "rp-title"); const tl = el("div");
        tl.appendChild(el("h1", "", `Invoice ${r.invoice_number}`)); tl.appendChild(el("div", "sub", `${r.vendor} \u00b7 ${r.amount} ${r.currency} \u00b7 checked ${new Date(r.verified_at).toLocaleString()}`));
        tt.appendChild(tl); tt.appendChild(el("div", "rp-stamp " + stampCls, stampText)); paper.appendChild(tt);
        const bl = el("div", "rp-bottom " + stampCls); bl.appendChild(el("b", "", "Bottom line")); bl.appendChild(el("div", "", r.summary));
        const act = el("div"); act.style.marginTop = "6px"; const ab = el("b", "", "What to do: "); ab.style.display = "inline"; act.appendChild(ab); act.appendChild(document.createTextNode(r.recommendation)); bl.appendChild(act);
        paper.appendChild(bl);

        // scorecards
        const sc = el("div", "rp-score"), S = r.stages;
        const card = (t, v, h, extra) => { const c = el("div", "rp-sc"); c.appendChild(el("div", "t", t)); c.appendChild(el("div", "v", v)); c.appendChild(el("div", "h", h)); if (extra) c.appendChild(extra); sc.appendChild(c); };
        const seg = el("div", "seg"); const codeClaims = r.claims.filter(c => c.basis === "code_check"); codeClaims.forEach(c => { const i = el("i", c.supported ? "" : "f"); i.title = c.claim; seg.appendChild(i); });
        card("Code checks", `${S.code.passed}/${S.code.total}`, S.code.failed ? `${S.code.failed} failed` : "all passed", seg);
        card("Fraud risk", r.fraud ? r.fraud.risk : "not run", r.fraud ? `${S.fraud.signals} signal${S.fraud.signals === 1 ? "" : "s"} fired` : "no fraud scan on record");
        card("AI suggested", r.ai.suggested || "n/a", r.ai.overruled_by_code ? "overruled by code" : r.ai.model ? r.ai.model : "advice only");
        card("Person decided", S.human.decision || "not yet", S.human.reviewer ? `by ${S.human.reviewer}` : "waiting");
        paper.appendChild(sc);

        // pipeline
        paper.appendChild(el("h2", "", "How the decision was reached"));
        const fl = el("div", "rp-flow");
        fl.appendChild(stage(S.code.failed ? "bad" : "good", "code_check", "Code checks", `${S.code.passed} of ${S.code.total} passed`));
        fl.appendChild(stage(!r.fraud ? "" : r.fraud.risk === "high" ? "bad" : r.fraud.risk === "medium" ? "warn" : "good", "fraud_signal", "Fraud signals", r.fraud ? `${r.fraud.risk} risk, ${S.fraud.signals} signal(s)` : "not run"));
        fl.appendChild(stage(S.ai.ran ? "warn" : "", "ai", "AI suggestion", S.ai.suggested ? `suggested ${S.ai.suggested}` : "none"));
        fl.appendChild(stage(S.gate.overruled ? "bad" : "good", "gate", "Safety gates", S.gate.overruled ? "overruled the AI" : "no override needed"));
        fl.appendChild(stage(S.human.decision ? (S.human.decision === "approve" ? "good" : "bad") : "", "human", "A person", S.human.decision ? `${S.human.reviewer}: ${S.human.decision}` : "not decided yet"));
        paper.appendChild(fl);

        // findings
        paper.appendChild(el("h2", "", `Findings (${r.counts.supported} fine, ${r.counts.problems} problem${r.counts.problems === 1 ? "" : "s"})`));
        const tools = el("div", "rp-tools"); const cb = el("input"); cb.type = "checkbox"; cb.id = "rp-onlyp"; const lb = el("label", "", " Show only problems"); lb.htmlFor = "rp-onlyp";
        tools.appendChild(cb); tools.appendChild(lb); paper.appendChild(tools);
        const list = el("div");
        [...r.claims].sort((a, b) => Number(a.supported) - Number(b.supported)).forEach(c => {
            const f = el("div", "rp-f " + (c.supported ? "yes" : "no") + (c.untrusted ? " untrusted" : "")); f.dataset.ok = c.supported ? "1" : "0";
            const ic = el("div", "ic"); ic.appendChild(svgIcon(c.supported ? "check" : "x")); f.appendChild(ic);
            const b = el("div"); b.appendChild(el("div", "tx", c.claim));
            if (c.detail) b.appendChild(el("div", "det", c.detail));
            const m = el("div", "meta");
            const chip = el("span", "chip " + (c.basis === "ai" ? "ai" : c.basis === "human" ? "human" : c.basis === "gate" ? "bad" : ""));
            chip.appendChild(svgIcon(c.basis)); chip.appendChild(document.createTextNode(" " + c.basis_label)); m.appendChild(chip);
            if (c.untrusted) m.appendChild(el("span", "chip", "quoted as written, not trusted"));
            c.records.forEach(x => m.appendChild(idChip(x))); b.appendChild(m); f.appendChild(b); list.appendChild(f);
        });
        cb.addEventListener("change", () => list.querySelectorAll(".rp-f").forEach(f => { f.style.display = cb.checked && f.dataset.ok === "1" ? "none" : ""; }));
        paper.appendChild(list);

        // investigator
        paper.appendChild(el("h2", "", "What the investigator looked at"));
        if (!r.steps.length) paper.appendChild(el("div", "rp-empty", "The investigator made no lookups for this invoice."));
        else {
            const tl2 = el("div", "rp-tl");
            r.steps.forEach((st, i) => {
                const e = el("div", "rp-ev"); e.dataset.n = String(i + 1);
                const a = el("div", "a"); a.appendChild(svgIcon(st.tool)); a.appendChild(document.createTextNode(" " + (TOOL[st.tool] || st.tool))); e.appendChild(a);
                e.appendChild(el("div", "b", st.finding));
                const m = el("div", "meta");
                st.records.forEach(x => m.appendChild(idChip(x))); e.appendChild(m);
                tl2.appendChild(e);
            });
            paper.appendChild(tl2);
        }

        // case memory
        paper.appendChild(el("h2", "", "Earlier similar cases"));
        if (!r.precedents.length) paper.appendChild(el("div", "rp-empty", "No earlier human decision for this vendor with the same signals."));
        else {
            const g = el("div", "rp-cases");
            r.precedents.forEach(p => {
                const c = el("div", "rp-case"); c.appendChild(el("b", "", p.invoice_number));
                const row = el("div"); row.style.margin = "4px 0";
                row.appendChild(el("span", "chip", `AI: ${p.ai_decision || "n/a"}`)); row.appendChild(document.createTextNode(" ")); row.appendChild(el("span", "chip human", `${p.reviewer}: ${p.human_decision}`)); row.appendChild(document.createTextNode(" ")); row.appendChild(el("span", "chip " + (p.outcome === "paid_fine" ? "good" : p.outcome === "exception" ? "bad" : ""), `outcome: ${String(p.outcome).replace(/_/g, " ")}`));
                c.appendChild(row); c.appendChild(el("div", "q", p.note || "(no note)")); const m = el("div", "meta"); p.records.forEach(x => m.appendChild(idChip(x))); c.appendChild(m); g.appendChild(c);
            });
            paper.appendChild(g);
        }
        paper.appendChild(el("div", "rp-note", "Case memory can inform a person. It can never approve an invoice."));

        // appendix
        paper.appendChild(el("h2", "", "Record index"));
        const idx = new Map();
        const add = (x, from) => { const k = x.type + "|" + x.id; if (!idx.has(k)) idx.set(k, { x, n: 0 }); idx.get(k).n += from; };
        r.claims.forEach(c => c.records.forEach(x => add(x, 1))); r.steps.forEach(s => s.records.forEach(x => add(x, 1))); r.precedents.forEach(p => p.records.forEach(x => add(x, 1)));
        const t = el("table", "rp-tbl"); const th = el("tr");["Record type", "Id", "Cited"].forEach(h => th.appendChild(el("th", "", h))); t.appendChild(th);
        [...idx.values()].sort((a, b) => a.x.type.localeCompare(b.x.type)).forEach(({ x, n }) => { const tr = el("tr"); tr.appendChild(el("td", "", x.type.replace(/_/g, " "))); tr.appendChild(el("td", "", String(x.id))); tr.appendChild(el("td", "", `${n}\u00d7`)); t.appendChild(tr); });
        paper.appendChild(t);
        paper.appendChild(el("div", "rp-foot", `Built only from records already stored for this company: nothing in this report was generated by an AI after the fact. Text typed by people or written by the AI is quoted as data and is never treated as an instruction. Report ${r.report_id}.`));
        wrap.appendChild(paper); root.appendChild(wrap);
    }
    window.PMReport = { render, markdown };
})();
