(function () {
    const nativeFetch = window.fetch.bind(window);
    const apiBase = () => (typeof API !== "undefined" ? API : (location.protocol === "file:" || (["localhost", "127.0.0.1"].includes(location.hostname) && location.port !== "8000")) ? "http://localhost:8000" : location.origin);

    window.fetch = function (url, opts) {
        const options = Object.assign({}, opts || {});
        if (String(url).startsWith(apiBase())) {
            options.credentials = "include";
            const headers = new Headers(options.headers || {});
            headers.delete("Authorization");
            headers.set("X-CSRF", "1");
            options.headers = headers;
        }
        return nativeFetch(url, options);
    };

    const readUser = () => {
        try { return JSON.parse(localStorage.getItem("pm_user") || "null"); } catch (e) { return null; }
    };

    window.PM = {
        user: readUser,
        role: () => (readUser() || {}).role || "viewer",
        canAct: () => ["admin", "reviewer"].includes((readUser() || {}).role),
        isAdmin: () => (readUser() || {}).role === "admin",
        signedIn: () => !!readUser(),
        clear: () => { try { localStorage.removeItem("pm_user"); localStorage.removeItem("pm_jwt"); } catch (e) { } },
        async logout() {
            try { await fetch(apiBase() + "/auth/logout", { method: "POST" }); } catch (e) { }
            window.PM.clear();
            location.replace("login.html");
        },
        async refresh() {
            try {
                const r = await fetch(apiBase() + "/auth/me");
                if (r.status === 401) { window.PM.clear(); location.replace("login.html"); return null; }
                if (r.status === 403) { location.replace("account.html"); return null; }
                if (!r.ok) return readUser();
                const u = await r.json();
                localStorage.setItem("pm_user", JSON.stringify(u));
                document.body.dataset.role = u.role;
                return u;
            } catch (e) { return readUser(); }
        },
    };

    try { localStorage.removeItem("pm_jwt"); } catch (e) { }
    document.addEventListener("DOMContentLoaded", () => {
        document.body.dataset.role = window.PM.role();
        const style = document.createElement("style");
        style.textContent =
            'body[data-role="viewer"] .rev-only{display:none!important}' +
            'body[data-role="viewer"] .admin-only,body[data-role="reviewer"] .admin-only{display:none!important}';
        document.head.appendChild(style);
        if (window.PM.signedIn()) window.PM.refresh();
    });
})();
