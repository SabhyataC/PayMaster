"""Hosting readiness: the console follows the server it is served from, the server serves the console,
nothing outside console/ can be fetched, deploy files exist. Run on a TEST database:  python -m tests.test_hosting"""
import re
import sys
from pathlib import Path

from tests.test_hardening import RESULTS, check, client, section  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


def test_hosting():
    section("Hosting: one server for API and console")
    bad = []
    for f in list((ROOT / "console").glob("*.html")) + [ROOT / "console" / "pm.js"]:
        t = f.read_text(encoding="utf-8")
        for m in re.finditer(r'localhost:8000', t):
            line = t[max(0, t.rfind("\n", 0, m.start())):t.find("\n", m.end())]
            if "location.origin" not in line:
                bad.append(f.name)
    check("no console file points only at localhost:8000 (each falls back to its own origin when hosted)", not bad, bad)
    r = client.get("/console/login.html")
    check("the backend serves the console (login page 200)", r.status_code == 200 and "<html" in r.text.lower(), r.status_code)
    check("static pages and scripts are served", client.get("/console/pm.js").status_code == 200 and client.get("/console/team.html").status_code == 200)
    for p in ("/console/../backend/db.py", "/console/%2e%2e/backend/db.py", "/console/..%2fbackend%2fdb.py", "/console/../.env"):
        r = client.get(p)
        check(f"cannot read files outside console/ ({p})", r.status_code in (400, 404) and "DATABASE_URL" not in r.text, r.status_code)
    check("API root still answers for the health check", client.get("/").status_code == 200)
    for f in ("Dockerfile", "render.yaml", ".dockerignore", "scripts/apply_schema.py"):
        check(f"{f} exists", (ROOT / f).is_file())
    y = (ROOT / "render.yaml").read_text()
    for k in ("DATABASE_URL", "JWT_SECRET", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID", "GEMINI_API_KEY", "TRUST_PROXY"):
        check(f"render.yaml sets {k}", k in y)
    secrets = re.findall(r"(?:SECRET|PASSWORD|TOKEN|API_KEY)\n\s+value:\s*\S+", y)
    check("render.yaml holds no secret values (all are sync: false or generated)", not secrets, secrets)
    d = (ROOT / "Dockerfile").read_text()
    check("Dockerfile does not copy .env or tests", ".env" not in d.replace(".dockerignore", "") and "COPY tests" not in d)
    check("the container applies the schema, then starts on $PORT", "apply_schema" in d and "${PORT" in d)


def main():
    try:
        test_hosting()
    except Exception as e:
        import traceback; traceback.print_exc()
        check("test_hosting ran without crashing", False, repr(e))
    print(f"\n{sum(RESULTS)} / {len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
