"""
Configuration loader for the onboarding service.

Everything client-facing — your business details, the discovery questions, your
packages/pricing, the Stripe Payment Links, and the agreement wording — lives in
config.json so you can edit it WITHOUT touching code. config.example.json ships
with sensible defaults; copy it to config.json and adjust.

Secrets (Stripe is just a link, but the admin password and any SMTP creds) come
from environment variables, never config.json.
"""

import os
import json

_HERE = os.path.dirname(__file__)
CONFIG_PATH = os.environ.get("ONBOARDING_CONFIG", os.path.join(_HERE, "config.json"))
EXAMPLE_PATH = os.path.join(_HERE, "config.example.json")


def load_config():
    """Load config.json, falling back to config.example.json so the service
    still boots out-of-the-box for a demo."""
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_PATH
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_source"] = path
    return cfg


def get_package(cfg, package_key):
    for p in cfg.get("packages", []):
        if p.get("key") == package_key:
            return p
    return None
