/* PayMaster UI kit: toasts and dialogs that replace alert(), confirm() and prompt().
   PM.toast(text, "ok" | "error" | "info")
   await PM.confirm(text, { title, okText, cancelText, danger })            -> true / false
   await PM.ask(title, [{name,label,value,min,required,textarea,hint}], {okText, danger, intro}) -> {name: value} or null
   Everything is written with textContent, so text from the server can never become markup. */
(function () {
    const css = `
.pmui-toasts{position:fixed;top:16px;right:16px;z-index:100000;display:flex;flex-direction:column;gap:10px;width:min(380px,calc(100vw - 32px));pointer-events:none}
.pmui-toast{pointer-events:auto;display:flex;gap:10px;align-items:flex-start;background:#fff;color:#1d1d1b;border:1px solid #e4e2da;border-left:5px solid #1f4e8c;border-radius:10px;padding:11px 12px;box-shadow:0 8px 24px rgba(0,0,0,.14);font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;cursor:pointer;animation:pmui-in .22s ease-out;position:relative;overflow:hidden}
.pmui-toast.ok{border-left-color:#0b6b4f}.pmui-toast.error{border-left-color:#b3261e}
.pmui-toast .ic{flex:none;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:700;font-size:13px;background:#1f4e8c}
.pmui-toast.ok .ic{background:#0b6b4f}.pmui-toast.error .ic{background:#b3261e}
.pmui-toast .tx{flex:1;word-break:break-word}.pmui-toast .x{flex:none;color:#6b6a63;font-size:16px;line-height:1}
.pmui-toast .life{position:absolute;left:0;bottom:0;height:3px;background:currentColor;opacity:.18;animation:pmui-life linear forwards}
.pmui-toast.out{animation:pmui-out .18s ease-in forwards}
.pmui-ov{position:fixed;inset:0;z-index:100001;background:rgba(15,23,38,.55);display:grid;place-items:center;padding:16px;animation:pmui-fade .15s ease-out}
.pmui-dlg{background:#fff;color:#1d1d1b;border-radius:14px;width:min(440px,100%);max-height:92vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.35);padding:20px;font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;animation:pmui-pop .18s ease-out}
.pmui-dlg h3{margin:0 0 6px;font-size:17px}.pmui-dlg .intro{margin:0 0 12px;color:#4a4a45;white-space:pre-line}
.pmui-dlg label{display:block;font-weight:600;font-size:13px;margin:10px 0 4px}
.pmui-dlg input,.pmui-dlg textarea{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #cfcdc4;border-radius:8px;font:inherit}
.pmui-dlg textarea{min-height:84px;resize:vertical}.pmui-dlg input:focus,.pmui-dlg textarea:focus{outline:2px solid #1f4e8c;outline-offset:1px;border-color:#1f4e8c}
.pmui-dlg .hint{font-size:12px;color:#6b6a63;margin-top:3px;display:flex;justify-content:space-between;gap:8px}.pmui-dlg .hint .bad{color:#b3261e}
.pmui-dlg .row{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}
.pmui-dlg button{padding:8px 16px;border-radius:8px;border:1px solid #cfcdc4;background:#fff;color:#1d1d1b;font:inherit;cursor:pointer}
.pmui-dlg button.ok{background:#0b6b4f;border-color:#0b6b4f;color:#fff;font-weight:600}.pmui-dlg button.danger{background:#b3261e;border-color:#b3261e;color:#fff;font-weight:600}
.pmui-dlg button:disabled{opacity:.45;cursor:not-allowed}.pmui-dlg button:focus-visible{outline:2px solid #1f4e8c;outline-offset:2px}
@keyframes pmui-in{from{transform:translateX(24px);opacity:0}to{transform:none;opacity:1}}@keyframes pmui-out{to{transform:translateX(24px);opacity:0}}
@keyframes pmui-life{from{width:100%}to{width:0}}@keyframes pmui-fade{from{opacity:0}to{opacity:1}}@keyframes pmui-pop{from{transform:scale(.96) translateY(6px);opacity:0}to{transform:none;opacity:1}}
@media(max-width:520px){.pmui-toasts{top:auto;bottom:12px;right:16px}}
@media(prefers-reduced-motion:reduce){.pmui-toast,.pmui-ov,.pmui-dlg{animation:none}.pmui-toast .life{display:none}}`;
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
    let host;
    function ensure() {
        if (host && host.isConnected) return;
        if (!document.getElementById("pmui-css")) { const s = el("style"); s.id = "pmui-css"; s.textContent = css; document.head.appendChild(s); }
        host = el("div", "pmui-toasts"); host.setAttribute("role", "status"); host.setAttribute("aria-live", "polite");
        document.body.appendChild(host);
    }
    function toast(text, kind) {
        kind = kind || "info";
        const run = () => {
            ensure();
            const t = el("div", "pmui-toast " + kind);
            t.appendChild(el("span", "ic", kind === "ok" ? "✓" : kind === "error" ? "!" : "i"));
            t.appendChild(el("span", "tx", String(text == null ? "" : text)));
            t.appendChild(el("span", "x", "×"));
            const ms = kind === "error" ? 8000 : 4500;
            const life = el("span", "life"); life.style.animationDuration = ms + "ms"; t.appendChild(life);
            const close = () => { if (t.classList.contains("out")) return; t.classList.add("out"); setTimeout(() => t.remove(), 190); };
            t.addEventListener("click", close);
            host.appendChild(t);
            while (host.children.length > 4) host.firstChild.remove();
            setTimeout(close, ms);
        };
        if (document.body) run(); else document.addEventListener("DOMContentLoaded", run);
    }
    function dialog(title, intro, fields, opts) {
        opts = opts || {};
        return new Promise(resolve => {
            const prev = document.activeElement;
            const ov = el("div", "pmui-ov");
            const dlg = el("form", "pmui-dlg"); dlg.setAttribute("role", "dialog"); dlg.setAttribute("aria-modal", "true"); dlg.noValidate = true;
            dlg.appendChild(el("h3", "", title));
            if (intro) dlg.appendChild(el("p", "intro", intro));
            const inputs = [];
            fields.forEach((f, i) => {
                const id = "pmui-f" + i + "-" + Math.random().toString(36).slice(2, 6);
                const lb = el("label", "", f.label); lb.htmlFor = id; dlg.appendChild(lb);
                const inp = f.textarea ? el("textarea") : el("input"); inp.id = id; inp.value = f.value == null ? "" : String(f.value);
                if (!f.textarea) inp.type = "text"; if (f.placeholder) inp.placeholder = f.placeholder; if (f.inputmode) inp.inputMode = f.inputmode;
                dlg.appendChild(inp);
                const hint = el("div", "hint"); const h1 = el("span", "", f.hint || ""), h2 = el("span", "");
                hint.appendChild(h1); hint.appendChild(h2); dlg.appendChild(hint);
                inputs.push({ f, inp, h1, h2 });
            });
            const row = el("div", "row");
            const cancel = el("button", "", opts.cancelText || "Cancel"); cancel.type = "button";
            const ok = el("button", opts.danger ? "danger" : "ok", opts.okText || "OK"); ok.type = "submit";
            row.appendChild(cancel); row.appendChild(ok); dlg.appendChild(row);
            const valid = () => inputs.every(({ f, inp }) => {
                const v = inp.value.trim();
                if (f.min && v.length < f.min) return false;
                if (f.required && !v) return false;
                return true;
            });
            const refresh = () => {
                inputs.forEach(({ f, inp, h2 }) => {
                    if (f.min) { const n = inp.value.trim().length; h2.textContent = n >= f.min ? "✓" : `${n}/${f.min} characters`; h2.className = n >= f.min ? "" : "bad"; }
                });
                ok.disabled = !valid();
            };
            const finish = val => { document.removeEventListener("keydown", onKey, true); ov.remove(); if (prev && prev.focus) try { prev.focus(); } catch (e) { } resolve(val); };
            const onKey = ev => {
                if (ev.key === "Escape") { ev.preventDefault(); finish(null); }
                else if (ev.key === "Tab") {
                    const els = [...dlg.querySelectorAll("input,textarea,button:not(:disabled)")]; if (!els.length) return;
                    const first = els[0], last = els[els.length - 1];
                    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
                    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
                }
            };
            document.addEventListener("keydown", onKey, true);
            dlg.addEventListener("input", refresh);
            dlg.addEventListener("submit", ev => { ev.preventDefault(); if (!valid()) return; const out = {}; inputs.forEach(({ f, inp }) => { out[f.name] = inp.value.trim(); }); finish(out); });
            cancel.addEventListener("click", () => finish(null));
            ov.addEventListener("mousedown", ev => { if (ev.target === ov) finish(null); });
            ov.appendChild(dlg); document.body.appendChild(ov); refresh();
            const focusEl = inputs.length ? inputs[0].inp : (opts.danger ? cancel : ok);
            focusEl.focus(); if (inputs.length && focusEl.select) focusEl.select();
        });
    }
    window.PM = window.PM || {};
    window.PM.toast = toast;
    window.PM.confirm = (text, o) => { o = o || {}; return dialog(o.title || "Please confirm", text, [], { okText: o.okText || "Yes, continue", cancelText: o.cancelText || "Cancel", danger: o.danger }).then(r => r !== null); };
    window.PM.ask = (title, fields, o) => { o = o || {}; return dialog(title, o.intro, fields, o); };
    window.PM.askReason = (title, o) => { o = o || {}; return dialog(title, o.intro, [{ name: "reason", label: o.label || "Reason", textarea: true, min: o.min || 10, hint: o.hint || "Written to the audit log." }], o).then(r => r && r.reason); };
})();
