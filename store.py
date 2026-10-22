import os
import json
import hashlib

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=280,
    )
else:
    engine = create_engine("sqlite:///mailroom.db")


def init():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS evaluations (
                evaluation_id TEXT PRIMARY KEY,
                input_digest TEXT NOT NULL,
                verifier_jwk TEXT NOT NULL,
                propose_response TEXT NOT NULL,
                committed BOOLEAN DEFAULT FALSE,
                commit_response TEXT
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS proposals (
                evaluation_id TEXT NOT NULL,
                dossier_id TEXT NOT NULL,
                content_fingerprint TEXT NOT NULL,
                call_id TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                proposal_digest TEXT NOT NULL,
                PRIMARY KEY (evaluation_id, dossier_id)
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS stable_cache (
                content_fingerprint TEXT PRIMARY KEY,
                proposal_json TEXT NOT NULL,
                call_id TEXT NOT NULL
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS receipts (
                evaluation_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY (evaluation_id, call_id)
            )
        """))


def fingerprint_dossier(dossier):
    return hashlib.sha256(
        json.dumps(
            dossier,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
