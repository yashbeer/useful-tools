#!/usr/bin/env python3
"""sparrow - a single-file email campaign runner.

Storage is SQLite; campaign copy is authored in campaign.toml and reloaded
every tick. One email per tick by default, so the tick is the rate limiter.
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import imaplib
import json
import os
import re
import smtplib
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SPARROW_DB", os.path.join(HERE, "sparrow.db"))
CFG_PATH = os.environ.get("SPARROW_CONFIG", os.path.join(HERE, "campaign.toml"))
TS = "%Y-%m-%dT%H:%M:%SZ"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MERGE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
CONTACT_COLS = ("email", "firstname", "lastname", "company", "designation", "phone")
CORE = set(CONTACT_COLS)
C_SELECT = ", ".join(f"c.{c}" for c in CONTACT_COLS)
ENROLL_FIELDS = {"campaign_id", "status", "step", "next_at"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS)


def env(key, default=None):
    """Env var, treating empty string as unset."""
    v = os.environ.get(key)
    return v if v not in (None, "") else default


def load_env_file(path):
    """Read an env file the way systemd's EnvironmentFile= does, so a local run
    and a unit run see identical values. Real env vars always win."""
    if not os.path.exists(path):
        return None
    n = 0
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line[0] in "#;":      # systemd: only whole-line comments
            continue
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) > 1 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:     # never clobber the real environment
            os.environ[key] = val
            n += 1
    return n


def require_env(key, hint=""):
    v = env(key)
    if v is None:
        raise SystemExit(f"{key} is not set{hint} - see .env.example")
    return v


def check_transport():
    """Fail at startup, not at the first send hours later."""
    missing = []
    if not env("SMTP_HOST"):
        missing.append("SMTP_HOST")
    if not env("IMAP_HOST"):
        missing.append("IMAP_HOST      (or IMAP_HOST=off to disable reply polling)")
    if not env("SMTP_PASS") and (env("SMTP_SECURITY") or "").lower() != "none":
        missing.append("SMTP_PASS      (or SMTP_SECURITY=none for a local relay)")
    if missing:
        raise SystemExit("missing required environment:\n  " + "\n  ".join(missing)
                         + "\nsee .env.example")


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- db

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  id          INTEGER PRIMARY KEY,
  email       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  firstname   TEXT NOT NULL DEFAULT '',
  lastname    TEXT NOT NULL DEFAULT '',
  company     TEXT NOT NULL DEFAULT '',
  designation TEXT NOT NULL DEFAULT '',
  phone       TEXT NOT NULL DEFAULT '',
  extra      TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS enrollments (
  id           INTEGER PRIMARY KEY,
  contact_id   INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  campaign_id  TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active',
  step         INTEGER NOT NULL DEFAULT 0,
  next_at      TEXT,
  last_sent_at TEXT,
  thread_msgid TEXT,
  UNIQUE(contact_id, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_due ON enrollments(status, next_at);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY,
  contact_id INTEGER,
  campaign_id TEXT,
  type       TEXT NOT NULL,
  step       INTEGER,
  msgid      TEXT,
  detail     TEXT,
  at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(type, at);

CREATE TABLE IF NOT EXISTS suppressions (
  email  TEXT PRIMARY KEY COLLATE NOCASE,
  reason TEXT,
  at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def migrate(db):
    """Bring an existing contacts table up to CONTACT_COLS. Idempotent."""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(contacts)")}
    if "name" in cols and "firstname" not in cols:
        db.execute("ALTER TABLE contacts RENAME COLUMN name TO firstname")
        cols.discard("name")
        cols.add("firstname")
        log("migrated: contacts.name -> contacts.firstname")
    for col in CONTACT_COLS:
        if col not in cols:
            db.execute(f"ALTER TABLE contacts ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            log(f"migrated: added contacts.{col}")

    for table in ("enrollments", "events"):
        names = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        if "campaign" in names and "campaign_id" not in names:
            db.execute(f"ALTER TABLE {table} RENAME COLUMN campaign TO campaign_id")
            log(f"migrated: {table}.campaign -> {table}.campaign_id")


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    migrate(db)
    return db


def event(db, contact_id, campaign_id, type_, step=None, msgid=None, detail=None):
    db.execute(
        "INSERT INTO events (contact_id,campaign_id,type,step,msgid,detail) "
        "VALUES (?,?,?,?,?,?)",
        (contact_id, campaign_id, type_, step, msgid, detail),
    )


# ----------------------------------------------------------------------- config


def load_cfg(path=CFG_PATH) -> dict:
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    if not cfg.get("steps"):
        raise SystemExit(f"{path}: no [[steps]] defined")
    if not cfg.get("id"):
        raise SystemExit(f"{path}: no id - add a stable id, e.g. id = \"outreach\". "
                         "It keys enrollments; name is a free-text label you can change.")
    return cfg


def setting(db, cfg, key, default=None, cast=str):
    """DB override beats campaign.toml beats the built-in default."""
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is not None:
        raw = row["value"]
    elif key in cfg:
        raw = cfg[key]
    elif key in cfg.get("window", {}):
        raw = cfg["window"][key]
    else:
        return default
    if cast is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def tzinfo(cfg) -> ZoneInfo:
    return ZoneInfo(cfg.get("window", {}).get("tz", "UTC"))


def in_window(db, cfg) -> bool:
    w = cfg.get("window", {})
    local = datetime.now(tzinfo(cfg))
    if DAYS[local.weekday()] not in w.get("days", DAYS):
        return False
    start = setting(db, cfg, "window_start", w.get("start", "00:00"))
    end = setting(db, cfg, "window_end", w.get("end", "23:59"))
    return start <= local.strftime("%H:%M") <= end


def sent_today(db, cfg) -> int:
    local = datetime.now(tzinfo(cfg)).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.execute(
        "SELECT count(*) c FROM events WHERE type='sent' AND at >= ?", (iso(local),)
    ).fetchone()["c"]


# -------------------------------------------------------------------- rendering


def fields_of(row) -> dict:
    out = {k: (row[k] or "") for k in CONTACT_COLS}
    try:
        out.update(json.loads(row["extra"] or "{}"))
    except json.JSONDecodeError:
        pass
    return {k: ("" if v is None else str(v)) for k, v in out.items()}


def render(tpl: str, fields: dict):
    """Substitute {{merge}} tags. Returns (text, [names that were empty])."""
    missing = []

    def sub(m):
        key = m.group(1)
        val = fields.get(key, "")
        if not val:
            missing.append(key)
        return val

    return MERGE.sub(sub, tpl), missing


# ------------------------------------------------------------------------ smtp


def smtp_connect(cfg):
    host = require_env("SMTP_HOST")
    port = int(env("SMTP_PORT", 587))
    security = env("SMTP_SECURITY", "ssl" if port == 465 else "starttls").lower()
    user = env("SMTP_USER", email.utils.parseaddr(cfg["from_addr"])[1])
    password = env("SMTP_PASS")

    if security == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
        if security == "starttls":
            s.starttls()
    if password:
        s.login(user, password)
    elif security != "none":
        raise SystemExit("SMTP_PASS is not set "
                         "(use SMTP_SECURITY=none for an unauthenticated local relay)")
    return s


def build_message(cfg, step, fields, thread_msgid):
    subject_tpl = step.get("subject", "").strip()
    if subject_tpl:
        subject, miss_s = render(subject_tpl, fields)
    else:  # blank subject => threaded follow-up on step 1's subject
        subject, miss_s = render(cfg["steps"][0].get("subject", ""), fields)
        subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    body, miss_b = render(step.get("body", ""), fields)

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = fields["email"]
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    from_email = email.utils.parseaddr(cfg["from_addr"])[1]
    msg["Message-ID"] = email.utils.make_msgid(domain=from_email.split("@")[-1])
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    unsub = cfg.get("unsubscribe_mailto", from_email)
    msg["List-Unsubscribe"] = f"<mailto:{unsub}?subject=unsubscribe>"
    if thread_msgid:
        msg["In-Reply-To"] = thread_msgid
        msg["References"] = thread_msgid
    msg.set_content(body)
    return msg, sorted(set(miss_s + miss_b))


# ------------------------------------------------------------------------ tick


def due_rows(db, cfg, limit):
    return db.execute(
        f"""
        SELECT c.id contact_id, {C_SELECT}, c.extra,
               e.id enr_id, e.campaign_id, e.step, e.next_at, e.thread_msgid
        FROM enrollments e JOIN contacts c ON c.id = e.contact_id
        WHERE e.status = 'active'
          AND e.campaign_id = ?
          AND (e.next_at IS NULL OR e.next_at <= ?)
          AND c.email NOT IN (SELECT email FROM suppressions)
        ORDER BY e.next_at IS NOT NULL, e.next_at
        LIMIT ?
        """,
        (cfg["id"], iso(now()), limit),
    ).fetchall()


def idle_reason(db, cfg):
    """Why a real send would do nothing right now, or None if it would send."""
    if setting(db, cfg, "paused", False, bool):
        return "paused (settings override)"
    if not in_window(db, cfg):
        w = cfg.get("window", {})
        return (f"outside send window {w.get('start','00:00')}-{w.get('end','23:59')} "
                f"{','.join(w.get('days', []))} {w.get('tz','UTC')}")
    if setting(db, cfg, "max_per_day", 180, int) - sent_today(db, cfg) <= 0:
        return f"daily budget spent ({sent_today(db, cfg)})"
    return None


def send_due(db, cfg, dry=False, _last=[None], _seen=[None]):
    steps = cfg["steps"]
    reason = idle_reason(db, cfg)
    out = []  # dry-run buffers its preview so identical ticks stay quiet

    if dry:  # a preview is not a send - never gate it on the clock
        if reason:
            out.append(f"note: a real run would idle here - {reason}")
    elif reason:
        if reason != _last[0]:      # log the transition, not every tick
            log(f"idle: {reason}")
            _last[0] = reason
        return 0
    else:
        _last[0] = None

    budget = setting(db, cfg, "max_per_day", 180, int) - sent_today(db, cfg)
    per_tick = setting(db, cfg, "sends_per_tick", 1, int)
    n = 25 if dry else max(0, min(budget, per_tick))  # dry-run previews the queue
    if n == 0:
        return 0

    rows = due_rows(db, cfg, n)
    if not rows:
        return 0

    smtp = None if dry else smtp_connect(cfg)
    sent = 0
    try:
        for row in rows:
            idx = min(row["step"], len(steps) - 1)  # drift clamp: reuse last copy
            fields = fields_of(row)
            msg, missing = build_message(cfg, steps[idx], fields, row["thread_msgid"])

            if missing and cfg.get("require_merge_fields", True):
                if not dry:
                    db.execute("UPDATE enrollments SET status='error' WHERE id=?",
                               (row["enr_id"],))
                    event(db, row["contact_id"], row["campaign_id"], "error", idx,
                          detail="empty merge fields: " + ",".join(missing))
                (out.append if dry else log)(
                    f"SKIP {row['email']} step {idx}: empty merge fields {missing}")
                continue

            if dry:  # read-only: render it, show it, change nothing
                due = row["next_at"] or "now"
                out.append(f"[dry] -> {row['email']}  step {idx}  due {due}")
                out.append(f"       subject: {msg['Subject']}")
                out.extend(f"       | {line}"
                           for line in msg.get_content().strip().splitlines())
                sent += 1
                continue

            smtp.send_message(msg)

            new_step = row["step"] + 1
            done = new_step >= len(steps)
            next_at = None
            if not done:
                after = float(steps[new_step].get("after_days", 1))
                next_at = iso(now() + timedelta(days=after))
            db.execute(
                """UPDATE enrollments
                   SET step=?, status=?, next_at=?, last_sent_at=?,
                       thread_msgid = COALESCE(thread_msgid, ?)
                   WHERE id=?""",
                (new_step, "done" if done else "active", next_at, iso(now()),
                 msg["Message-ID"], row["enr_id"]),
            )
            event(db, row["contact_id"], row["campaign_id"], "sent", idx, msg["Message-ID"])
            sent += 1
            log(f"sent -> {row['email']} step {idx}" + (" (sequence done)" if done else ""))
    finally:
        if smtp:
            try:
                smtp.quit()
            except Exception:
                pass

    if dry and out:
        fingerprint = hashlib.sha256("\n".join(out).encode()).hexdigest()
        if fingerprint != _seen[0]:   # re-render only when copy or queue changes
            _seen[0] = fingerprint
            for line in out:
                log(line)
            log(f"[dry] {sent} due - quiet until campaign.toml or the queue changes")
    return sent


# ---------------------------------------------------------------------- replies


def hdr(raw) -> str:
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:
        return raw or ""


def poll_replies(db, cfg):
    host = require_env("IMAP_HOST", " (use IMAP_HOST=off to disable reply polling)")
    if host.lower() in ("off", "none"):
        return
    port = int(env("IMAP_PORT", 993))
    user = env("IMAP_USER") or env("SMTP_USER") or email.utils.parseaddr(cfg["from_addr"])[1]
    password = env("IMAP_PASS") or env("SMTP_PASS")
    if not password:
        return
    days = int(cfg.get("imap_lookback_days", 2))
    since = (now() - timedelta(days=days)).strftime("%d-%b-%Y")

    by_addr = {r["email"].lower(): r["id"]
               for r in db.execute("SELECT id,email FROM contacts")}
    by_msgid = {r["thread_msgid"]: r["contact_id"]
                for r in db.execute(
                    "SELECT thread_msgid,contact_id FROM enrollments WHERE thread_msgid IS NOT NULL")}

    m = imaplib.IMAP4_SSL(host, port, timeout=30)
    try:
        m.login(user, password)
        m.select("INBOX", readonly=True)
        typ, data = m.search(None, "SINCE", since)
        if typ != "OK":
            return
        ids = data[0].split()
        for uid in ids:
            typ, parts = m.fetch(
                uid, "(INTERNALDATE BODY.PEEK[HEADER.FIELDS "
                     "(FROM SUBJECT DATE IN-REPLY-TO REFERENCES)])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            raw = parts[0][1].decode("utf-8", "replace")
            head = {}
            for line in raw.splitlines():
                if ":" in line and not line.startswith((" ", "\t")):
                    k, v = line.split(":", 1)
                    head[k.strip().lower()] = v.strip()
            sender = email.utils.parseaddr(hdr(head.get("from", "")))[1].lower()
            arrived = message_time(parts[0][0], head)

            # A thread reference can only exist if we mailed them first, so it
            # needs no time check. A bare address match does: the inbox is full
            # of mail that predates the campaign.
            refs = (head.get("in-reply-to", "") + " " + head.get("references", ""))
            cid = next((c for mid, c in by_msgid.items() if mid and mid in refs), None)
            after_send = cid is not None

            if cid is None:
                cid = by_addr.get(sender)
            if cid is None:
                if any(t in sender for t in ("mailer-daemon", "postmaster")):
                    mark_bounce(db, m, uid, by_addr)
                continue

            changed = db.execute(
                """UPDATE enrollments SET status='replied'
                   WHERE contact_id=? AND status='active'
                     AND (? OR (last_sent_at IS NOT NULL AND last_sent_at <= ?))""",
                (cid, after_send, arrived),
            ).rowcount
            if changed:
                event(db, cid, None, "replied", detail=hdr(head.get("subject", ""))[:200])
                log(f"reply <- {sender} ({changed} sequence stopped)")
    finally:
        try:
            m.logout()
        except Exception:
            pass


def message_time(prefix, head):
    """When the server received it. INTERNALDATE beats a forgeable Date header."""
    try:
        t = imaplib.Internaldate2tuple(prefix)
        if t:
            return iso(datetime.fromtimestamp(time.mktime(t)).astimezone())
    except Exception:
        pass
    try:
        return iso(email.utils.parsedate_to_datetime(head.get("date", "")))
    except Exception:
        return iso(now())


def mark_bounce(db, m, uid, by_addr):
    typ, parts = m.fetch(uid, "(BODY.PEEK[TEXT])")
    if typ != "OK" or not parts or not isinstance(parts[0], tuple):
        return
    text = parts[0][1].decode("utf-8", "replace").lower()
    for addr, cid in by_addr.items():
        if addr in text:
            db.execute("UPDATE enrollments SET status='bounced' WHERE contact_id=?", (cid,))
            db.execute("INSERT OR IGNORE INTO suppressions (email,reason) VALUES (?,'bounce')",
                       (addr,))
            event(db, cid, None, "bounced")
            log(f"bounce <- {addr}")
            return


# ------------------------------------------------------------------------ CRUD


def upsert(db, item: dict, valid_campaign=None) -> str:
    addr = str(item["email"]).strip().lower()
    if not addr or "@" not in addr:
        raise ValueError(f"invalid email: {item['email']!r}")
    campaign = item.get("campaign_id")
    if campaign and valid_campaign and campaign != valid_campaign:
        raise ValueError(f"unknown campaign_id {campaign!r} "        # before any write
                         f"(campaign.toml declares {valid_campaign!r})")
    extra = {k: str(v) for k, v in item.items()
             if k not in CORE and k not in ENROLL_FIELDS and v not in (None, "")}

    row = db.execute("SELECT id, extra FROM contacts WHERE email=?", (addr,)).fetchone()
    if row:
        cid, verb = row["id"], "updated"
        merged = json.loads(row["extra"] or "{}")
        merged.update(extra)
        sets, vals = ["extra=?"], [json.dumps(merged)]
        for k in CONTACT_COLS[1:]:
            if item.get(k):
                sets.append(f"{k}=?")
                vals.append(str(item[k]))
        db.execute(f"UPDATE contacts SET {','.join(sets)} WHERE id=?", (*vals, cid))
    else:
        cur = db.execute(
            f"INSERT INTO contacts ({','.join(CONTACT_COLS)},extra) "
            f"VALUES ({','.join('?' * len(CONTACT_COLS))},?)",
            (addr, *(str(item.get(c, "")) for c in CONTACT_COLS[1:]), json.dumps(extra)),
        )
        cid, verb = cur.lastrowid, "created"

    if campaign:
        db.execute(
            "INSERT OR IGNORE INTO enrollments (contact_id,campaign_id,status,step,next_at) "
            "VALUES (?,?,?,?,?)",
            (cid, campaign, item.get("status", "active"),
             int(item.get("step", 0) or 0), item.get("next_at")),
        )
        sets, vals = [], []
        for k in ("status", "next_at"):
            if k in item and item[k] not in (None, ""):
                sets.append(f"{k}=?")
                vals.append(str(item[k]))
        if "step" in item and item["step"] not in (None, ""):
            sets.append("step=?")
            vals.append(int(item["step"]))
        if sets:
            db.execute(
                f"UPDATE enrollments SET {','.join(sets)} WHERE contact_id=? AND campaign_id=?",
                (*vals, cid, campaign),
            )
    return verb


def stats(db, cfg):
    out = {"sent_today": sent_today(db, cfg),
           "max_per_day": setting(db, cfg, "max_per_day", 180, int),
           "paused": setting(db, cfg, "paused", False, bool),
           "in_window": in_window(db, cfg),
           "contacts": db.execute("SELECT count(*) c FROM contacts").fetchone()["c"],
           "suppressed": db.execute("SELECT count(*) c FROM suppressions").fetchone()["c"],
           "by_status": {}}
    out["budget_left"] = max(0, out["max_per_day"] - out["sent_today"])
    for r in db.execute("SELECT status, count(*) c FROM enrollments GROUP BY status"):
        out["by_status"][r["status"]] = r["c"]
    out["due_now"] = len(due_rows(db, cfg, 10_000))
    return out


# ------------------------------------------------------------------------- api


def make_api(db_path, cfg_path):
    local = threading.local()

    def db():
        if not hasattr(local, "db"):
            local.db = connect()
        return local.db

    token = env("SYSTEM_AUTH_TOKEN")

    class Handler(BaseHTTPRequestHandler):
        server_version = "sparrow"

        def _guard(self):
            if not token:
                self._json({"error": "SYSTEM_AUTH_TOKEN not set"}, 503)
                return False
            if self.headers.get("Authorization") != f"Bearer {token}":
                self._json({"error": "unauthorized"}, 401)
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or "{}")

        def _json(self, obj, code=200):
            b = json.dumps(obj, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            if not self._guard():
                return
            cfg = load_cfg(cfg_path)
            path = urlparse(self.path).path.rstrip("/")
            if path == "/contacts":
                items = self._body()
                items = items if isinstance(items, list) else [items]
                conn, res = db(), []
                conn.execute("BEGIN")
                try:
                    for it in items:
                        try:
                            res.append(upsert(conn, it, cfg["id"]))
                        except (ValueError, KeyError) as e:
                            res.append(f"error: {e}")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                return self._json({"results": res}, 201)
            if path == "/suppressions":
                it = self._body()
                db().execute("INSERT OR REPLACE INTO suppressions (email,reason) VALUES (?,?)",
                             (it["email"].strip().lower(), it.get("reason", "manual")))
                return self._json({"ok": True}, 201)
            self._json({"error": "not found"}, 404)

        def do_PATCH(self):
            if not self._guard():
                return
            cfg = load_cfg(cfg_path)
            path = urlparse(self.path).path
            m = re.fullmatch(r"/contacts/(.+)", unquote(path))
            if m:
                addr = m.group(1).strip().lower()
                if not db().execute("SELECT 1 FROM contacts WHERE email=?", (addr,)).fetchone():
                    return self._json({"error": "unknown contact"}, 404)
                patch = dict(self._body())
                patch["email"] = addr
                if "campaign_id" not in patch:
                    rows = db().execute(
                        "SELECT campaign_id FROM enrollments e JOIN contacts c ON c.id=e.contact_id "
                        "WHERE c.email=?", (addr,)).fetchall()
                    if len(rows) == 1:
                        patch["campaign_id"] = rows[0]["campaign_id"]
                    elif len(rows) > 1:
                        return self._json({"error": "specify campaign_id"}, 409)
                return self._json({"result": upsert(db(), patch, cfg["id"])})
            if urlparse(path).path.rstrip("/") == "/settings":
                for k, v in self._body().items():
                    db().execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                                 (k, str(v)))
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)

        def do_DELETE(self):
            if not self._guard():
                return
            m = re.fullmatch(r"/settings/(.+)", unquote(urlparse(self.path).path))
            if not m:
                return self._json({"error": "not found"}, 404)
            db().execute("DELETE FROM settings WHERE key=?", (m.group(1),))
            self._json({"ok": True})

        def do_GET(self):
            if not self._guard():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path.rstrip("/") or "/"
            cfg = load_cfg(cfg_path)
            if path == "/stats":
                return self._json(stats(db(), cfg))
            if path == "/contacts":
                sql = (f"SELECT {C_SELECT},c.extra,e.campaign_id,e.status,e.step,"
                       "e.next_at,e.last_sent_at FROM contacts c "
                       "LEFT JOIN enrollments e ON e.contact_id=c.id WHERE 1=1")
                args = []
                for col in ("status", "campaign_id"):
                    if col in q:
                        sql += f" AND e.{col}=?"
                        args.append(q[col])
                if "email" in q:
                    sql += " AND c.email=?"
                    args.append(q["email"].lower())
                sql += " ORDER BY e.next_at LIMIT ?"
                args.append(int(q.get("limit", 200)))
                out = []
                for r in db().execute(sql, args):
                    d = dict(r)
                    d.pop("extra", None)
                    d.update(json.loads(r["extra"] or "{}"))
                    out.append(d)
                return self._json(out)
            if path == "/events":
                rows = db().execute(
                    "SELECT e.*, c.email FROM events e LEFT JOIN contacts c ON c.id=e.contact_id "
                    "ORDER BY e.id DESC LIMIT ?", (int(q.get("limit", 100)),)).fetchall()
                return self._json([dict(r) for r in rows])
            self._json({"error": "not found"}, 404)

        def log_message(self, *a):
            pass  # journald timestamps for us

    return Handler


def serve(cfg):
    host = cfg.get("api_host", "127.0.0.1")
    port = int(cfg.get("api_port", 8787))
    srv = ThreadingHTTPServer((host, port), make_api(DB_PATH, CFG_PATH))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"api on http://{host}:{port}")


# ------------------------------------------------------------------------ main


def tick(db, cfg, dry, counter=[0]):
    counter[0] += 1
    every = int(cfg.get("imap_every", 5))
    if not dry and every and counter[0] % every == 1:
        try:
            poll_replies(db, cfg)
        except Exception as e:
            log("imap error:", e)
    try:
        return send_due(db, cfg, dry)
    except Exception as e:
        log("send error:", e)
        return 0


def main():
    p = argparse.ArgumentParser(description="single-file email campaign runner")
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "stats", "poll", "init"])
    p.add_argument("--loop", action="store_true", help="tick forever (default: one tick)")
    p.add_argument("--dry-run", action="store_true", help="render and log, never send")
    p.add_argument("--api", action="store_true", help="serve the HTTP API alongside --loop")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--env", default=os.path.join(HERE, ".env"),
                   help="env file to load (default: .env beside server.py)")
    a = p.parse_args()

    loaded = load_env_file(a.env)
    if loaded:
        log(f"loaded {loaded} var(s) from {a.env}")

    if a.command == "init":
        db = connect()
        log(f"initialised {DB_PATH}")
        return

    db = connect()

    cfg = load_cfg()
    if a.command == "stats":
        print(json.dumps(stats(db, cfg), indent=2))
        return
    if a.command == "poll":
        check_transport()
        poll_replies(db, cfg)
        return

    if not a.dry_run:
        check_transport()
    if a.api:
        serve(cfg)
    if not a.loop:
        tick(db, cfg, a.dry_run)
        return
    log(f"tick every {a.interval}s" + (" [DRY RUN]" if a.dry_run else ""))
    while True:
        try:
            cfg = load_cfg()  # hot reload
        except Exception as e:
            log("config error:", e)
        tick(db, cfg, a.dry_run)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
