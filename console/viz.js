/* Shared chart helpers for the Approvals, Reconciliation and Lifecycle views. Plain SVG, no libraries.
   Text from the server only ever goes through textContent / setAttribute, never innerHTML. */
(function () {
    const NS = "http://www.w3.org/2000/svg";
    const C = { good: "#0b6b4f", good2: "#2e9e7a", bad: "#b3261e", amber: "#c77700", blue: "#1f4e8c", violet: "#6b4fbb", mute: "#6b6a63", line: "#e4e2da", ink: "#1d1d1b" };
    const svg = (tag, a, text) => { const e = document.createElementNS(NS, tag); for (const k in (a || {})) e.setAttribute(k, a[k]); if (text !== undefined) e.textContent = text; return e; };
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
    const nf = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
    const dur = s => s == null ? "n/a" : s < 90 ? `${Math.round(s)} sec` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} days`;
    const dayLabel = iso => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

    const CSS = `
.vz-grid{display:grid;gap:14px;grid-template-columns:repeat(12,1fr);margin-bottom:14px}
.vz-card{background:#fff;border:1px solid #e4e2da;border-radius:14px;padding:14px 16px;min-width:0;grid-column:span 12;box-shadow:0 1px 0 rgba(0,0,0,.02)}
.vz-3{grid-column:span 3}.vz-4{grid-column:span 4}.vz-5{grid-column:span 5}.vz-6{grid-column:span 6}.vz-7{grid-column:span 7}.vz-8{grid-column:span 8}
@media(max-width:1000px){.vz-3{grid-column:span 6}.vz-4,.vz-5,.vz-6,.vz-7,.vz-8{grid-column:span 12}}@media(max-width:560px){.vz-3{grid-column:span 12}}
.vz-card h3{margin:0;font-size:14px}.vz-card .vz-sub{font-size:12px;color:#6b6a63;margin:2px 0 10px}
.vz-kpi{position:relative;overflow:hidden}.vz-kpi .t{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#6b6a63}
.vz-kpi .n{font-size:30px;font-weight:800;margin:2px 0;letter-spacing:-.02em}.vz-kpi .h{font-size:12px;color:#6b6a63}
.vz-kpi svg{display:block;width:100%;height:38px;margin-top:8px}
.vz-kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--k,#1f4e8c)}
.vz-svg{width:100%;height:auto;display:block;overflow:visible}
.vz-leg{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:#4a4a45;margin-top:8px}.vz-leg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.vz-tip{position:fixed;z-index:9999;pointer-events:none;background:#0e1726;color:#fff;font-size:12px;padding:6px 9px;border-radius:7px;box-shadow:0 4px 14px rgba(0,0,0,.25);display:none;white-space:pre-line}
.vz-insight{background:linear-gradient(135deg,#0e1726,#1b2c4a);color:#e8eef9;border:0}
.vz-insight h3{color:#fff;display:flex;gap:8px;align-items:center}.vz-insight .spark{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,#7fd1b9,#8fb4ff);color:#0e1726;font-size:13px}
.vz-insight ul{margin:8px 0 0;padding:0;list-style:none;display:grid;gap:8px}.vz-insight li{font-size:14px;line-height:1.4;padding-left:16px;position:relative}
.vz-insight li::before{content:"";position:absolute;left:0;top:7px;width:7px;height:7px;border-radius:50%;background:#7fd1b9}.vz-insight li.warn::before{background:#ffb454}.vz-insight li.bad::before{background:#ff8a80}
.vz-insight small{color:#9fb0cf}
.vz-hbar{display:grid;grid-template-columns:minmax(90px,38%) 1fr auto;gap:8px;align-items:center;font-size:13px;margin:8px 0}
.vz-hbar .tr{height:10px;background:#f1f0ea;border-radius:99px;overflow:hidden}.vz-hbar .fl{display:block;height:100%;border-radius:99px;width:0;transition:width .8s cubic-bezier(.2,.8,.2,1)}
.vz-hbar .lb{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vz-empty{color:#6b6a63;font-size:13px;padding:22px 0;text-align:center}
.vz-live{display:inline-flex;gap:6px;align-items:center;font-size:12px;color:#0b6b4f;font-weight:600}.vz-live i{width:8px;height:8px;border-radius:50%;background:#0b6b4f;animation:vzp 1.6s infinite}
@keyframes vzp{0%{box-shadow:0 0 0 0 rgba(11,107,79,.5)}70%{box-shadow:0 0 0 8px rgba(11,107,79,0)}100%{box-shadow:0 0 0 0 rgba(11,107,79,0)}}
@media(prefers-reduced-motion:reduce){.vz-live i{animation:none}.vz-hbar .fl{transition:none}}
.vz-pop{animation:vzin .5s ease both}@keyframes vzin{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){.vz-pop{animation:none}}`;
    function css() { if (document.getElementById("vz-css")) return; const s = el("style"); s.id = "vz-css"; s.textContent = CSS; document.head.appendChild(s); }

    let tip;
    function showTip(ev, text) {
        if (!tip) { tip = el("div", "vz-tip"); document.body.appendChild(tip); }
        tip.textContent = text; tip.style.display = "block";
        tip.style.left = Math.min(ev.clientX + 12, window.innerWidth - 200) + "px"; tip.style.top = (ev.clientY + 14) + "px";
    }
    const hideTip = () => { if (tip) tip.style.display = "none"; };
    const hover = (n, text) => { n.addEventListener("mousemove", e => showTip(e, text)); n.addEventListener("mouseleave", hideTip); };

    function countUp(node, to, fmt) {
        fmt = fmt || (v => String(Math.round(v)));
        if (reduce || !isFinite(to)) { node.textContent = fmt(to); return; }
        const t0 = performance.now(), D = 700;
        (function f(t) { const k = Math.min(1, (t - t0) / D), e = 1 - Math.pow(1 - k, 3); node.textContent = fmt(to * e); if (k < 1) requestAnimationFrame(f); })(t0);
    }

    function kpi(title, value, hint, color, series, fmt) {
        const c = el("div", "vz-card vz-kpi vz-3 vz-pop"); c.style.setProperty("--k", color || C.blue);
        c.appendChild(el("div", "t", title)); const n = el("div", "n", ""); c.appendChild(n); countUp(n, value, fmt);
        c.appendChild(el("div", "h", hint));
        if (series && series.some(v => v > 0)) c.appendChild(spark(series, color || C.blue));
        return c;
    }
    function spark(values, color) {
        const w = 200, h = 38, max = Math.max(...values, 1), n = values.length;
        const s = svg("svg", { viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none", "aria-hidden": "true" });
        const pts = values.map((v, i) => [n === 1 ? w / 2 : (i * w) / (n - 1), h - 4 - (v / max) * (h - 10)]);
        const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
        s.appendChild(svg("path", { d: `${line} L ${w} ${h} L 0 ${h} Z`, fill: color, opacity: ".12" }));
        s.appendChild(svg("path", { d: line, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
        return s;
    }

    /* stacked daily columns. series: [{key,label,color}], rows: [{day, ...key:number}] */
    function stacked(rows, series, opts) {
        opts = opts || {};
        const W = 640, H = 210, L = 30, B = 24, T = 8, R = 6, iw = W - L - R, ih = H - T - B;
        const tot = r => series.reduce((a, s) => a + (r[s.key] || 0), 0);
        const max = Math.max(...rows.map(tot), 1), niceMax = max <= 4 ? 4 : Math.ceil(max / 4) * 4;
        const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "vz-svg", role: "img", "aria-label": opts.label || "Daily chart" });
        for (let i = 0; i <= 4; i++) {
            const y = T + ih - (ih * i) / 4;
            s.appendChild(svg("line", { x1: L, x2: W - R, y1: y, y2: y, stroke: "#eeede8" }));
            s.appendChild(svg("text", { x: L - 6, y: y + 4, "text-anchor": "end", "font-size": 10, fill: C.mute }, String(Math.round((niceMax * i) / 4))));
        }
        const bw = iw / rows.length;
        rows.forEach((r, i) => {
            let y = T + ih; const x = L + i * bw + bw * 0.18, w = bw * 0.64;
            series.forEach(se => {
                const v = r[se.key] || 0; if (!v) return;
                const h = (v / niceMax) * ih; y -= h;
                const rect = svg("rect", { x, y: reduce ? y : T + ih, width: w, height: reduce ? h : 0, rx: 3, fill: se.color });
                if (!reduce) { rect.appendChild(svg("animate", { attributeName: "y", from: T + ih, to: y, dur: "0.6s", begin: `${i * 0.03}s`, fill: "freeze" })); rect.appendChild(svg("animate", { attributeName: "height", from: 0, to: h, dur: "0.6s", begin: `${i * 0.03}s`, fill: "freeze" })); }
                s.appendChild(rect);
            });
            const hit = svg("rect", { x: L + i * bw, y: T, width: bw, height: ih, fill: "transparent" });
            hover(hit, `${dayLabel(r.day)}\n` + series.map(se => `${se.label}: ${r[se.key] || 0}`).join("\n")); s.appendChild(hit);
            if (rows.length <= 16 || i % 2 === 0) s.appendChild(svg("text", { x: L + i * bw + bw / 2, y: H - 6, "text-anchor": "middle", "font-size": 10, fill: C.mute }, dayLabel(r.day)));
        });
        return s;
    }

    /* line chart with area; lines: [{key,label,color}] */
    function lines(rows, series, opts) {
        opts = opts || {};
        const W = 640, H = 190, L = 30, B = 24, T = 10, R = 8, iw = W - L - R, ih = H - T - B;
        const max = Math.max(...rows.flatMap(r => series.map(se => r[se.key] || 0)), 1), niceMax = max <= 4 ? 4 : Math.ceil(max / 4) * 4;
        const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "vz-svg", role: "img", "aria-label": opts.label || "Trend chart" });
        for (let i = 0; i <= 4; i++) {
            const y = T + ih - (ih * i) / 4;
            s.appendChild(svg("line", { x1: L, x2: W - R, y1: y, y2: y, stroke: "#eeede8" }));
            s.appendChild(svg("text", { x: L - 6, y: y + 4, "text-anchor": "end", "font-size": 10, fill: C.mute }, String(Math.round((niceMax * i) / 4))));
        }
        const X = i => L + (rows.length === 1 ? iw / 2 : (i * iw) / (rows.length - 1)), Y = v => T + ih - (v / niceMax) * ih;
        series.forEach((se, k) => {
            const d = rows.map((r, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(r[se.key] || 0).toFixed(1)).join(" ");
            if (k === 0) s.appendChild(svg("path", { d: `${d} L ${X(rows.length - 1)} ${T + ih} L ${X(0)} ${T + ih} Z`, fill: se.color, opacity: ".10" }));
            const p = svg("path", { d, fill: "none", stroke: se.color, "stroke-width": 2.4, "stroke-linejoin": "round", "stroke-linecap": "round" });
            if (!reduce) { p.setAttribute("pathLength", "1"); p.setAttribute("stroke-dasharray", "1"); p.setAttribute("stroke-dashoffset", "1"); p.appendChild(svg("animate", { attributeName: "stroke-dashoffset", from: 1, to: 0, dur: "0.9s", fill: "freeze" })); }
            s.appendChild(p);
        });
        const bw = iw / Math.max(1, rows.length - 1);
        rows.forEach((r, i) => {
            const hit = svg("rect", { x: X(i) - bw / 2, y: T, width: bw, height: ih, fill: "transparent" });
            hover(hit, `${dayLabel(r.day)}\n` + series.map(se => `${se.label}: ${r[se.key] || 0}`).join("\n")); s.appendChild(hit);
            if (rows.length <= 16 || i % 2 === 0) s.appendChild(svg("text", { x: X(i), y: H - 6, "text-anchor": "middle", "font-size": 10, fill: C.mute }, dayLabel(r.day)));
        });
        return s;
    }

    /* ring gauge 0-100 with a label in the middle */
    function gauge(score, color, label, sub) {
        const R = 52, CIRC = 2 * Math.PI * R, s = svg("svg", { viewBox: "0 0 140 140", class: "vz-svg", style: "max-width:170px;margin:0 auto", role: "img", "aria-label": `${label}: ${score} out of 100` });
        s.appendChild(svg("circle", { cx: 70, cy: 70, r: R, fill: "none", stroke: "#eeede8", "stroke-width": 12 }));
        const arc = svg("circle", { cx: 70, cy: 70, r: R, fill: "none", stroke: color, "stroke-width": 12, "stroke-linecap": "round", transform: "rotate(-90 70 70)", "stroke-dasharray": `${CIRC}`, "stroke-dashoffset": reduce ? CIRC * (1 - score / 100) : CIRC });
        if (!reduce) arc.appendChild(svg("animate", { attributeName: "stroke-dashoffset", from: CIRC, to: CIRC * (1 - score / 100), dur: "1s", fill: "freeze", calcMode: "spline", keyTimes: "0;1", keySplines: ".2 .8 .2 1" }));
        s.appendChild(arc);
        s.appendChild(svg("text", { x: 70, y: 74, "text-anchor": "middle", "font-size": 32, "font-weight": 800, fill: C.ink }, String(Math.round(score))));
        s.appendChild(svg("text", { x: 70, y: 94, "text-anchor": "middle", "font-size": 10, fill: C.mute, "letter-spacing": ".06em" }, (sub || "OUT OF 100").toUpperCase()));
        return s;
    }

    function hbars(items, fmt) {  /* [{label,value,color}] */
        const box = el("div"), max = Math.max(...items.map(i => i.value), 1);
        items.forEach(i => {
            const row = el("div", "vz-hbar"), lb = el("span", "lb", i.label), tr = el("span", "tr"), fl = el("span", "fl");
            fl.style.background = i.color || C.blue; tr.appendChild(fl); row.appendChild(lb); row.appendChild(tr); row.appendChild(el("b", "", fmt ? fmt(i.value) : String(i.value)));
            requestAnimationFrame(() => requestAnimationFrame(() => { fl.style.width = Math.max(2, (i.value / max) * 100) + "%"; }));
            if (i.tip) hover(row, i.tip); box.appendChild(row);
        });
        return box;
    }

    function legend(items) { const l = el("div", "vz-leg"); items.forEach(i => { const s = el("span"); const d = el("i"); d.style.background = i.color; s.appendChild(d); s.appendChild(document.createTextNode(i.label)); l.appendChild(s); }); return l; }
    function card(cls, title, sub) { const c = el("div", "vz-card " + cls + " vz-pop"); if (title) c.appendChild(el("h3", "", title)); if (sub) c.appendChild(el("div", "vz-sub", sub)); return c; }
    function insight(lines_) {
        const c = el("div", "vz-card vz-insight vz-12 vz-pop"), h = el("h3"); h.appendChild(el("span", "spark", "✦")); h.appendChild(document.createTextNode("What this tells you")); c.appendChild(h);
        const ul = el("ul"); lines_.forEach(l => ul.appendChild(el("li", l.kind || "", l.text))); c.appendChild(ul); return c;
    }

    window.PMViz = { C, svg, el, css, dur, dayLabel, countUp, kpi, spark, stacked, lines, gauge, hbars, legend, card, insight, hover, hideTip, nf, reduce };
})();
