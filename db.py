"""
SQLite storage for the client onboarding service.

This database is DELIBERATELY separate from the CRM's leads.db. The onboarding
service is internet-facing; the CRM is not. Keeping the stores apart means a
compromise of the public service never exposes the lead database.

Tables
------
sessions                 one onboarding link per client (token = the URL slug)
questionnaire_responses  the discovery answers captured during/after the call
agreements               the exact agreement HTML a client was shown, hashed
signatures               APPEND-ONLY audit trail — the legal record of signing

The signatures table is never updated or deleted in normal operation. Each row
is a self-contained evidential record: who signed, when (server clock), from
where (IP/user-agent), what they intended, and a SHA-256 of the precise
document version they agreed to. That bundle is what gives a simple electronic
signature its evidential weight under UK law (Electronic Communications Act
2000 s.7 + retained eIDAS).
"""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "ONBOARDING_DB",
    os.path.join(os.path.dirname(__file__), "onboarding.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT PRIMARY KEY,
    client_name     TEXT,
    client_email    TEXT,
    company         TEXT,
    package_key     TEXT,
    deposit_pence   INTEGER,
    currency        TEXT DEFAULT 'gbp',
    status          TEXT DEFAULT 'created',   -- created|questionnaire|signed|paid
    crm_lead_id     TEXT,                      -- optional link back to the CRM
    notes           TEXT,
    offers_json     TEXT,                      -- optional list of packages to present at pay (per-client pricing)
    created_at      TEXT NOT NULL,
    expires_at      TEXT
);

CREATE TABLE IF NOT EXISTS questionnaire_responses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL,
    answers_json TEXT NOT NULL,
    brief       TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (token) REFERENCES sessions(token)
);

CREATE TABLE IF NOT EXISTS agreements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    version         TEXT,
    html            TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    deposit_pence   INTEGER,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (token) REFERENCES sessions(token)
);

-- Client-uploaded assets (logos, images, content) keyed to a session.
CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT NOT NULL,
    original_name TEXT,
    stored_name   TEXT NOT NULL,   -- path relative to UPLOAD_DIR (token/uuid.ext)
    content_type  TEXT,
    size_bytes    INTEGER,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (token) REFERENCES sessions(token)
);

-- APPEND-ONLY. Do not UPDATE or DELETE rows here.
CREATE TABLE IF NOT EXISTS signatures (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    token             TEXT NOT NULL,
    agreement_sha256  TEXT NOT NULL,   -- proves which document version was signed
    signer_full_name  TEXT NOT NULL,
    signer_email      TEXT,
    signature_png     TEXT,            -- base64 data URL of the drawn signature
    intent_confirmed  INTEGER NOT NULL DEFAULT 0,
    signed_at_utc     TEXT NOT NULL,   -- SERVER clock, ISO8601 Z
    signer_ip         TEXT,
    user_agent        TEXT,
    certificate_html  TEXT,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (token) REFERENCES sessions(token)
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Safe migration: add offers_json to sessions tables created before it existed.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "offers_json" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN offers_json TEXT")


# ----- sessions -----------------------------------------------------------

def create_session(token, client_name, client_email, company, package_key,
                   deposit_pence, currency, crm_lead_id, notes,
                   created_at, expires_at, offers_json=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sessions
               (token, client_name, client_email, company, package_key,
                deposit_pence, currency, status, crm_lead_id, notes,
                offers_json, created_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (token, client_name, client_email, company, package_key,
             deposit_pence, currency, "created", crm_lead_id, notes,
             offers_json, created_at, expires_at),
        )


def get_session(token):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        return dict(row) if row else None


def list_sessions(limit=200):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def set_status(token, status):
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET status = ? WHERE token = ?", (status, token)
        )


# ----- questionnaire ------------------------------------------------------

def save_questionnaire(token, answers_json, brief, created_at):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO questionnaire_responses (token, answers_json, brief, created_at)
               VALUES (?,?,?,?)""",
            (token, answers_json, brief, created_at),
        )


def latest_questionnaire(token):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM questionnaire_responses
               WHERE token = ? ORDER BY id DESC LIMIT 1""",
            (token,),
        ).fetchone()
        return dict(row) if row else None


# ----- agreements ---------------------------------------------------------

def save_agreement(token, version, html, content_sha256, deposit_pence, created_at):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agreements
               (token, version, html, content_sha256, deposit_pence, created_at)
               VALUES (?,?,?,?,?,?)""",
            (token, version, html, content_sha256, deposit_pence, created_at),
        )


def latest_agreement(token):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agreements WHERE token = ? ORDER BY id DESC LIMIT 1",
            (token,),
        ).fetchone()
        return dict(row) if row else None


# ----- signatures (append-only) ------------------------------------------

def save_signature(token, agreement_sha256, signer_full_name, signer_email,
                   signature_png, intent_confirmed, signed_at_utc, signer_ip,
                   user_agent, certificate_html, created_at):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO signatures
               (token, agreement_sha256, signer_full_name, signer_email,
                signature_png, intent_confirmed, signed_at_utc, signer_ip,
                user_agent, certificate_html, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (token, agreement_sha256, signer_full_name, signer_email,
             signature_png, 1 if intent_confirmed else 0, signed_at_utc,
             signer_ip, user_agent, certificate_html, created_at),
        )
        return cur.lastrowid


def get_signature_for_token(token):
    """The first (and normally only) signature recorded for a session."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM signatures WHERE token = ? ORDER BY id ASC LIMIT 1",
            (token,),
        ).fetchone()
        return dict(row) if row else None


def get_signature(sig_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM signatures WHERE id = ?", (sig_id,)
        ).fetchone()
        return dict(row) if row else None


# ----- uploads ------------------------------------------------------------

def add_upload(token, original_name, stored_name, content_type, size_bytes, created_at):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO uploads
               (token, original_name, stored_name, content_type, size_bytes, created_at)
               VALUES (?,?,?,?,?,?)""",
            (token, original_name, stored_name, content_type, size_bytes, created_at),
        )
        return cur.lastrowid


def list_uploads(token):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM uploads WHERE token = ? ORDER BY id ASC", (token,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_upload(upload_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        return dict(row) if row else None


def delete_upload(upload_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
