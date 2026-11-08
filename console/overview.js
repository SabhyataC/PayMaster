/* Overview analytics: charts drawn with plain SVG, no libraries. Data from GET /analytics?days=N.
   Every label from the server goes through textContent / setAttribute, never innerHTML. */
(function () {
    const NS = "http://www.w3.org/2000/svg";
    const C = { good: "#0b6b4f", stop: "#b3261e", other: "#c9c7bd", blue: "#1f4e8c", amber: "#c77700", ink: "#1d1d1b", mute: "#6b6a63", line: "#e4e2da" };
    const REASON = {
        bank_details_changed: "Bank details changed", po_not_found: "No matching purchase order", po_belongs_to_other_vendor: "PO belongs to another vendor",
        delivery_not_received_yet: "Goods not received yet", amount_out_of_tolerance: "Amount over the PO", quantity_out_of_tolerance: "Quantity over the PO",
        duplicate_invoice: "Duplicate invoice", investigator_failed: "AI could not finish", fraud_gate: "Fraud gate", safety_gate: "Safety gate"
    };
    const STATUS = { received: "Received", approved: "Approved", held: "Waiting for a person", rejected: "Rejected", paid: "Paid", needs_review: "Needs a fix", cancelled: "Cancelled" };
    const SCOL = { approved: C.good, paid: "#2e9e7a", held: C.amber, rejected: C.stop, cancelled: "#8a2c26", received: "#7a8aa5", needs_review: "#a06bd0" };
    const human = k => REASON[k] || String(k).replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
    const svg = (tag, a, text) => { const e = document.createElementNS(NS, tag); for (const k in (a || {})) e.setAttribute(k, a[k]); if (text !== undefined) e.textContent = text; return e; };
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
    const nf = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
    const money = (v, cur) => `${cur || ""} ${Math.abs(v) >= 10000 ? nf.format(v) : Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`.trim();
    const dur = s => s == null ? "n/a" : s < 90 ? `${Math.round(s)} sec` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} days`;
    const dayLabel = iso => { const d = new Date(iso + "T00:00:00"); return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }); };

    const CSS = `
#analytics{margin:0 0 16px}
.an-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:6px 0 10px}.an-head h2{font-size:16px;margin:0;flex:1}
.an-range{display:flex;border:1px solid #e4e2da;border-radius:8px;overflow:hidden;background:#fff}.an-range button{border:0;background:#fff;padding:6px 12px;cursor:pointer;font:inherit;font-size:13px;color:#4a4a45}
.an-range button.on{background:#0b6b4f;color:#fff;font-weight:600}
.an-grid{display:grid;gap:14px;grid-template-columns:repeat(12,1fr)}
.an-card{background:#fff;border:1px solid #e4e2da;border-radius:12px;padding:14px 16px;min-width:0;grid-column:span 12}
.an-card h3{margin:0 0 2px;font-size:14px}.an-card .sub{font-size:12px;color:#6b6a63;margin:0 0 8px}
.an-k{grid-column:span 3}.an-8{grid-column:span 8}.an-4{grid-column:span 4}.an-6{grid-column:span 6}
@media(max-width:1100px){.an-k{grid-column:span 6}.an-8,.an-4,.an-6{grid-column:span 12}}@media(max-width:560px){.an-k{grid-column:span 12}}
.an-k .t{font-size:12px;color:#6b6a63;text-transform:uppercase;letter-spacing:.05em}.an-k .n{font-size:26px;font-weight:700;margin:2px 0}.an-k .h{font-size:12px;color:#6b6a63}
.an-k svg{display:block;width:100%;height:42px;margin-top:6px}
.an-k.bad{border-left:4px solid #b3261e}.an-k.good{border-left:4px solid #0b6b4f}.an-k.info{border-left:4px solid #1f4e8c}.an-k.warn{border-left:4px solid #c77700}
.an-svg{width:100%;height:auto;display:block}
.an-leg{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:#4a4a45;margin-top:6px}.an-leg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.an-bar{display:grid;grid-template-columns:minmax(110px,44%) 1fr auto;gap:8px;align-items:center;font-size:13px;margin:7px 0}
.an-bar .tr{height:10px;background:#f1f0ea;border-radius:99px;overflow:hidden}.an-bar .fl{height:100%;border-radius:99px}.an-bar .lb{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.an-empty{color:#6b6a63;font-size:13px;padding:18px 0;text-align:center}
.an-tip{position:fixed;z-index:9999;pointer-events:none;background:#0e1726;color:#fff;font-size:12px;padding:6px 9px;border-radius:7px;box-shadow:0 4px 14px rgba(0,0,0,.25);display:none;white-space:pre-line}
.an-ai{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;text-align:center}.an-ai b{display:block;font-size:24px}.an-ai span{font-size:12px;color:#6b6a63}
.an-ai>div{background:#faf9f5;border-radius:10px;padding:10px 6px}
.an-note{font-size:12px;color:#6b6a63;margin-top:8px}`;

    let tip;
    function showTip(ev, text) {
        if (!tip) { tip = el("div", "an-tip"); document.body.appendChild(tip); }
        tip.textContent = text; tip.style.display = "block";
        tip.style.left = Math.min(ev.clientX + 12, window.innerWidth - 190) + "px"; tip.style.top = (ev.clientY + 14) + "px";
    }
    const hideTip = () => { if (tip) tip.style.display = "none"; };
    const hover = (node, text) => { node.addEventListener("mousemove", e => showTip(e, text)); node.addEventListener("mouseleave", hideTip); };

    function spark(values, color) {
        const w = 200, h = 42, max = Math.max(...values, 1), n = values.length;
        const s = svg("svg", { viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none", "aria-hidden": "true" });
        const pts = values.map((v, i) => [n === 1 ? w / 2 : (i * w) / (n - 1), h - 4 - (v / max) * (h - 10)]);
        const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
        s.appendChild(svg("path", { d: `${line} L ${w} ${h} L 0 ${h} Z`, fill: color, opacity: ".12" }));
        s.appendChild(svg("path", { d: line, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
        return s;
    }
    function kpi(cls, title, big, hint, chart) {
        const c = el("div", "an-card an-k " + cls);
        c.appendChild(el("div", "t", title)); c.appendChild(el("div", "n", big)); c.appendChild(el("div", "h", hint));
        if (chart) c.appendChild(chart);
        return c;
    }
    function gauge(rate) {
        const s = svg("svg", { viewBox: "0 0 200 42", "aria-hidden": "true" });
        s.appendChild(svg("rect", { x: 0, y: 16, width: 200, height: 10, rx: 5, fill: "#f1f0ea" }));
        s.appendChild(svg("rect", { x: 0, y: 16, width: Math.max(0, Math.min(1, rate || 0)) * 200, height: 10, rx: 5, fill: C.blue }));
        return s;
    }
    function timeline(d) {
        const W = 760, H = 250, L = 34, B = 26, T = 8, R = 6, n = d.timeline.length;
        const max = Math.max(1, ...d.timeline.map(x => x.good + x.stopped + x.other));
        const nice = max <= 4 ? max : Math.ceil(max / 4) * 4;
        const s = svg("svg", { class: "an-svg", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Invoices received per day" });
        for (let i = 0; i <= 4; i++) {
            const v = (nice * i) / 4, y = H - B - (v / nice) * (H - B - T);
            s.appendChild(svg("line", { x1: L, x2: W - R, y1: y, y2: y, stroke: C.line, "stroke-width": 1 }));
            s.appendChild(svg("text", { x: L - 6, y: y + 4, "text-anchor": "end", "font-size": 11, fill: C.mute }, String(Math.round(v * 10) / 10)));
        }
        const bw = (W - L - R) / n, gap = Math.min(4, bw * 0.25);
        d.timeline.forEach((x, i) => {
            const gx = L + i * bw + gap / 2, w = Math.max(2, bw - gap);
            let y = H - B;
            const g = svg("g");
            [["good", C.good], ["stopped", C.stop], ["other", C.other]].forEach(([k, col]) => {
                const hh = (x[k] / nice) * (H - B - T); if (!hh) return;
                y -= hh; g.appendChild(svg("rect", { x: gx, y, width: w, height: hh, fill: col, rx: Math.min(3, w / 2) }));
            });
            g.appendChild(svg("rect", { x: gx, y: T, width: w, height: H - B - T, fill: "transparent" }));
            hover(g, `${dayLabel(x.day)}\n${x.good + x.stopped + x.other} received\n${x.good} approved or paid\n${x.stopped} stopped\n${x.other} in progress`);
            s.appendChild(g);
            const every = n <= 8 ? 1 : n <= 31 ? 5 : n <= 95 ? 14 : 30;
            if ((i % every === 0 && n - 1 - i >= every * 0.6) || i === n - 1) s.appendChild(svg("text", { x: gx + w / 2, y: H - 8, "text-anchor": "middle", "font-size": 11, fill: C.mute }, dayLabel(x.day)));
        });
        return s;
    }
    function donut(d) {
        const entries = Object.entries(d.status), tot = entries.reduce((a, [, n]) => a + n, 0) || 1;
        const s = svg("svg", { viewBox: "0 0 200 200", class: "an-svg", style: "max-width:220px;margin:0 auto", role: "img", "aria-label": "Invoices by status" });
        const r = 70, cx = 100, cy = 100, circ = 2 * Math.PI * r; let off = 0;
        s.appendChild(svg("circle", { cx, cy, r, fill: "none", stroke: "#f1f0ea", "stroke-width": 26 }));
        entries.forEach(([k, n]) => {
            const len = (n / tot) * circ;
            const c = svg("circle", { cx, cy, r, fill: "none", stroke: SCOL[k] || C.other, "stroke-width": 26, "stroke-dasharray": `${Math.max(0, len - 1.5)} ${circ}`, "stroke-dashoffset": -off, transform: `rotate(-90 ${cx} ${cy})` });
            hover(c, `${STATUS[k] || k}: ${n} (${Math.round((100 * n) / tot)}%)`); s.appendChild(c); off += len;
        });
        s.appendChild(svg("text", { x: cx, y: cy - 2, "text-anchor": "middle", "font-size": 30, "font-weight": 700, fill: C.ink }, String(tot === 1 && !entries.length ? 0 : tot)));
        s.appendChild(svg("text", { x: cx, y: cy + 18, "text-anchor": "middle", "font-size": 12, fill: C.mute }, "invoices"));
        return s;
    }
    function bars(items, color, label) {
        const box = el("div");
        if (!items.length) { box.appendChild(el("div", "an-empty", "Nothing here yet")); return box; }
        const max = Math.max(...items.map(i => i.n));
        items.forEach(i => {
            const row = el("div", "an-bar"); row.appendChild(el("div", "lb", (label || human)(i.key)));
            const tr = el("div", "tr"), fl = el("div", "fl"); fl.style.width = (100 * i.n / max) + "%"; fl.style.background = color; tr.appendChild(fl);
            row.appendChild(tr); row.appendChild(el("b", "", String(i.n))); box.appendChild(row);
        });
        return box;
    }
    function paidChart(d) {
        const W = 560, H = 190, L = 40, B = 24, T = 10, R = 8, v = d.paid_series.map(x => x.amount), n = v.length;
        const max = Math.max(1, ...v), s = svg("svg", { class: "an-svg", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Paid out per day" });
        for (let i = 0; i <= 3; i++) { const y = H - B - (i / 3) * (H - B - T); s.appendChild(svg("line", { x1: L, x2: W - R, y1: y, y2: y, stroke: C.line })); s.appendChild(svg("text", { x: L - 6, y: y + 4, "text-anchor": "end", "font-size": 11, fill: C.mute }, nf.format((max * i) / 3))); }
        const pts = v.map((a, i) => [L + (n === 1 ? 0 : (i * (W - L - R)) / (n - 1)), H - B - (a / max) * (H - B - T)]);
        const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
        s.appendChild(svg("path", { d: `${line} L ${pts[n - 1][0]} ${H - B} L ${pts[0][0]} ${H - B} Z`, fill: C.good, opacity: ".13" }));
        s.appendChild(svg("path", { d: line, fill: "none", stroke: C.good, "stroke-width": 2.5, "stroke-linejoin": "round" }));
        d.paid_series.forEach((x, i) => { if (!x.amount) return; const c = svg("circle", { cx: pts[i][0], cy: pts[i][1], r: 4, fill: "#fff", stroke: C.good, "stroke-width": 2 }); hover(c, `${dayLabel(x.day)}\nPaid out ${money(x.amount, d.currency)}`); s.appendChild(c); });
        const every = n <= 8 ? 1 : n <= 31 ? 7 : n <= 95 ? 20 : 40;
        d.paid_series.forEach((x, i) => { if ((i % every === 0 && n - 1 - i >= every * 0.6) || i === n - 1) s.appendChild(svg("text", { x: pts[i][0], y: H - 6, "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle", "font-size": 11, fill: C.mute }, dayLabel(x.day))); });
        return s;
    }
    function vendors(d) {
        const box = el("div");
        if (!d.vendors.length) { box.appendChild(el("div", "an-empty", "No vendors in this period")); return box; }
        const max = Math.max(1, ...d.vendors.map(v => v.paid));
        d.vendors.forEach(v => {
            const row = el("div", "an-bar"); row.appendChild(el("div", "lb", v.name));
            const tr = el("div", "tr"), fl = el("div", "fl"); fl.style.width = (100 * v.paid / max) + "%"; fl.style.background = C.blue; tr.appendChild(fl);
            row.appendChild(tr); row.appendChild(el("b", "", v.paid ? money(v.paid, d.currency) : `${v.invoices} inv`)); hover(row, `${v.name}\n${v.invoices} invoice(s), ${v.stopped} stopped\nPaid ${money(v.paid, d.currency)}`);
            box.appendChild(row);
        });
        return box;
    }
    function legend(items) { const l = el("div", "an-leg"); items.forEach(([c, t]) => { const s = el("span"); const i = el("i"); i.style.background = c; s.appendChild(i); s.appendChild(document.createTextNode(t)); l.appendChild(s); }); return l; }
    function card(cls, title, sub) { const c = el("div", "an-card " + cls); c.appendChild(el("h3", "", title)); if (sub) c.appendChild(el("p", "sub", sub)); return c; }

    function render(root, d, onRange) {
        root.textContent = "";
        const head = el("div", "an-head"); head.appendChild(el("h2", "", "How Paymaster is doing"));
        const rg = el("div", "an-range");
        [7, 30, 90].forEach(n => { const b = el("button", d.days === n ? "on" : "", `${n} days`); b.type = "button"; b.addEventListener("click", () => onRange(n)); rg.appendChild(b); });
        head.appendChild(rg); root.appendChild(head);
        const k = d.kpi, g = el("div", "an-grid");
        if (!d.total) {
            const c = card("", "No invoices in the last " + d.days + " days", "Upload an invoice and the charts fill in."); c.appendChild(el("div", "an-empty", "Nothing to chart yet.")); g.appendChild(c); root.appendChild(g); return;
        }
        const stoppedSeries = d.timeline.map(x => x.stopped);
        g.appendChild(kpi("bad", "Stopped before paying", money(k.stopped_amount, d.currency), `${k.stopped_count} invoice${k.stopped_count === 1 ? "" : "s"} held, rejected or cancelled`, spark(stoppedSeries, C.stop)));
        g.appendChild(kpi("good", "Paid out", money(k.paid_amount, d.currency), `${k.paid_count} payment${k.paid_count === 1 ? "" : "s"} confirmed by PayPal`, spark(d.paid_series.map(x => x.amount), C.good)));
        g.appendChild(kpi("info", "Cleared by checks alone", k.auto_rate == null ? "n/a" : Math.round(k.auto_rate * 100) + "%", `${k.auto_approved} of ${k.verified} verified invoices passed every check`, gauge(k.auto_rate)));
        g.appendChild(kpi("warn", "Median time to verdict", dur(k.median_seconds), "From arrival to Verify's decision", null));
        const t = card("an-8", "Invoices received per day", "Green: approved or paid. Red: stopped. Grey: still in progress. Hover a bar for numbers.");
        t.appendChild(timeline(d)); t.appendChild(legend([[C.good, "Approved or paid"], [C.stop, "Stopped"], [C.other, "In progress"]])); g.appendChild(t);
        const st = card("an-4", "Where invoices are now"); st.appendChild(donut(d));
        st.appendChild(legend(Object.entries(d.status).map(([k2, n]) => [SCOL[k2] || C.other, `${STATUS[k2] || k2} ${n}`]))); g.appendChild(st);
        const rs = card("an-4", "Why invoices were stopped", "Reasons recorded by Verify"); rs.appendChild(bars(d.reasons, C.stop)); g.appendChild(rs);
        const fs = card("an-4", "Fraud signals that fired", "Facts found by code or AI, never decisions"); fs.appendChild(bars(d.signals, C.amber)); g.appendChild(fs);
        const ai = card("an-4", "AI and people", "The AI suggests. Code and people decide.");
        const a = el("div", "an-ai");
        [[d.ai.overruled_by_code, "AI said approve, code said no"], [d.human.approved, "people approved"], [d.human.rejected, "people rejected"]].forEach(([n, l]) => { const x = el("div"); x.appendChild(el("b", "", String(n))); x.appendChild(el("span", "", l)); a.appendChild(x); });
        ai.appendChild(a); ai.appendChild(el("div", "an-note", d.ai.ran ? `The AI investigator ran on ${d.ai.ran} invoice${d.ai.ran === 1 ? "" : "s"} in this period.` : "The AI investigator has not run in this period.")); g.appendChild(ai);
        const pc = card("an-8", "Money paid out per day", "Confirmed by PayPal, in " + d.currency); pc.appendChild(paidChart(d)); g.appendChild(pc);
        const vc = card("an-4", "Top vendors by amount paid"); vc.appendChild(vendors(d)); g.appendChild(vc);
        root.appendChild(g);
        if (d.mixed_currency) root.appendChild(el("div", "an-note", `Amounts are shown in ${d.currency} only. Invoices in other currencies are counted but not added to the money totals.`));
    }

    let days = 30;
    async function load(apiBase, headers) {
        const root = document.getElementById("analytics"); if (!root) return;
        if (!document.getElementById("an-css")) { const s = el("style"); s.id = "an-css"; s.textContent = CSS; document.head.appendChild(s); }
        const go = async n => {
            days = n;
            try {
                const r = await fetch(`${apiBase}/analytics?days=${n}`, { headers: headers() });
                if (!r.ok) { root.textContent = ""; root.appendChild(el("div", "an-note", `Charts unavailable (error ${r.status})`)); return; }
                render(root, await r.json(), go);
            } catch (e) { root.textContent = ""; root.appendChild(el("div", "an-note", "Charts unavailable: cannot reach the API")); }
        };
        return go(days);
    }
    window.PMOverview = { load, render };
})();
