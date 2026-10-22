import sqlite3, json, hashlib
from pathlib import Path

DB = Path("mailroom.db")

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS evaluations (
          evaluation_id TEXT PRIMARY KEY,
          input_digest TEXT NOT NULL,
          verifier_jwk TEXT NOT NULL,
          propose_response TEXT NOT NULL,
          committed INTEGER DEFAULT 0,
          commit_response TEXT
        );

        CREATE TABLE IF NOT EXISTS proposals (
          evaluation_id TEXT NOT NULL,
          dossier_id TEXT NOT NULL,
          content_fingerprint TEXT NOT NULL,
          call_id TEXT NOT NULL,
          proposal_json TEXT NOT NULL,
          proposal_digest TEXT NOT NULL,
          PRIMARY KEY (evaluation_id, dossier_id)
        );

        CREATE TABLE IF NOT EXISTS stable_cache (
          content_fingerprint TEXT PRIMARY KEY,
          proposal_json TEXT NOT NULL,
          call_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS receipts (
          evaluation_id TEXT NOT NULL,
          call_id TEXT NOT NULL,
          receipt_json TEXT NOT NULL,
          PRIMARY KEY (evaluation_id, call_id)
        );
        """)

def fingerprint_dossier(d: dict) -> str:
    return hashlib.sha256(
        json.dumps(d, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode()
    ).hexdigest()