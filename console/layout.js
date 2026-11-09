(function () {
    const slot = document.getElementById("sidebar");
    window.layoutReady = fetch("base.html")
        .then(r => r.text())
        .then(html => {
            slot.innerHTML = html;
            const page = location.pathname.split("/").pop() || "index.html";
            if (page !== "index.html") {
                slot.querySelectorAll(".nav a").forEach(a =>
                    a.classList.toggle("active", a.getAttribute("href") === page));
            }
            const u = JSON.parse(localStorage.getItem("pm_user") || "null");
            document.getElementById("who").textContent = u ? `${u.full_name} · ${u.company_name || ""}` : "";
            document.getElementById("logout").onclick = () => PM.logout();
        });
})();