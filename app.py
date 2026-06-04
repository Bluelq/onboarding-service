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
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, request, render_template, redirect, url_for, abort,
    Response, jsonify, send_file,
)
from werkzeug.utils import secure_filename

import db
from config import load_config, get_package

app = Flask(__name__)
# Behind a TLS-terminating proxy (Cloudflare tunnel, Render, etc.) — honour the
# X-Forwarded-Proto / -Host headers so generated links use https and the real
# host, and so request.remote_addr reflects the client.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
db.init_db()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
# Shared secret the CRM uses to pull a captured brief server-to-server.
ONBOARDING_API_KEY = os.environ.get("ONBOARDING_API_KEY", "")
# When the service is reached via a proxy on a different domain (e.g. the
# Vercel marketing site rewriting /agreement/* to this backend), set this so
# generated client links use the public domain instead of the backend host.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Client asset uploads (logos, images, content). Stored on the persistent disk
# next to the DB so they survive redeploys on Render (/var/data/uploads).
UPLOAD_DIR = os.environ.get("ONBOARDING_UPLOAD_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(db.DB_PATH)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_UPLOAD_EXT = {
    # images
    "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "tiff", "heic", "ico",
    # docs / content
    "pdf", "doc", "docx", "txt", "rtf", "odt", "csv", "xls", "xlsx", "ppt", "pptx",
    # design / source
    "ai", "psd", "eps", "sketch", "fig", "indd",
    # archives (clients often zip a folder of assets)
    "zip", "rar", "7z",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def now_utc_iso():
    """Server-side UTC timestamp. Never trust the browser clock for signing."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def public_base():
    """Base URL for client-facing links. PUBLIC_BASE_URL wins (proxy/custom
    domain); otherwise fall back to the host the request came in on."""
    return PUBLIC_BASE_URL or request.host_url.rstrip("/")


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


def offers_for_session(cfg, session):
    """The list of package 'offers' to present at the pay step. A session may
    carry a tailored set (offers_json) with per-client price/link overrides
    (e.g. Practice Growth at £300 for one client). Falls back to the single
    session package for back-compatibility."""
    raw = session.get("offers_json")
    offers = []
    if raw:
        try:
            entries = json.loads(raw)
        except Exception:
            entries = []
        for e in entries:
            merged = dict(get_package(cfg, e.get("key")) or {})
            merged.setdefault("key", e.get("key"))
            # Apply per-client overrides (price_label, deposit_pence, stripe link).
            for k, v in (e or {}).items():
                if v not in (None, ""):
                    merged[k] = v
            if merged.get("key"):
                offers.append(merged)
    if not offers:
        pkg = get_package(cfg, session.get("package_key"))
        if pkg:
            offers.append(dict(pkg))
    return offers


def _package_display(cfg, session):
    """(name, price, deposit_display) for the agreement text. When the client
    is offered a choice, the wording stays package-agnostic ('the selected
    package') since they pick — and pay the exact amount — at checkout."""
    offers = offers_for_session(cfg, session)
    if len(offers) > 1:
        return (
            "selected",
            "the price shown for the selected package at checkout",
            "the amount shown for the selected package at checkout",
        )
    pkg = offers[0] if offers else {}
    return (
        pkg.get("name") or "the agreed",
        pkg.get("price_label") or "",
        pounds(pkg.get("deposit_pence") if pkg else session.get("deposit_pence")),
    )


def agreement_subs(cfg, session):
    """The placeholder values used across the agreement intro, clauses, and
    footer note. Centralised so the page and the hashed body stay consistent."""
    biz = cfg["business"]
    company = (session.get("company") or "").strip()
    name, price, deposit_display = _package_display(cfg, session)
    return {
        "business_legal_name": biz.get("legal_name", biz.get("trading_name", "")),
        "client_name": session.get("client_name") or "the Client",
        "client_company_clause": f" of {company}" if company else "",
        "date": (session.get("created_at") or "")[:10],
        "package_name": name,
        "package_price": price,
        "deposit_amount": deposit_display,
    }


def fill_text(text, subs):
    for k, v in subs.items():
        text = (text or "").replace("{{" + k + "}}", str(v))
    return text


def render_agreement_body(cfg, session):
    """Deterministically render the agreement intro + clauses to HTML with the
    client's details filled in. The EXACT string this returns is what we hash
    and what the client signs, so it must be stable for a given session + config."""
    ag = cfg["agreement"]
    subs = agreement_subs(cfg, session)
    parts = ['<div class="agreement-body">']
    parts.append(f'<p class="agreement-intro">{fill_text(ag["intro"], subs)}</p>')
    for clause in ag.get("clauses", []):
        parts.append(f'<h3>{fill_text(clause["heading"], subs)}</h3>')
        parts.append(f'<p>{fill_text(clause["body"], subs)}</p>')
    parts.append("</div>")
    return "\n".join(parts)


def ensure_agreement(cfg, session):
    """Return the stored agreement row for this session, creating it on first
    view so the signed hash is locked to the version the client first saw."""
    existing = db.latest_agreement(session["token"])
    if existing:
        return existing
    body_html = render_agreement_body(cfg, session)
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


# ----- legal pages (public) -----------------------------------------------

@app.route("/terms")
def terms():
    return render_template("legal_terms.html", cfg=load_config())


@app.route("/privacy")
def privacy():
    return render_template("legal_privacy.html", cfg=load_config())


@app.route("/cancellation")
def cancellation():
    return render_template("legal_cancellation.html", cfg=load_config())


# --------------------------------------------------------------------------
# admin
# --------------------------------------------------------------------------

@app.route("/admin")
@require_admin
def admin():
    cfg = load_config()
    sessions = db.list_sessions()
    base = public_base()
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
    uploads = db.list_uploads(token)
    for f in uploads:
        f["_size"] = _human_size(f.get("size_bytes"))
    base = public_base()
    return render_template(
        "admin_detail.html", cfg=cfg, session=session, answers=answers,
        brief=(q["brief"] if q else None), sig=sig, uploads=uploads,
        base=base, pounds=pounds,
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

    # Two ways to specify what the client can buy:
    #   - "offers": [ {key, price_label?, deposit_pence?, stripe_payment_link?}, ... ]
    #       → present these as selectable options at checkout (per-client pricing).
    #   - "package_key": "growth"  → single package (back-compat).
    offers_in = data.get("offers")
    offers_json = None
    package_key = (data.get("package_key") or "").strip()

    if isinstance(offers_in, list) and offers_in:
        # Keep only known fields per entry; ignore anything without a key.
        clean = []
        for e in offers_in:
            if not isinstance(e, dict) or not e.get("key"):
                continue
            entry = {"key": e["key"]}
            for f in ("price_label", "deposit_pence", "stripe_payment_link", "name"):
                if e.get(f) not in (None, ""):
                    entry[f] = e[f]
            clean.append(entry)
        if clean:
            offers_json = json.dumps(clean)
            package_key = package_key or clean[0]["key"]

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
        offers_json=offers_json,
        created_at=now_utc_iso(),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=expiry_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    base = public_base()
    session = db.get_session(token)
    resolved = offers_for_session(cfg, session)
    return jsonify({
        "token": token,
        "questionnaire_url": f"{base}/onboard/{token}",
        "agreement_url": f"{base}/agreement/{token}",
        "pay_url": f"{base}/pay/{token}",
        "offers": [{"key": o.get("key"), "name": o.get("name"),
                    "price_label": o.get("price_label"),
                    "has_link": bool(o.get("stripe_payment_link"))} for o in resolved],
        "package": (package or {}).get("name"),
        "has_stripe_link": any(o.get("stripe_payment_link") for o in resolved),
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
    offers = offers_for_session(cfg, session)
    # Attach a display deposit for each offer card.
    for o in offers:
        o["_deposit"] = pounds(o.get("deposit_pence"))

    if existing_sig:
        # Already signed — show the receipt + the package choice + pay.
        return render_template(
            "signed.html", cfg=cfg, session=session, sig=existing_sig,
            offers=offers,
        )

    return render_template(
        "agreement.html", cfg=cfg, session=session,
        agreement_html=agreement_row["html"],
        agreement_sha256=agreement_row["content_sha256"],
        offers=offers,
        footer_note=fill_text(cfg["agreement"].get("footer_note", ""), agreement_subs(cfg, session)),
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
        offers = offers_for_session(cfg, session)
        for o in offers:
            o["_deposit"] = pounds(o.get("deposit_pence"))
        return render_template(
            "agreement.html", cfg=cfg, session=session,
            agreement_html=agreement_row["html"],
            agreement_sha256=agreement_row["content_sha256"],
            offers=offers,
            footer_note=fill_text(cfg["agreement"].get("footer_note", ""), agreement_subs(cfg, session)),
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

    offers = offers_for_session(cfg, session)
    # Which package did they pick? ?pkg=<key>; default to the only/first offer.
    chosen_key = request.args.get("pkg")
    package = None
    if chosen_key:
        package = next((o for o in offers if o.get("key") == chosen_key), None)
    if package is None:
        package = offers[0] if offers else None

    link = (package or {}).get("stripe_payment_link") or ""
    if not link:
        return render_template("no_payment_link.html", cfg=cfg, session=session,
                               package=package), 200

    # Prefill the client's email and tag the payment with our token + chosen
    # package so the Stripe dashboard / webhook can reconcile it back.
    sep = "&" if "?" in link else "?"
    ref = token if not package.get("key") else f"{token}:{package.get('key')}"
    url = f"{link}{sep}client_reference_id={ref}"
    if session.get("client_email"):
        url += f"&prefilled_email={session['client_email']}"
    db.set_status(token, "paid")  # optimistic; confirm via Stripe dashboard/webhook
    return redirect(url)


# --------------------------------------------------------------------------
# client: asset uploads (logos, images, content)
# --------------------------------------------------------------------------

def _ext_ok(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_UPLOAD_EXT


def _human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


@app.errorhandler(413)
def too_large(_e):
    return ("That upload is larger than the %d MB limit. Please upload fewer or "
            "smaller files at a time (or zip them)." % MAX_UPLOAD_MB), 413


@app.route("/upload/<token>", methods=["GET"])
def upload_page(token):
    cfg = load_config()
    session = db.get_session(token)
    if not session:
        abort(404)
    files = db.list_uploads(token)
    for f in files:
        f["_size"] = _human_size(f.get("size_bytes"))
    try:
        saved = int(request.args.get("saved", 0))
    except ValueError:
        saved = 0
    rejected = [r for r in (request.args.get("rejected", "").split(",")) if r]
    return render_template(
        "upload.html", cfg=cfg, session=session, files=files,
        max_mb=MAX_UPLOAD_MB, saved=saved, rejected=rejected,
        allowed_hint="images, PDFs, Office docs, design files, and zips",
    )


@app.route("/upload/<token>", methods=["POST"])
def upload_submit(token):
    session = db.get_session(token)
    if not session:
        abort(404)
    incoming = request.files.getlist("files")
    saved, rejected = 0, []
    dest_dir = os.path.join(UPLOAD_DIR, token)
    os.makedirs(dest_dir, exist_ok=True)
    for fs in incoming:
        if not fs or not fs.filename:
            continue
        if not _ext_ok(fs.filename):
            rejected.append(fs.filename)
            continue
        safe = secure_filename(fs.filename) or "file"
        stored = f"{secrets.token_hex(8)}_{safe}"
        fs.save(os.path.join(dest_dir, stored))
        size = os.path.getsize(os.path.join(dest_dir, stored))
        db.add_upload(token, fs.filename, f"{token}/{stored}", fs.mimetype, size, now_utc_iso())
        saved += 1
    from urllib.parse import urlencode
    qs = urlencode({"saved": saved, "rejected": ",".join(rejected)})
    return redirect(url_for("upload_page", token=token) + ("?" + qs if (saved or rejected) else ""))


@app.route("/upload/<token>/delete/<int:upload_id>", methods=["POST"])
def upload_delete(token, upload_id):
    up = db.get_upload(upload_id)
    if not up or up.get("token") != token:
        abort(404)
    try:
        os.remove(os.path.join(UPLOAD_DIR, up["stored_name"]))
    except OSError:
        pass
    db.delete_upload(upload_id)
    return redirect(url_for("upload_page", token=token))


@app.route("/admin/uploads/<int:upload_id>")
@require_admin
def admin_download_upload(upload_id):
    up = db.get_upload(upload_id)
    if not up:
        abort(404)
    path = os.path.join(UPLOAD_DIR, up["stored_name"])
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=up.get("original_name") or "file")


def _build_assets_zip(token):
    """Build an in-memory zip of a session's uploads. Returns a send_file
    response, or None if there are no files."""
    files = db.list_uploads(token)
    if not files:
        return None
    session = db.get_session(token)
    label = ((session or {}).get("client_name") or token).replace(" ", "_") or token
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        seen = {}
        for f in files:
            path = os.path.join(UPLOAD_DIR, f["stored_name"])
            if not os.path.exists(path):
                continue
            name = f.get("original_name") or os.path.basename(f["stored_name"])
            if name in seen:
                seen[name] += 1
                base, dot, ext = name.rpartition(".")
                name = f"{base}_{seen[name]}{dot}{ext}" if dot else f"{name}_{seen[name]}"
            else:
                seen[name] = 0
            zf.write(path, arcname=name)
    mem.seek(0)
    return send_file(mem, mimetype="application/zip", as_attachment=True,
                     download_name=f"assets_{label}.zip")


@app.route("/admin/sessions/<token>/uploads.zip")
@require_admin
def admin_uploads_zip(token):
    resp = _build_assets_zip(token)
    if resp is None:
        abort(404)
    return resp


# --------------------------------------------------------------------------
# CRM control panel: keyed read API (list sessions, download assets)
# --------------------------------------------------------------------------

@app.route("/api/sessions")
def api_sessions():
    """List sessions with onboarding status for the CRM dashboard. Keyed."""
    if not ONBOARDING_API_KEY or request.headers.get("X-Api-Key") != ONBOARDING_API_KEY:
        abort(401)
    base = public_base()
    out = []
    for s in db.list_sessions(limit=500):
        tok = s["token"]
        sig = db.get_signature_for_token(tok)
        n_uploads = len(db.list_uploads(tok))
        out.append({
            "token": tok,
            "client_name": s.get("client_name"),
            "client_email": s.get("client_email"),
            "company": s.get("company"),
            "status": s.get("status"),
            "created_at": s.get("created_at"),
            "crm_lead_id": s.get("crm_lead_id"),
            "signed": bool(sig),
            "signed_by": sig.get("signer_full_name") if sig else None,
            "signed_at": sig.get("signed_at_utc") if sig else None,
            "paid": s.get("status") == "paid",
            "upload_count": n_uploads,
            "urls": {
                "questionnaire": f"{base}/onboard/{tok}",
                "agreement": f"{base}/agreement/{tok}",
                "upload": f"{base}/upload/{tok}",
                "certificate": (f"{base}/agreement/{tok}/certificate" if sig else None),
            },
        })
    return jsonify({"sessions": out})


@app.route("/api/sessions/<token>/assets.zip")
def api_session_assets(token):
    """Keyed zip download of a session's uploaded assets (CRM proxies this)."""
    if not ONBOARDING_API_KEY or request.headers.get("X-Api-Key") != ONBOARDING_API_KEY:
        abort(401)
    resp = _build_assets_zip(token)
    if resp is None:
        abort(404)
    return resp


@app.route("/api/sessions/<token>", methods=["DELETE"])
def api_delete_session(token):
    """Delete an UNSIGNED session (abandoned/test) and its files. Signed
    sessions are refused — a signed agreement is a legal record."""
    if not ONBOARDING_API_KEY or request.headers.get("X-Api-Key") != ONBOARDING_API_KEY:
        abort(401)
    if not db.get_session(token):
        abort(404)
    if db.get_signature_for_token(token):
        return jsonify({"error": "This session is signed and cannot be deleted (legal record)."}), 409
    import shutil
    folder = os.path.join(UPLOAD_DIR, token)
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
    db.delete_session_cascade(token)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    app.run(host="127.0.0.1", port=port, debug=True)
