/* Live payment lifecycle for one invoice. Polls GET /lifecycle/{ref} while the payment is still moving.
   PMLife.mount(boxElement, invoiceRef, apiBase). Text from the server goes through textContent only. */
(function () {
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
    const dur = s => s == null ? "" : s < 1 ? "instant" : s < 90 ? `${Math.round(s)} sec` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} days`;
    const clock = s => { s = Math.max(0, Math.floor(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0"); };
    const when = t => t ? new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
    const P = {
        received: ["m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7", "M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"],
        verified: ["M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z", "M14 2v4a2 2 0 0 0 2 2h4", "m9 15 2 2 4-4"],
        fraud: ["M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z", "M12 8v4", "M12 16h.01"],
        human: ["m16 11 2 2 4-4", "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2", "M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0z"],
        payout: ["M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z", "m21.854 2.147-10.94 10.939"],
        webhook: ["M8 3 4 7l4 4", "M4 7h16", "m16 21 4-4-4-4", "M20 17H4"],
        ledger: ["M12 7v14", "M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"],
        reconciled: ["M21.801 10A10 10 0 1 1 17 3.335", "m9 11 3 3L22 4"],
        done: ["M20 6 9 17l-5-5"],
        problem: ["M12 8v4", "M12 16h.01"],
        skipped: ["M5 12h14"]
    };
    const svgIcon = key => {
        const NS = "http://www.w3.org/2000/svg", s = document.createElementNS(NS, "svg");
        s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true");
        (P[key] || []).forEach(d => { const p = document.createElementNS(NS, "path"); p.setAttribute("d", d); s.appendChild(p); });
        return s;
    };
    const SEG = ["#1f4e8c", "#2e9e7a", "#6b4fbb", "#c77700", "#0b6b4f", "#3b7dd8", "#8a5200", "#b3261e"];

    const CSS = `
.lc{background:linear-gradient(180deg,#fff,#fbfaf6);border:1px solid #e4e2da;border-radius:16px;padding:16px 16px 14px;margin-bottom:14px;position:relative;overflow:hidden}
.lc-top{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:4px}.lc-top h2{margin:0;font-size:15px;flex:1;min-width:200px}
.lc-pill{font-size:12px;font-weight:700;padding:3px 10px;border-radius:99px;display:inline-flex;gap:6px;align-items:center}
.lc-pill.live{background:#e6f3ee;color:#0b6b4f}.lc-pill.live i{width:8px;height:8px;border-radius:50%;background:#0b6b4f;animation:lcp 1.4s infinite}
.lc-pill.done{background:#0b6b4f;color:#fff}.lc-pill.bad{background:#fbe9e7;color:#b3261e}
@keyframes lcp{0%{box-shadow:0 0 0 0 rgba(11,107,79,.55)}70%{box-shadow:0 0 0 8px rgba(11,107,79,0)}100%{box-shadow:0 0 0 0 rgba(11,107,79,0)}}
.lc-clock{font:700 15px ui-monospace,SFMono-Regular,Menlo,monospace;background:#0e1726;color:#7fd1b9;border-radius:8px;padding:3px 9px}
.lc-bar{height:6px;background:#eeede8;border-radius:99px;overflow:hidden;margin:8px 0 18px}.lc-bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#0b6b4f,#2e9e7a,#7fd1b9);border-radius:99px;transition:width .9s cubic-bezier(.2,.8,.2,1)}
.lc-bar.bad i{background:linear-gradient(90deg,#b3261e,#e57368)}
.lc-row{display:grid;grid-template-columns:repeat(8,1fr);position:relative}
.lc-st{position:relative;text-align:center;padding:0 3px 6px}
.lc-st:not(:last-child)::before{content:"";position:absolute;top:21px;left:calc(50% + 24px);right:calc(-50% + 24px);height:4px;border-radius:4px;background:#e4e2da}
.lc-st.done:not(:last-child)::before{background:#2e9e7a}
.lc-st.flow:not(:last-child)::before{background:repeating-linear-gradient(90deg,#2e9e7a 0 8px,#cfe9df 8px 16px);background-size:32px 4px;animation:lcf 0.9s linear infinite}
@keyframes lcf{to{background-position:32px 0}}
.lc-nd{width:44px;height:44px;border-radius:50%;margin:0 auto 8px;display:grid;place-items:center;font-size:18px;border:3px solid #e4e2da;background:#fff;color:#8a8a82;position:relative;z-index:1;transition:all .4s}
.lc-nd svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.lc-st.done .lc-nd{background:#0b6b4f;border-color:#0b6b4f;color:#fff;box-shadow:0 4px 12px rgba(11,107,79,.25)}
.lc-st.skipped .lc-nd{border-style:dashed;background:#faf9f5}
.lc-st.problem .lc-nd{background:#b3261e;border-color:#b3261e;color:#fff;animation:lcs .5s 2}
@keyframes lcs{25%{transform:translateX(-3px)}75%{transform:translateX(3px)}}
.lc-st.active .lc-nd{border-color:#1f4e8c;color:#1f4e8c;background:#e8eff9}
.lc-st.active .lc-nd::after{content:"";position:absolute;inset:-9px;border-radius:50%;border:2px solid #1f4e8c;opacity:0;animation:lcr 1.8s infinite}
@keyframes lcr{0%{transform:scale(.7);opacity:.7}100%{transform:scale(1.25);opacity:0}}
.lc-st.just .lc-nd{animation:lcpop .7s}@keyframes lcpop{0%{transform:scale(.6)}60%{transform:scale(1.18)}100%{transform:scale(1)}}
.lc-nm{font-size:12px;font-weight:700;line-height:1.2}.lc-st.problem .lc-nm{color:#b3261e}.lc-st.active .lc-nm{color:#1f4e8c}
.lc-tm{font-size:11px;color:#6b6a63;margin-top:2px}.lc-dt{font-size:11px;color:#6b6a63;margin-top:2px}.lc-took{display:inline-block;margin-top:4px;font-size:10px;font-weight:700;background:#eeede8;color:#4a4a45;border-radius:99px;padding:1px 7px}
.lc-two{display:grid;grid-template-columns:1.2fr 1fr;gap:14px;margin-top:14px}
.lc-box{background:#fff;border:1px solid #eeede8;border-radius:12px;padding:10px 12px}.lc-box h3{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#6b6a63}
.lc-seg{display:flex;height:16px;border-radius:99px;overflow:hidden;background:#f1f0ea}.lc-seg i{display:block;height:100%;min-width:3px;transition:flex .8s}
.lc-leg{display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:8px;font-size:12px;color:#4a4a45}.lc-leg i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px}
.lc-feed{display:grid;gap:6px;max-height:170px;overflow:auto}.lc-ev{display:grid;grid-template-columns:auto 1fr;gap:8px;font-size:13px;animation:lcin .5s both}
.lc-ev time{font:11px ui-monospace,Menlo,monospace;color:#6b6a63;padding-top:2px}.lc-ev.bad b{color:#b3261e}
@keyframes lcin{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:none}}
@media(max-width:860px){.lc-row{grid-template-columns:1fr}.lc-st{text-align:left;display:grid;grid-template-columns:54px 1fr;padding-bottom:12px}.lc-nd{margin:0}
.lc-st:not(:last-child)::before{top:46px;bottom:-6px;left:21px;right:auto;width:4px;height:auto}.lc-two{grid-template-columns:1fr}.lc-st.flow:not(:last-child)::before{background-size:4px 32px;background:repeating-linear-gradient(180deg,#2e9e7a 0 8px,#cfe9df 8px 16px);animation:none}}
@media(prefers-reduced-motion:reduce){.lc *{animation:none!important;transition:none!important}}`;
    function css() { if (document.getElementById("lc-css")) return; const s = el("style"); s.id = "lc-css"; s.textContent = CSS; document.head.appendChild(s); }

    let current = null;
    function mount(box, ref, api) {
        css(); if (current) current.stop();
        const state = { stopped: false, timer: null, tick: null, prev: null, seen: new Set(), feed: [], d: null, clockBase: 0 };
        current = { stop() { state.stopped = true; clearTimeout(state.timer); clearInterval(state.tick); } };
        box.textContent = "";
        const root = el("div", "lc"); box.appendChild(root);

        function draw(d) {
            const changed = state.prev ? d.stages.filter((s, i) => state.prev.stages[i].state !== s.state).map(s => s.key) : [];
            // feed: every dated stage, newest first; stages that changed since the last poll animate in
            d.stages.forEach(s => { if (s.at && !state.seen.has(s.key + s.state)) { state.seen.add(s.key + s.state); state.feed.push({ at: s.at, label: s.label, detail: s.detail, bad: s.state === "problem" }); } });
            state.feed.sort((a, b) => new Date(b.at) - new Date(a.at));
            root.textContent = "";
            const top = el("div", "lc-top"); top.appendChild(el("h2", "", `Payment lifecycle: invoice ${d.invoice_number}`));
            const pill = el("span", "lc-pill " + (d.has_problem ? "bad" : d.complete ? "done" : "live"));
            if (d.has_problem) pill.textContent = "Needs attention"; else if (d.complete) pill.textContent = "Complete"; else { pill.appendChild(el("i")); pill.appendChild(document.createTextNode("Live")); }
            top.appendChild(pill); const ck = el("span", "lc-clock", ""); ck.title = d.complete ? "Total time from arrival to the last step" : "Time since the invoice arrived"; top.appendChild(ck); root.appendChild(top);
            const settle = () => { ck.textContent = d.complete && d.total_seconds != null ? clock(d.total_seconds) : clock(((Date.now() - new Date(d.started_at)) / 1000)); };
            settle(); clearInterval(state.tick); if (!d.complete) state.tick = setInterval(settle, 1000);

            const done = d.stages.filter(s => s.state === "done" || s.state === "skipped").length, pct = Math.round((100 * done) / d.stages.length);
            const bar = el("div", "lc-bar" + (d.has_problem ? " bad" : "")), fill = el("i"); bar.appendChild(fill); root.appendChild(bar); requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = pct + "%"; }));

            const row = el("div", "lc-row");
            d.stages.forEach((st, i) => {
                const active = i === d.active, cls = ["lc-st", st.state];
                const nextPending = d.stages[i + 1] && d.stages[i + 1].state === "pending" && i + 1 === d.active && (st.state === "done" || st.state === "skipped");
                if (active) cls.push("active"); if (nextPending) cls.push("flow"); if (changed.includes(st.key)) cls.push("just");
                const c = el("div", cls.join(" "));
                const nd = el("div", "lc-nd");
                nd.appendChild(svgIcon(st.state === "problem" ? "problem" : st.state === "done" ? "done" : st.state === "skipped" ? "skipped" : st.key));
                c.appendChild(nd);
                const b = el("div"); b.appendChild(el("div", "lc-nm", st.label));
                b.appendChild(el("div", "lc-tm", st.at ? new Date(st.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : st.state === "skipped" ? "not needed" : active ? "in progress" : "waiting"));
                b.appendChild(el("div", "lc-dt", st.detail)); if (st.seconds != null && st.state === "done") b.appendChild(el("span", "lc-took", "+" + dur(st.seconds)));
                c.appendChild(b); row.appendChild(c);
            });
            root.appendChild(row);

            const two = el("div", "lc-two");
            const b1 = el("div", "lc-box"); b1.appendChild(el("h3", "", "Where the time went"));
            const timed = d.stages.filter(s => s.seconds != null && s.seconds > 0 && s.state === "done"), sum = timed.reduce((a, s) => a + s.seconds, 0);
            if (timed.length && sum > 0) {
                const seg = el("div", "lc-seg"), leg = el("div", "lc-leg");
                timed.forEach((s, i) => {
                    const x = el("i"); x.style.background = SEG[d.stages.findIndex(q => q.key === s.key) % SEG.length]; x.style.flex = String(Math.max(s.seconds, sum * 0.02)); x.title = `${s.label}: ${dur(s.seconds)}`; seg.appendChild(x);
                    const l = el("span"); const dot = el("i"); dot.style.background = x.style.background; l.appendChild(dot); l.appendChild(document.createTextNode(`${s.label} ${dur(s.seconds)}`)); leg.appendChild(l);
                });
                b1.appendChild(seg); b1.appendChild(leg);
                const slow = timed.reduce((m, s) => (s.seconds > m.seconds ? s : m), timed[0]);
                b1.appendChild(el("div", "lc-dt", `Slowest step: ${slow.label} (${dur(slow.seconds)}).`));
            } else b1.appendChild(el("div", "lc-dt", "Step timings appear as the invoice moves along."));
            const b2 = el("div", "lc-box"); b2.appendChild(el("h3", "", "Activity"));
            const feed = el("div", "lc-feed");
            state.feed.slice(0, 12).forEach(e => { const r = el("div", "lc-ev" + (e.bad ? " bad" : "")); r.appendChild(el("time", "", new Date(e.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }))); const t = el("div"); t.appendChild(el("b", "", e.label)); t.appendChild(document.createTextNode(" " + (e.detail || ""))); r.appendChild(t); feed.appendChild(r); });
            b2.appendChild(feed); two.appendChild(b1); two.appendChild(b2); root.appendChild(two);
            state.prev = d;
        }

        async function poll() {
            if (state.stopped) return;
            let d = null;
            try { const r = await fetch(`${api}/lifecycle/${encodeURIComponent(ref)}`); if (r.ok) d = await r.json(); } catch (e) { }
            if (state.stopped) return;
            if (d) {
                const same = state.prev && JSON.stringify(state.prev.stages.map(s => [s.state, s.at])) === JSON.stringify(d.stages.map(s => [s.state, s.at])); if (!same) draw(d); else state.prev = d;
                if (d.complete) { return; }
            }
            state.timer = setTimeout(poll, document.hidden ? 15000 : d && d.has_problem ? 10000 : 4000);
        }
        poll();
    }
    window.PMLife = { mount };
})();
