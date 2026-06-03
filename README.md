# Client Onboarding Service

A small, standalone, internet-facing Flask app that handles three client-facing
steps in one place:

1. **Discovery questionnaire** — capture exactly what site the client wants
   (fill it together on the call, or send the link).
2. **Agreement + e-signature** — a UK-valid electronic signature with a full
   audit trail and a certificate of completion. Replaces DocuSign for service
   agreements.
3. **Stripe deposit** — a "Pay deposit" button that unlocks after signing and
   hands off to your Stripe Payment Link.

It is **deliberately separate** from the CRM (`leads.db`). Deploy this to a host;
keep the CRM on your machine. Nothing here can touch your lead database.

---

## Quick start (local)

```powershell
cd "C:\Aentic\UK Lead Generator\onboarding_service"
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json   # then edit config.json
$env:ADMIN_PASSWORD = "pick-a-password"
python app.py
```

Open http://127.0.0.1:5055/admin (user `admin`, the password you set).

## What you edit before a call — `config.json`

- **business** — your legal/trading name, contact details (used in the agreement).
- **questionnaire** — the discovery questions (add/remove freely).
- **packages** — name, price, deposit, and the **Stripe Payment Link** for each.
- **agreement** — the services-agreement wording. *Starting template — have a
  solicitor review before relying on it.*

## The flow on the day

1. In **/admin**, create a session for the client → you get two links.
2. Send (or screen-share) the **questionnaire** link, fill it in together.
3. Submitting it lands them on the **agreement** page — they sign.
4. They click **Pay deposit** → your Stripe Payment Link.
5. Back in **/admin → the session**, you can read the brief, view the signature,
   and print the **certificate of completion**.

## Stripe Payment Links

This service does not handle card data — it just links out to Stripe-hosted
pages. Create a Payment Link in your Stripe dashboard (Products → Payment Links),
paste the URL into the package's `stripe_payment_link` in `config.json`. The
client's email is prefilled and the link is tagged with `client_reference_id`
(the session token) so you can reconcile payments. Stripe emails you on payment;
a webhook to auto-update status is an easy future add.

## The signature & UK law

For a website-design **services agreement**, a simple electronic signature is
valid and admissible in the UK (Electronic Communications Act 2000 s.7; retained
eIDAS). Evidential weight comes from the audit trail this service records on every
signing:

- typed full legal name **and** a drawn signature,
- an explicit **intent-to-be-bound** checkbox,
- a **server-side UTC timestamp** (never the browser clock),
- the signer's **IP address** and **user-agent**,
- a **SHA-256 fingerprint of the exact agreement text** they saw.

These live in an append-only `signatures` table and are rendered into a
certificate of completion. Not legal advice — but it's the same evidence bundle
the cheaper e-sign tools rely on.

## Deploy

`render.yaml` is a one-click Render blueprint. After the first deploy, set
`ADMIN_PASSWORD` as a secret in the dashboard. The SQLite db lives on a 1GB
persistent disk so signatures survive redeploys. Any host that runs
`gunicorn app:app` works (Railway, Fly, a VPS).

## Environment variables

| Var | Purpose | Default |
|-----|---------|---------|
| `ADMIN_USER` | admin login user | `admin` |
| `ADMIN_PASSWORD` | admin login password | `changeme` (change it!) |
| `ONBOARDING_DB` | sqlite path | `./onboarding.db` |
| `ONBOARDING_CONFIG` | config path | `./config.json` |
| `ONBOARDING_API_KEY` | shared secret for the CRM Site Wizard to pull briefs | _(unset = endpoint off)_ |
| `PORT` | server port | `5055` |

## Site Wizard integration (CRM)

The CRM has a **Site Wizard** at `/site-wizard` that turns a captured brief into
a live site preview using your existing `website_generator.py`. To let it pull a
brief by token from this service, set on **both** sides the same secret:

- here: `ONBOARDING_API_KEY=<secret>`
- in the CRM Settings (or `.settings.json`): `onboarding_api_key=<secret>` and
  `onboarding_base_url=<this service's URL>`

Then in the wizard, paste a session token and click **Pull** — or just paste the
brief text directly. Generation runs on the CRM (internal), so your Anthropic
key never touches the public box.
