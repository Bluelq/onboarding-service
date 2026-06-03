#!/usr/bin/env python3
"""
Client Onboarding Service  —  standalone, internet-facing.

A small Flask app that does exactly three client-facing things and nothing else:

  1. Discovery questionnaire   GET/POST /onboard/<token>
  2. Agreement + e-signature   GET /agreement/<token>, POST .../sign
  3. Stripe deposit handoff    GET /pay/<token>

Plus a password-protected /admin to mint a per-client link and review what was
captured (answers, signature, certificate of completion).

It has its OWN sqlite db (onboarding.db) and knows nothing about the CRM. Deploy
it to a host; keep the CRM on your machine. The CRM (or you) creates a session
here and sends the client the link.

Run locally:   python app.py         (http://127.0.0.1:5055)
Production:    gunicorn app:app
"""

import os
import io
import json
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, request, render_template, redirect, url_for, abort,
    Response, jsonify, send_file,
)

import db
from config import load_config, get_package

app = Flask(__name__)
db.init_db()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
# Shared secret the CRM uses to pull a captured brief server-to-server.
ONBOARDING_API_KEY = os.environ.get("ONBOARDING_API_KEY", "")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def now_utc_iso():
    """Server-side UTC timestamp. Never trust the browser clock for signing."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def client_ip():
    """Real client IP, honouring the proxy header set by Render/Cloudflare."""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pounds(pence):
    if pence is None:
        return ""
    return "£{:,.2f}".format(pence / 100.0)


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASSWORD:
            return Response(
                "Admin login required", 401,
                {"WWW-Authenticate": 'Basic realm="Onboarding admin"'},
            )
        return f(*args, **kwargs)
    return wrapper


def render_agreement_body(cfg, session, package):
    """Deterministically render the agreement clauses to HTML with the client's
    details filled in. The EXACT string this returns is what we hash and what the
    client signs, so it must be stable for a given session + config version."""
    ag = cfg["agreement"]
    biz = cfg["business"]

    company = (session.get("company") or "").strip()
    company_clause = f" of {company}" if company else ""

    subs = {
        "business_legal_name": biz.get("legal_name", biz.get("trading_name", "")),
        "client_name": session.get("client_name") or "the Client",
        "client_company_clause": company_clause,
        "date": (session.get("created_at") or "")[:10],
        "package_name": (package or {}).get("name", "the agreed package"),
        "package_price": (package or {}).get("price_label", ""),
        "deposit_amount": pounds(session.get("deposit_pence")),
    }

    def fill(s):
        for k, v in subs.items():
            s = s.replace("{{" + k + "}}", str(v))
        return s

    parts = ['<div class="agreement-body">']
    parts.append(f'<p class="agreement-intro">{fill(ag["intro"])}</p>')
    for clause in ag.get("clauses", []):
        parts.append(f'<h3>{fill(clause["heading"])}</h3>')
        parts.append(f'<p>{fill(clause["body"])}</p>')
    parts.append("</div>")
    return "\n".join(parts)


def ensure_agreement(cfg, session):
    """Return the stored agreement row for this session, creating it on first
    view so the signed hash is locked to the version the client first saw."""
    existing = db.latest_agreement(session["token"])
    if existing:
        return existing
    package = get_package(cfg, session.get("package_key"))
    body_html = render_agreement_body(cfg, session, package)
    digest = sha256_text(body_html)
    db.save_agreement(
        token=session["token"],
        version=cfg["agreement"].get("version", "v1"),
        html=body_html,
        content_sha256=digest,
        deposit_pence=session.get("deposit_pence"),
        created_at=now_utc_iso(),
    )
    return db.latest_agreement(session["token"])


def build_certificate_html(cfg, session, agreement, sig):
    """A self-contained 'certificate of completion' — the human-readable proof
    bundle. Mirrors what DocuSign et al. produce: identity, intent, timestamp,
    IP/device, and the document hash that ties the signature to the exact text."""
    biz = cfg["business"]
    return render_template(
        "certificate.html",
        cfg=cfg, biz=biz, session=session, agreement=agreement, sig=sig,
        deposit=pounds(session.get("deposit_pence")),
    )


# --------------------------------------------------------------------------
# health / root
# --------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "service": "onboarding"})


@app.route("/")
def root():
    return redirect(url_for("admin"))


# --------------------------------------------------------------------------
# admin
# --------------------------------------------------------------------------

@app.route("/admin")
@require_admin
def admin():
    cfg = load_config()
    sessions = db.list_sessions()
    base = request.host_url.rstrip("/")
    warn_default_pw = ADMIN_PASSWORD == "changeme"
    return render_template(
        "admin.html", cfg=cfg, sessions=sessions, base=base,
        pounds=pounds, warn_default_pw=warn_default_pw,
    )


@app.route("/admin/sessions", methods=["POST"])
@require_admin
def admin_create_session():
    cfg = load_config()
    f = request.form
    package_key = f.get("package_key") or ""
    package = get_package(cfg, package_key)
    deposit_pence = package.get("deposit_pence") if package else None

    # Allow a manual deposit override (pence) for custom quotes.
    override = (f.get("deposit_pounds") or "").strip()
    if override:
        try:
            deposit_pence = int(round(float(override) * 100))
        except ValueError:
            pass

    token = secrets.token_urlsafe(9)
    expiry_days = int(cfg.get("session_expiry_days", 30))
    db.create_session(
        token=token,
        client_name=(f.get("client_name") or "").strip(),
        client_email=(f.get("client_email") or "").strip(),
        company=(f.get("company") or "").strip(),
        package_key=package_key,
        deposit_pence=deposit_pence,
        currency="gbp",
        crm_lead_id=(f.get("crm_lead_id") or "").strip() or None,
        notes=(f.get("notes") or "").strip() or None,
        created_at=now_utc_iso(),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=expiry_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return redirect(url_for("admin_session_detail", token=token))


@app.route("/admin/sessions/<token>")
@require_admin
def admin_session_detail(token):
    cfg = load_config()
    session = db.get_session(token)
    if not session:
        abort(404)
    q = db.latest_questionnaire(token)
    answers = json.loads(q["answers_json"]) if q else None
    sig = db.get_signature_for_token(token)
    base = request.host_url.rstrip("/")
    return render_template(
        "admin_detail.html", cfg=cfg, session=session, answers=answers,
        brief=(q["brief"] if q else None), sig=sig, base=base, pounds=pounds,
    )


# --------------------------------------------------------------------------
# client: questionnaire
# --------------------------------------------------------------------------

@app.route("/onboard/<token>", methods=["GET"])
def onboard(token):
    cfg = load_config()
    session = db.get_session(token)
    if not session:
        abort(404)
    q = db.latest_questionnaire(token)
    answers = json.loads(q["answers_json"]) if q else {}
    return render_template(
        "questionnaire.html", cfg=cfg, session=session, answers=answers,
    )


@app.route("/onboard/<token>", methods=["POST"])
def onboard_submit(token):
    cfg = load_config()
    session = db.get_session(token)
    if not session:
        abort(404)

    answers = {}
    for section in cfg["questionnaire"]["sections"]:
        for question in section["questions"]:
            qid = question["id"]
            if question["type"] == "checkboxes":
                answers[qid] = request.form.getlist(qid)
            else:
                answers[qid] = request.form.get(qid, "")

    brief = build_brief_text(cfg, session, answers)
    db.save_questionnaire(token, json.dumps(answers), brief, now_utc_iso())
    if session.get("status") == "created":
        db.set_status(token, "questionnaire")

    # Straight on to the agreement so the call flows: discuss -> sign -> pay.
    return redirect(url_for("agreement", token=token))


def build_brief_text(cfg, session, answers):
    """Flatten the answers into a readable brief you can paste into the CRM /
    website_generator.py. Stored alongside the raw JSON."""
    lines = [f"DISCOVERY BRIEF — {session.get('client_name','')}".strip(),
             f"Captured: {now_utc_iso()}", ""]
    for section in cfg["questionnaire"]["sections"]:
        lines.append(f"## {section['title']}")
        for question in section["questions"]:
            val = answers.get(question["id"], "")
            if isinstance(val, list):
                val = ", ".join(val)
            if val:
                lines.append(f"- {question['label']}: {val}")
        lines.append("")
    return "\n".join(lines)


@app.route("/onboard/<token>/brief.txt")
def onboard_brief(token):
    q = db.latest_questionnaire(token)
    if not q:
        abort(404)
    return Response(q["brief"] or "", mimetype="text/plain")


@app.route("/api/session", methods=["POST"])
def api_create_session():
    """Create an onboarding session from the CRM (keyed, no admin login).
    Returns the token and the client-facing links. Mirrors admin_create_session
    but authenticated by the shared X-Api-Key instead of basic auth."""
    if not ONBOARDING_API_KEY or request.headers.get("X-Api-Key") != ONBOARDING_API_KEY:
        abort(401)
    cfg = load_config()
    data = request.get_json(silent=True) or {}
    package_key = data.get("package_key") or ""
    package = get_package(cfg, package_key)
    deposit_pence = package.get("deposit_pence") if package else None

    token = secrets.token_urlsafe(9)
    expiry_days = int(cfg.get("session_expiry_days", 30))
    db.create_session(
        token=token,
        client_name=(data.get("client_name") or "").strip(),
        client_email=(data.get("client_email") or "").strip(),
        company=(data.get("company") or "").strip(),
        package_key=package_key,
        deposit_pence=deposit_pence,
        currency="gbp",
        crm_lead_id=(data.get("crm_lead_id") or "").strip() or None,
        notes=(data.get("notes") or "").strip() or None,
        created_at=now_utc_iso(),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=expiry_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    base = request.host_url.rstrip("/")
    return jsonify({
        "token": token,
        "questionnaire_url": f"{base}/onboard/{token}",
        "agreement_url": f"{base}/agreement/{token}",
        "pay_url": f"{base}/pay/{token}",
        "deposit_pence": deposit_pence,
        "package": (package or {}).get("name"),
        "has_stripe_link": bool(package and package.get("stripe_payment_link")),
    }), 201


@app.route("/api/brief/<token>")
def api_brief(token):
    """Machine-readable brief for the CRM's Site Wizard. Protected by a shared
    key (X-Api-Key header) so it isn't world-readable. Returns the structured
    answers + the flattened brief + the client basics."""
    if not ONBOARDING_API_KEY or request.headers.get("X-Api-Key") != ONBOARDING_API_KEY:
        abort(401)
    session = db.get_session(token)
    if not session:
        abort(404)
    q = db.latest_questionnaire(token)
    return jsonify({
        "token": token,
        "client_name": session.get("client_name"),
        "client_email": session.get("client_email"),
        "company": session.get("company"),
        "answers": json.loads(q["answers_json"]) if q else None,
        "brief": q["brief"] if q else None,
    })


# --------------------------------------------------------------------------
# client: agreement + signature
# --------------------------------------------------------------------------

@app.route("/agreement/<token>", methods=["GET"])
def agreement(token):
    cfg = load_config()
    session = db.get_session(token)
    if not session:
        abort(404)

    existing_sig = db.get_signature_for_token(token)
    agreement_row = ensure_agreement(cfg, session)
    package = get_package(cfg, session.get("package_key"))

    if existing_sig:
        # Already signed — show the receipt + pay button, no re-sign.
        return render_template(
            "signed.html", cfg=cfg, session=session, sig=existing_sig,
            package=package, deposit=pounds(session.get("deposit_pence")),
            can_pay=bool(package and package.get("stripe_payment_link")),
        )

    return render_template(
        "agreement.html", cfg=cfg, session=session,
        agreement_html=agreement_row["html"],
        agreement_sha256=agreement_row["content_sha256"],
        package=package, deposit=pounds(session.get("deposit_pence")),
    )


@app.route("/agreement/<token>/sign", methods=["POST"])
def agreement_sign(token):
    cfg = load_config()
    session = db.get_session(token)
    if not session:
        abort(404)

    if db.get_signature_for_token(token):
        # Idempotent: never record a second signature for the same session.
        return redirect(url_for("agreement", token=token))

    agreement_row = ensure_agreement(cfg, session)

    full_name = (request.form.get("full_name") or "").strip()
    intent = request.form.get("intent") == "on"
    signature_png = request.form.get("signature_png") or ""
    # The hash the BROWSER saw, echoed back; must match what we stored.
    shown_hash = request.form.get("agreement_sha256") or ""

    errors = []
    if not full_name:
        errors.append("Please type your full legal name.")
    if not intent:
        errors.append("Please tick the box to confirm your intent to sign.")
    if not signature_png.startswith("data:image"):
        errors.append("Please draw your signature in the box.")
    if shown_hash != agreement_row["content_sha256"]:
        errors.append("The agreement changed since it was loaded. Please reload and try again.")

    if errors:
        package = get_package(cfg, session.get("package_key"))
        return render_template(
            "agreement.html", cfg=cfg, session=session,
            agreement_html=agreement_row["html"],
            agreement_sha256=agreement_row["content_sha256"],
            package=package, deposit=pounds(session.get("deposit_pence")),
            errors=errors,
            form={"full_name": full_name},
        ), 400

    signed_at = now_utc_iso()
    sig_id = db.save_signature(
        token=token,
        agreement_sha256=agreement_row["content_sha256"],
        signer_full_name=full_name,
        signer_email=session.get("client_email"),
        signature_png=signature_png,
        intent_confirmed=intent,
        signed_at_utc=signed_at,
        signer_ip=client_ip(),
        user_agent=request.headers.get("User-Agent", ""),
        certificate_html=None,
        created_at=signed_at,
    )
    db.set_status(token, "signed")

    # Build + store the certificate now that we have the signature row.
    sig = db.get_signature(sig_id)
    cert_html = build_certificate_html(cfg, session, agreement_row, sig)
    # Persist the certificate text onto the (append-only) row via a one-time
    # backfill UPDATE. This is the only mutation we make and it only fills a
    # NULL field on the row we just inserted — the evidential fields are immutable.
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE signatures SET certificate_html = ? WHERE id = ? AND certificate_html IS NULL",
            (cert_html, sig_id),
        )

    return redirect(url_for("agreement", token=token))


@app.route("/agreement/<token>/certificate")
def agreement_certificate(token):
    sig = db.get_signature_for_token(token)
    if not sig:
        abort(404)
    if sig.get("certificate_html"):
        return Response(sig["certificate_html"], mimetype="text/html")
    # Fallback: rebuild on the fly.
    cfg = load_config()
    session = db.get_session(token)
    agreement_row = db.latest_agreement(token)
    return Response(build_certificate_html(cfg, session, agreement_row, sig),
                    mimetype="text/html")


# --------------------------------------------------------------------------
# client: payment handoff (Stripe Payment Link)
# --------------------------------------------------------------------------

@app.route("/pay/<token>")
def pay(token):
    cfg = load_config()
    session = db.get_session(token)
    if not session:
        abort(404)

    # Gate payment behind signing — they should agree before they pay.
    if not db.get_signature_for_token(token):
        return redirect(url_for("agreement", token=token))

    package = get_package(cfg, session.get("package_key"))
    link = (package or {}).get("stripe_payment_link") or ""
    if not link:
        return render_template("no_payment_link.html", cfg=cfg, session=session,
                               package=package), 200

    # Prefill the client's email and tag the payment with our token so the
    # Stripe dashboard / webhook can reconcile it back to this session.
    sep = "&" if "?" in link else "?"
    url = f"{link}{sep}client_reference_id={token}"
    if session.get("client_email"):
        url += f"&prefilled_email={session['client_email']}"
    db.set_status(token, "paid")  # optimistic; confirm via Stripe dashboard/webhook
    return redirect(url)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(host="127.0.0.1", port=port, debug=True)
