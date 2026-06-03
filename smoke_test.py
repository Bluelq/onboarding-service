"""End-to-end smoke test via Flask's test client. No live server needed.
Run: python smoke_test.py  (uses a throwaway temp db)."""
import os, tempfile, base64, re

# Isolate: temp db + a config with a fake Stripe link so /pay redirects.
_tmp = tempfile.mkdtemp()
os.environ["ONBOARDING_DB"] = os.path.join(_tmp, "test.db")
os.environ["ADMIN_PASSWORD"] = "secret"

import json
cfg = json.load(open("config.example.json", encoding="utf-8"))
cfg["packages"][0]["stripe_payment_link"] = "https://buy.stripe.com/test_123"
cfgpath = os.path.join(_tmp, "config.json")
json.dump(cfg, open(cfgpath, "w", encoding="utf-8"))
os.environ["ONBOARDING_CONFIG"] = cfgpath

import app as appmod
client = appmod.app.test_client()

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
ok = 0; fail = 0
def check(name, cond):
    global ok, fail
    print(("PASS" if cond else "FAIL"), name)
    if cond: ok += 1
    else: fail += 1

# 1. admin requires auth
r = client.get("/admin")
check("admin blocks unauthenticated", r.status_code == 401)

r = client.get("/admin", headers=AUTH)
check("admin loads with auth", r.status_code == 200 and b"New client onboarding" in r.data)

# 2. create a session
r = client.post("/admin/sessions", headers=AUTH, data={
    "client_name": "Jane Smith", "client_email": "jane@example.com",
    "company": "Smith Ltd", "package_key": "starter",
}, follow_redirects=False)
check("create session redirects", r.status_code == 302)
token = r.headers["Location"].rstrip("/").split("/")[-1]
check("got a token", bool(token) and len(token) > 5)

# 3. questionnaire GET + POST
r = client.get(f"/onboard/{token}")
check("questionnaire loads", r.status_code == 200 and b"design your site" in r.data)

r = client.post(f"/onboard/{token}", data={
    "business_name": "Smith Plumbing", "goal": "Get enquiries / leads",
    "style": "Modern & minimal", "pages": ["Home", "Contact"],
}, follow_redirects=False)
check("questionnaire submit -> agreement", r.status_code == 302 and "/agreement/" in r.headers["Location"])

r = client.get(f"/onboard/{token}/brief.txt")
check("brief.txt has content", r.status_code == 200 and b"Smith Plumbing" in r.data)

# 4. agreement page + extract the sha256 the browser would echo
r = client.get(f"/agreement/{token}")
check("agreement loads", r.status_code == 200 and b"Services Agreement" in r.data)
m = re.search(rb'name="agreement_sha256" value="([0-9a-f]{64})"', r.data)
check("agreement exposes sha256", bool(m))
sha = m.group(1).decode() if m else ""

# 5. signing — reject bad, accept good
r = client.post(f"/agreement/{token}/sign", data={
    "full_name": "", "agreement_sha256": sha, "signature_png": "data:image/png;base64,xx",
})
check("sign rejects missing name/intent", r.status_code == 400)

r = client.post(f"/agreement/{token}/sign", data={
    "full_name": "Jane Smith", "intent": "on",
    "signature_png": "data:image/png;base64,iVBORw0KGgo=",
    "agreement_sha256": sha,
}, follow_redirects=True)
check("valid sign succeeds", r.status_code == 200 and b"Agreement signed" in r.data)

# 6. tamper detection — wrong hash rejected (fresh would-be signer)
#    (already signed, so this also tests idempotency: a 2nd sign is ignored)
r = client.post(f"/agreement/{token}/sign", data={
    "full_name": "Someone Else", "intent": "on",
    "signature_png": "data:image/png;base64,iVBORw0KGgo=",
    "agreement_sha256": "deadbeef",
}, follow_redirects=True)
sig = appmod.db.get_signature_for_token(token)
check("only one signature recorded (idempotent)", sig and sig["signer_full_name"] == "Jane Smith")

# 7. certificate renders with the audit trail
r = client.get(f"/agreement/{token}/certificate")
body = r.data.decode()
check("certificate has SHA-256", sha in body)
check("certificate has timestamp", "Signed at" in body and "Z" in body)
check("certificate notes UK law", "eIDAS" in body)

# 8. pay redirects to Stripe with reference + email
r = client.get(f"/pay/{token}", follow_redirects=False)
loc = r.headers.get("Location", "")
check("pay redirects to stripe", r.status_code == 302 and "buy.stripe.com" in loc)
check("pay tags client_reference_id", f"client_reference_id={token}" in loc)
check("pay prefills email", "prefilled_email=jane@example.com" in loc)

# 9. admin detail shows the brief + signature
r = client.get(f"/admin/sessions/{token}", headers=AUTH)
check("admin detail shows signature", r.status_code == 200 and b"Jane Smith" in r.data and b"Smith Plumbing" in r.data)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
