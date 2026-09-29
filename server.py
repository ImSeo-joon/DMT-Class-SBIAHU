#!/usr/bin/env python3
"""Small, dependency-free class website with server-enforced roles and SQLite storage."""
from __future__ import annotations

import getpass
import hashlib
import hmac
import mimetypes
import json
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import formataddr
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
DB_DIR = ROOT / "var"
DB_PATH = Path(os.environ.get("DMT_DB_PATH", DB_DIR / "class_site.sqlite3"))
UPLOAD_DIR = Path(os.environ.get("DMT_UPLOAD_DIR", DB_PATH.parent / "uploads"))
SEED_PATH = ROOT / "content.json"
COOKIE = "dmt_session"
SESSION_DAYS = 14
PBKDF2_ROUNDS = 310_000
ROLES = {"admin", "officer", "counselor", "member"}
ROLE_LABELS = {"admin": "管理员", "officer": "班委", "counselor": "导员", "member": "班级成员"}
CONTENT_ROLES = {"admin", "officer", "counselor"}
RESOURCE_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".txt", ".md", ".zip", ".png", ".jpg", ".jpeg", ".webp"}
MAX_RESOURCE_BYTES = 25 * 1024 * 1024
RATE_LIMIT: dict[str, list[float]] = {}
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INVITE_ROLES = ("member", "officer", "counselor")
INVITE_ROLE_LABELS = {"member": "班级成员 / Member", "officer": "班委 / Officer", "counselor": "导员 / Counselor"}
REGISTRATION_MODES = ("invite", "open", "closed")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.deepseek.com").rstrip("/")
AI_MODELS = ("deepseek-chat", "deepseek-reasoner")
AI_TIMEOUT = 60
AI_MAX_PROMPT = 6000
EMAIL_VERIFICATION_MODES = ("off", "notify", "gate")
EMAIL_DOMAIN_CHECK_MODES = ("strict", "warn", "off")
EMAIL_CODE_TTL_MINUTES = 30
EMAIL_CODE_MAX_ATTEMPTS = 5
EMAIL_CACHE_TTL = 600
EMAIL_CACHE: dict[str, tuple[float, tuple[str, str]]] = {}
DISPOSABLE_DOMAINS = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "sharklasers.com", "tempmail.com",
    "temp-mail.org", "yopmail.com", "trashmail.com", "disposablemail.com", "maildrop.cc",
    "getnada.com", "fakeinbox.com", "mailnesia.com", "throwawaymail.com", "spam4.me",
    "guerrillamailblock.com", "grr.la", "mailcatch.com", "mytemp.email", "tmpmail.org",
}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, display_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
          password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
          role TEXT NOT NULL DEFAULT 'member', created_at TEXT NOT NULL,
          CHECK(role IN ('admin','officer','counselor','member'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS news (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL,
          updated_by TEXT REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL,
          updated_by TEXT REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS site_settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL,
          updated_by TEXT REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT REFERENCES users(id),
          action TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resources (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          category TEXT NOT NULL DEFAULT '班级共享', original_name TEXT NOT NULL,
          stored_name TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL, size INTEGER NOT NULL,
          uploaded_by TEXT REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invite_codes (
          id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, label TEXT NOT NULL DEFAULT '',
          role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('member','officer','counselor')),
          max_uses INTEGER NOT NULL DEFAULT 1, used_count INTEGER NOT NULL DEFAULT 0,
          expires_at TEXT, disabled INTEGER NOT NULL DEFAULT 0,
          created_by TEXT REFERENCES users(id), created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS email_tokens (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          purpose TEXT NOT NULL DEFAULT 'verify', code_hash TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, expires_at TEXT NOT NULL,
          used_at TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS team_posts (
          id TEXT PRIMARY KEY, author_id TEXT REFERENCES users(id) ON DELETE SET NULL,
          track TEXT NOT NULL DEFAULT '竞赛', title TEXT NOT NULL, body TEXT NOT NULL,
          needed TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS team_replies (
          id TEXT PRIMARY KEY, post_id TEXT NOT NULL REFERENCES team_posts(id) ON DELETE CASCADE,
          author_id TEXT REFERENCES users(id) ON DELETE SET NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback (
          id TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
          account_email TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '功能建议',
          body TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, mailed INTEGER NOT NULL DEFAULT 0, mail_detail TEXT NOT NULL DEFAULT ''
        );
        """)
        ensure_user_columns(con)
        ensure_feedback_columns(con)
        seeded = con.execute("SELECT 1 FROM site_settings WHERE key='seed_version'").fetchone()
        if not seeded:
            seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
            now = utc_now()
            if con.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0:
                for item in seed.get("news", []):
                    con.execute("INSERT INTO news(id,payload,updated_at) VALUES(?,?,?)",
                                (item["id"], json.dumps(item, ensure_ascii=False), now))
                for item in seed.get("events", []):
                    con.execute("INSERT INTO events(id,payload,updated_at) VALUES(?,?,?)",
                                (item.get("id") or str(uuid.uuid4()), json.dumps(item, ensure_ascii=False), now))
                con.execute("INSERT OR IGNORE INTO site_settings(key,value,updated_at) VALUES('schedule',?,?)",
                            (json.dumps(seed["schedule"], ensure_ascii=False), now))
            con.execute("INSERT INTO site_settings(key,value,updated_at) VALUES('seed_version','1',?)", (now,))
        if con.execute("SELECT 1 FROM site_settings WHERE key='schedule'").fetchone() is None:
            seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
            con.execute("INSERT INTO site_settings(key,value,updated_at) VALUES('schedule',?,?)",
                        (json.dumps(seed["schedule"], ensure_ascii=False), utc_now()))
        if con.execute("SELECT 1 FROM site_settings WHERE key='registration_mode'").fetchone() is None:
            con.execute("INSERT INTO site_settings(key,value,updated_at) VALUES('registration_mode',?,?)",
                        ("invite", utc_now()))
        for key, value in (("email_verification", "notify"), ("email_domain_check", "strict"),
                           ("email_allowed_domains", ""), ("site_notice", "")):
            if con.execute("SELECT 1 FROM site_settings WHERE key=?", (key,)).fetchone() is None:
                con.execute("INSERT INTO site_settings(key,value,updated_at) VALUES(?,?,?)", (key, value, utc_now()))


FEEDBACK_STATUSES = ("待处理", "处理中", "已解决")


def ensure_feedback_columns(con: sqlite3.Connection) -> None:
    columns = {row["name"] for row in con.execute("PRAGMA table_info(feedback)")}
    for name, ddl in (("status", "TEXT NOT NULL DEFAULT '待处理'"),
                      ("response", "TEXT NOT NULL DEFAULT ''"),
                      ("handled_at", "TEXT"),
                      ("handled_by", "TEXT")):
        if name not in columns:
            con.execute(f"ALTER TABLE feedback ADD COLUMN {name} {ddl}")


def ensure_user_columns(con: sqlite3.Connection) -> None:
    """Add columns introduced after the first release without touching existing data."""
    columns = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
    if "email_verified" not in columns:
        con.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
    if "email_verified_at" not in columns:
        con.execute("ALTER TABLE users ADD COLUMN email_verified_at TEXT")
    if "account_type" not in columns:
        con.execute("ALTER TABLE users ADD COLUMN account_type TEXT NOT NULL DEFAULT 'member'")


ACCOUNT_TYPES = ("member", "visitor")
VISITOR_BLOCKED = ("该板块仅对 DMT Class 01 成员开放 / Members only")


def account_type_of(user: sqlite3.Row | None) -> str:
    if not user:
        return ""
    try:
        value = str(user["account_type"] or "member").strip().lower()
    except Exception:
        return "member"
    return value if value in ACCOUNT_TYPES else "member"


def is_visitor(user: sqlite3.Row | None) -> bool:
    return account_type_of(user) == "visitor"


def is_member(user: sqlite3.Row | None) -> bool:
    """True only for signed-in member accounts; visitors and guests are not members."""
    return account_type_of(user) == "member"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt, digest


def valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@]{1,64}@[^\s@.]{1,190}(?:\.[^\s@.]{1,63})+", value))


def normalize_invite_code(value: object) -> str:
    return str(value or "").strip().upper()


def generate_invite_code() -> str:
    groups = ["".join(secrets.choice(INVITE_ALPHABET) for _ in range(5)) for _ in range(2)]
    return "DMT-" + "-".join(groups)


def registration_mode() -> str:
    init_db()
    with connect() as con:
        row = con.execute("SELECT value FROM site_settings WHERE key='registration_mode'").fetchone()
    value = str(row["value"]).strip().lower() if row else "invite"
    return value if value in REGISTRATION_MODES else "invite"


def set_registration_mode(mode: str) -> str:
    if mode not in REGISTRATION_MODES:
        raise ValueError("注册方式只能是 invite、open 或 closed")
    init_db()
    with connect() as con:
        con.execute("UPDATE site_settings SET value=?,updated_at=? WHERE key='registration_mode'", (mode, utc_now()))
    return mode


def invite_available(row: sqlite3.Row) -> bool:
    if row["disabled"]:
        return False
    if row["used_count"] >= row["max_uses"]:
        return False
    if row["expires_at"] and row["expires_at"] <= utc_now():
        return False
    return True


def create_invite(label: object, role: str, max_uses: int, expires_in_days: int,
                  created_by: str | None = None) -> dict:
    if role not in INVITE_ROLES:
        raise ValueError("邀请码身份只能是班级成员、班委或导员")
    if not isinstance(max_uses, int) or not 1 <= max_uses <= 500:
        raise ValueError("可用次数须在 1–500 之间")
    if not isinstance(expires_in_days, int) or not 0 <= expires_in_days <= 3650:
        raise ValueError("有效天数须在 0–3650 之间，0 表示不限时间")
    clean_label = clean_text(label, "备注", maximum=60)
    invite_id = str(uuid.uuid4())
    created_at = utc_now()
    expires_at = None
    if expires_in_days:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat(timespec="seconds")
    init_db()
    with connect() as con:
        for _ in range(8):
            code = generate_invite_code()
            if not con.execute("SELECT 1 FROM invite_codes WHERE code=?", (code,)).fetchone():
                break
        else:
            raise ValueError("邀请码生成失败，请重试")
        con.execute("INSERT INTO invite_codes(id,code,label,role,max_uses,used_count,expires_at,disabled,created_by,created_at)"
                    " VALUES(?,?,?,?,?,0,?,0,?,?)",
                    (invite_id, code, clean_label, role, max_uses, expires_at, created_by, created_at))
    return {"id": invite_id, "code": code, "label": clean_label, "role": role,
            "role_label": INVITE_ROLE_LABELS[role], "max_uses": max_uses, "used_count": 0,
            "expires_at": expires_at, "disabled": 0, "created_at": created_at}


def invite_list() -> list[dict]:
    init_db()
    with connect() as con:
        rows = con.execute("SELECT id,code,label,role,max_uses,used_count,expires_at,disabled,created_at"
                           " FROM invite_codes ORDER BY created_at DESC").fetchall()
    return [dict(row) | {"role_label": INVITE_ROLE_LABELS[row["role"]], "active": invite_available(row)}
            for row in rows]


def setting_value(key: str, default: str = "") -> str:
    try:
        with connect() as con:
            row = con.execute("SELECT value FROM site_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default  # 数据库还没初始化时按默认值处理
    return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> str:
    init_db()
    with connect() as con:
        con.execute("UPDATE site_settings SET value=?,updated_at=? WHERE key=?", (value, utc_now(), key))
    return value


def email_verification_mode() -> str:
    value = setting_value("email_verification", "notify").strip().lower()
    return value if value in EMAIL_VERIFICATION_MODES else "notify"


def set_email_verification_mode(mode: str) -> str:
    if mode not in EMAIL_VERIFICATION_MODES:
        raise ValueError("邮箱验证方式只能是 off、notify 或 gate")
    return set_setting("email_verification", mode)


def email_domain_check_mode() -> str:
    value = os.environ.get("EMAIL_DOMAIN_CHECK", "").strip().lower() or setting_value("email_domain_check", "strict")
    value = value.strip().lower()
    return value if value in EMAIL_DOMAIN_CHECK_MODES else "strict"


def set_email_domain_check_mode(mode: str) -> str:
    if mode not in EMAIL_DOMAIN_CHECK_MODES:
        raise ValueError("域名校验方式只能是 strict、warn 或 off")
    return set_setting("email_domain_check", mode)


def parse_domain_list(raw: object) -> list[str]:
    return [item.strip().lower().lstrip("@") for item in re.split(r"[,\s;]+", str(raw or "")) if item.strip()]


def email_allowed_domains() -> list[str]:
    raw = os.environ.get("EMAIL_ALLOWED_DOMAINS", "").strip() or setting_value("email_allowed_domains", "")
    return parse_domain_list(raw)


def set_email_allowed_domains(raw: object) -> list[str]:
    domains = parse_domain_list(raw)
    set_setting("email_allowed_domains", ", ".join(domains))
    return domains


def mailer_config() -> dict:
    try:
        port = int(os.environ.get("SMTP_PORT", "").strip() or 465)
    except ValueError:
        port = 465
    security = os.environ.get("SMTP_SECURITY", "").strip().lower() or ("ssl" if port == 465 else "starttls")
    if security not in ("ssl", "starttls", "none"):
        security = "starttls"
    user = os.environ.get("SMTP_USER", "").strip()
    return {"host": os.environ.get("SMTP_HOST", "").strip(), "port": port, "user": user,
            "password": os.environ.get("SMTP_PASSWORD", ""), "security": security,
            "sender": os.environ.get("SMTP_SENDER", "").strip() or user,
            "sender_name": os.environ.get("SMTP_SENDER_NAME", "").strip() or "DMT CLASS 01"}


def mailer_configured() -> bool:
    cfg = mailer_config()
    return bool(cfg["host"] and cfg["sender"])


def send_mail(to: str, subject: str, text: str) -> tuple[bool, str]:
    """Send a plain-text message. Returns (ok, detail) and never raises."""
    if not mailer_configured():
        return False, "尚未配置发信账号（需要 SMTP_HOST 与 SMTP_SENDER 或 SMTP_USER）"
    cfg = mailer_config()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((cfg["sender_name"], cfg["sender"]))
    message["To"] = to
    message.set_content(text)
    try:
        if cfg["security"] == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
        with server:
            server.ehlo()
            if cfg["security"] == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(message)
        return True, "已发送"
    except Exception as exc:  # network, auth, TLS and address problems all land here
        return False, f"{type(exc).__name__}: {exc}"


def _nslookup_mx(domain: str) -> tuple[str, str]:
    """Fallback MX lookup through the system nslookup binary."""
    try:
        proc = subprocess.run(["nslookup", "-type=mx", domain], capture_output=True, text=True, timeout=8)
    except FileNotFoundError:
        try:
            proc = subprocess.run(["nslookup", "-query=mx", domain], capture_output=True, text=True, timeout=8)
        except Exception:
            return "", ""
    except Exception:
        return "", ""
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    lowered = output.lower()
    if "non-existent domain" in lowered or "nxdomain" in lowered:
        return "none", "域名不存在"
    if "no answer" in lowered or "has no mx" in lowered or "no mx record" in lowered:
        return "", ""  # 域名存在但没有 MX：交给 A/AAAA 解析再判断
    if "can't find" in lowered:
        return "none", "域名不存在"
    hosts = re.findall(r"mail exchanger\s*=\s*([^\s]+)", output, re.I)
    if hosts:
        return "mx", ", ".join(host.rstrip(".") for host in hosts[:3])
    return "", ""


def _domain_verdict_uncached(domain: str) -> tuple[str, str]:
    """Return ("mx"|"a"|"none"|"error", detail) for a mail domain."""
    try:
        import dns.resolver  # optional: strict MX lookups when dnspython is installed
    except Exception:
        resolver = None
    else:
        resolver = dns.resolver
    if resolver is not None:
        try:
            records = resolver.resolve(domain, "MX", lifetime=8)
            hosts = sorted(str(record.exchange).rstrip(".") for record in records)
            if hosts:
                return "mx", ", ".join(hosts[:3])
        except resolver.NXDOMAIN:
            return "none", "域名不存在"
        except resolver.NoAnswer:
            pass
        except Exception as exc:
            return "error", type(exc).__name__
        for record_type in ("A", "AAAA"):
            try:
                resolver.resolve(domain, record_type, lifetime=8)
                return "a", f"无 MX 记录，但存在 {record_type} 记录"
            except resolver.NXDOMAIN:
                return "none", "域名不存在"
            except Exception:
                continue
        return "none", "没有可用的收信记录"
    verdict, detail = _nslookup_mx(domain)
    if verdict:
        return verdict, detail
    try:
        socket_hosts = socket.getaddrinfo(domain, None)
    except OSError:
        return "none", "域名不存在"
    except Exception as exc:
        return "error", type(exc).__name__
    return ("a", "仅有地址记录，未确认 MX") if socket_hosts else ("none", "域名不存在")


def email_domain_verdict(domain: str) -> tuple[str, str]:
    now = time.time()
    cached = EMAIL_CACHE.get(domain)
    if cached and cached[0] > now:
        return cached[1]
    verdict = _domain_verdict_uncached(domain)
    EMAIL_CACHE[domain] = (now + EMAIL_CACHE_TTL, verdict)
    return verdict


def check_email_address(address: str) -> tuple[bool, str]:
    """Validate the mail domain of an address. Returns (accepted, note)."""
    domain = str(address or "").rsplit("@", 1)[-1].strip().lower().rstrip(".")
    if not domain:
        return False, "邮箱格式不正确。"
    if domain in DISPOSABLE_DOMAINS:
        return False, "不支持一次性临时邮箱，请使用常用邮箱注册。"
    allowed = email_allowed_domains()
    if allowed and not any(domain == item or domain.endswith("." + item) for item in allowed):
        return False, "请使用学校邮箱注册（仅接受：" + "、".join(allowed) + "）。"
    mode = email_domain_check_mode()
    if mode == "off":
        return True, ""
    verdict, detail = email_domain_verdict(domain)
    if verdict in ("mx", "a"):
        return True, ""
    if verdict == "error":
        return True, f"域名校验未能完成（{detail}），本次先放行"
    return False, "邮箱域名不存在或无法接收邮件，请检查是否输错（例如把 com 写成 con）。"


def generate_email_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _email_code_hash(token_id: str, code: str) -> str:
    return hashlib.sha256(f"{token_id}:{code}".encode()).hexdigest()


def issue_email_code(user_id: str, purpose: str = "verify") -> str:
    """Create a fresh 6-digit code for a user and invalidate earlier ones."""
    code = generate_email_code()
    token_id = str(uuid.uuid4())
    expires = (datetime.now(timezone.utc) + timedelta(minutes=EMAIL_CODE_TTL_MINUTES)).isoformat(timespec="seconds")
    init_db()
    with connect() as con:
        con.execute("UPDATE email_tokens SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
                    (utc_now(), user_id, purpose))
        con.execute("INSERT INTO email_tokens(id,user_id,purpose,code_hash,attempts,expires_at,used_at,created_at)"
                    " VALUES(?,?,?,?,0,?,NULL,?)",
                    (token_id, user_id, purpose, _email_code_hash(token_id, code), expires, utc_now()))
    return code


def confirm_email_code(email: str, code: str) -> tuple[bool, str, str | None]:
    """Check a submitted code. Returns (ok, message, user_id)."""
    address = str(email or "").strip().lower()
    submitted = re.sub(r"\D", "", str(code or ""))
    if not address or len(submitted) != 6:
        return False, "请输入邮件里的 6 位验证码。", None
    init_db()
    with connect() as con:
        user = con.execute("SELECT id,email FROM users WHERE email=?", (address,)).fetchone()
        if not user:
            return False, "验证码无效或已过期，请重新获取。", None
        token = con.execute("SELECT * FROM email_tokens WHERE user_id=? AND purpose='verify' AND used_at IS NULL"
                            " ORDER BY created_at DESC LIMIT 1", (user["id"],)).fetchone()
        if not token:
            return False, "还没有待验证的验证码，请先获取。", user["id"]
        if token["expires_at"] <= utc_now():
            return False, "验证码已过期，请重新获取。", user["id"]
        if token["attempts"] >= EMAIL_CODE_MAX_ATTEMPTS:
            return False, "尝试次数过多，请重新获取验证码。", user["id"]
        if not hmac.compare_digest(_email_code_hash(token["id"], submitted), token["code_hash"]):
            con.execute("UPDATE email_tokens SET attempts=attempts+1 WHERE id=?", (token["id"],))
            return False, "验证码不正确。", user["id"]
        now = utc_now()
        con.execute("UPDATE email_tokens SET used_at=? WHERE user_id=? AND purpose='verify' AND used_at IS NULL",
                    (now, user["id"]))
        con.execute("UPDATE users SET email_verified=1,email_verified_at=? WHERE id=?", (now, user["id"]))
    return True, "邮箱验证成功。", user["id"]


def mark_email_verified(email: str) -> bool:
    """Used by the CLI when a student cannot receive the code."""
    address = str(email or "").strip().lower()
    init_db()
    with connect() as con:
        cur = con.execute("UPDATE users SET email_verified=1,email_verified_at=? WHERE email=?", (utc_now(), address))
        if cur.rowcount:
            con.execute("UPDATE email_tokens SET used_at=? WHERE user_id=(SELECT id FROM users WHERE email=?)"
                        " AND used_at IS NULL", (utc_now(), address))
    return bool(cur.rowcount)


def verification_email_text(email: str, code: str) -> tuple[str, str]:
    origin = os.environ.get("SITE_ORIGIN", "").strip().rstrip("/")
    subject = "DMT CLASS 01 邮箱验证码 / Email verification code"
    lines = [
        "你好，",
        "",
        f"你的验证码是：{code}",
        f"有效期 {EMAIL_CODE_TTL_MINUTES} 分钟，请勿转发给他人。",
        "",
        f"Your verification code is {code}, valid for {EMAIL_CODE_TTL_MINUTES} minutes.",
    ]
    if origin:
        lines += ["", f"DMT CLASS 01 网站 / Site: {origin}"]
    lines += ["", "如果不是你本人操作，请忽略这封邮件。", "If you did not request this, you can safely ignore it."]
    return subject, "\n".join(lines)


def send_verification_email(email: str, code: str) -> tuple[bool, str]:
    subject, text = verification_email_text(email, code)
    return send_mail(email, subject, text)


TEAM_TRACKS = ("竞赛", "科研", "活动", "课程", "其他")


def validate_team_post(raw: dict) -> dict:
    track = clean_text(raw.get("track"), "分类", maximum=20) or TEAM_TRACKS[0]
    if track not in TEAM_TRACKS:
        raise ValueError("分类只能是：" + "、".join(TEAM_TRACKS))
    return {"track": track,
            "title": clean_text(raw.get("title"), "标题", required=True, maximum=80),
            "body": clean_text(raw.get("body"), "正文", required=True, maximum=2000),
            "needed": clean_text(raw.get("needed"), "需要的伙伴或资源", maximum=200)}


NOTICE_KEY = "site_notice"


def site_notice() -> str:
    return setting_value(NOTICE_KEY, "").strip()


def set_site_notice(text: object) -> str:
    value = clean_text(text, "公告内容", maximum=300)
    set_setting(NOTICE_KEY, value)
    return value


def notification_payload(user: sqlite3.Row | None) -> dict:
    """顶部通知栏的数据：站点公告 + 最新帖子 + 最新新闻 + 最新资料。"""
    init_db()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    with connect() as con:
        post = con.execute("SELECT p.id,p.title,p.created_at,p.status,u.display_name AS author"
                           " FROM team_posts p LEFT JOIN users u ON u.id=p.author_id"
                           " ORDER BY p.created_at DESC LIMIT 1").fetchone()
        recent_posts = con.execute("SELECT COUNT(*) FROM team_posts WHERE created_at > ?", (week_ago,)).fetchone()[0]
        news_rows = con.execute("SELECT id,payload FROM news").fetchall()
        resource = con.execute("SELECT title,created_at FROM resources ORDER BY created_at DESC LIMIT 1").fetchone()
    latest_news = None
    for row in news_rows:
        item = json.loads(row["payload"])
        if latest_news is None or str(item.get("date") or "") > str(latest_news.get("date") or ""):
            latest_news = item
    payload: dict = {"notice": site_notice(), "recent_posts": recent_posts, "logged_in": bool(user)}
    if user and post:
        payload["latest_post"] = {"id": post["id"], "title": post["title"], "author": post["author"],
                                  "created_at": post["created_at"], "status": post["status"]}
    if latest_news:
        payload["latest_news"] = {"title": latest_news.get("title_cn") or latest_news.get("title_en") or "",
                                  "date": latest_news.get("date") or ""}
    if resource:
        payload["latest_resource"] = {"title": resource["title"], "created_at": resource["created_at"]}
    return payload


FEEDBACK_CATEGORIES = ("功能建议", "问题反馈", "内容纠错", "其他")


def feedback_admin_emails() -> list[str]:
    """反馈收件人：优先取环境变量 FEEDBACK_EMAIL，其次所有管理员账号邮箱。"""
    raw = os.environ.get("FEEDBACK_EMAIL", "").strip()
    if raw:
        return [item.strip() for item in re.split(r"[,\s;]+", raw) if item.strip()]
    init_db()
    with connect() as con:
        rows = con.execute("SELECT email FROM users WHERE role='admin' AND email<>'' ORDER BY created_at").fetchall()
    return [row["email"] for row in rows]


def post_feedback(user: sqlite3.Row, raw: dict) -> dict:
    category = clean_text(raw.get("category"), "反馈类别", maximum=20) or FEEDBACK_CATEGORIES[0]
    if category not in FEEDBACK_CATEGORIES:
        raise ValueError("反馈类别只能是：" + "、".join(FEEDBACK_CATEGORIES))
    body_text = clean_text(raw.get("body"), "反馈内容", required=True, maximum=2000)
    contact = clean_text(raw.get("contact"), "联系方式", maximum=120)
    display = str(user["display_name"] or "")
    email = str(user["email"] or "")
    identity = "访客 / Visitor" if is_visitor(user) else "DMT Class 01 成员"
    created = utc_now()
    recipients = feedback_admin_emails()
    mailed, detail = 0, "未配置发信账号，反馈已存档" if not mailer_configured() else ""
    if recipients and mailer_configured():
        lines = [f"来自班级网站的{category}", "",
                 f"提交人：{display}（{email}）", f"身份：{identity}",
                 f"联系方式：{contact or '未填写（可用注册邮箱）'}",
                 f"提交时间（UTC）：{created}", "", "反馈内容：", body_text, "",
                 "— 本邮件由 DMT Class 01 网站的个人中心反馈模块自动发送。"]
        ok = True
        reasons = []
        for to_addr in recipients:
            sent, why = send_mail(to_addr, f"【网站反馈】{category} · {display or email}", "\n".join(lines))
            if not sent:
                ok = False; reasons.append(f"{to_addr}: {why}")
        mailed = 1 if ok else 0
        detail = "已发送至 " + "、".join(recipients) if ok else "；".join(reasons)
    elif not recipients:
        detail = "没有可用的管理员邮箱，反馈已存档"
    feedback_id = str(uuid.uuid4())
    init_db()
    with connect() as con:
        con.execute("INSERT INTO feedback(id,user_id,account_email,category,body,contact,created_at,mailed,mail_detail)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (feedback_id, user["id"], email, category, body_text, contact, created, mailed, detail))
    return {"id": feedback_id, "category": category, "created_at": created,
            "mailed": bool(mailed), "detail": detail, "recipients": recipients}


def feedback_list(limit: int = 100, user_id: str | None = None) -> list[dict]:
    init_db()
    columns = ("f.id,f.user_id,f.account_email,f.category,f.body,f.contact,f.created_at,f.mailed,f.mail_detail,"
               "f.status,f.response,f.handled_at,u.display_name AS author,hu.display_name AS handler")
    sql = ("SELECT " + columns + " FROM feedback f LEFT JOIN users u ON u.id=f.user_id"
           " LEFT JOIN users hu ON hu.id=f.handled_by")
    params: tuple = ()
    if user_id:
        sql += " WHERE f.user_id=?"
        params = (user_id,)
    sql += " ORDER BY f.created_at DESC LIMIT ?"
    params = params + (limit,)
    with connect() as con:
        rows = con.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def handle_feedback(feedback_id: str, status: str, response: object, admin_id: str) -> bool:
    if status not in FEEDBACK_STATUSES:
        raise ValueError("处理状态只能是：" + "、".join(FEEDBACK_STATUSES))
    note = clean_text(response, "处理结果", maximum=1000)
    init_db()
    with connect() as con:
        cur = con.execute("UPDATE feedback SET status=?,response=?,handled_at=?,handled_by=? WHERE id=?",
                          (status, note, utc_now(), admin_id, feedback_id))
    return bool(cur.rowcount)


GUEST_AUTHOR_LABEL = "班级成员 / Member"


def _mask_team_author(row: dict) -> dict:
    """Hide who posted when the viewer is not signed in."""
    row["author"] = GUEST_AUTHOR_LABEL
    row["author_id"] = None
    return row


def team_post_list(limit: int = 100, mask_authors: bool = False) -> list[dict]:
    init_db()
    with connect() as con:
        rows = con.execute(
            "SELECT p.id,p.author_id,p.track,p.title,p.body,p.needed,p.status,p.created_at,"
            " u.display_name AS author,"
            " (SELECT COUNT(*) FROM team_replies r WHERE r.post_id=p.id) AS reply_count,"
            " (SELECT MAX(r.created_at) FROM team_replies r WHERE r.post_id=p.id) AS last_reply_at"
            " FROM team_posts p LEFT JOIN users u ON u.id=p.author_id"
            " ORDER BY p.created_at DESC LIMIT ?", (limit,)).fetchall()
    posts = [dict(row) for row in rows]
    if mask_authors:
        posts = [_mask_team_author(post) for post in posts]
    return posts


def team_post_detail(post_id: str, mask_authors: bool = False) -> dict | None:
    init_db()
    with connect() as con:
        post = con.execute(
            "SELECT p.*, u.display_name AS author FROM team_posts p LEFT JOIN users u ON u.id=p.author_id"
            " WHERE p.id=?", (post_id,)).fetchone()
        if not post:
            return None
        replies = con.execute(
            "SELECT r.id,r.author_id,r.body,r.created_at,u.display_name AS author"
            " FROM team_replies r LEFT JOIN users u ON u.id=r.author_id"
            " WHERE r.post_id=? ORDER BY r.created_at", (post_id,)).fetchall()
    detail = {"post": dict(post), "replies": [dict(r) for r in replies]}
    if mask_authors:
        _mask_team_author(detail["post"])
        for reply in detail["replies"]:
            _mask_team_author(reply)
    return detail


def create_team_post(author_id: str, raw: dict) -> dict:
    data = validate_team_post(raw)
    post_id = str(uuid.uuid4())
    now = utc_now()
    init_db()
    with connect() as con:
        con.execute("INSERT INTO team_posts(id,author_id,track,title,body,needed,status,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?, 'open', ?, ?)",
                    (post_id, author_id, data["track"], data["title"], data["body"], data["needed"], now, now))
    return {"id": post_id, **data, "status": "open", "created_at": now}


def add_team_reply(post_id: str, author_id: str, body: object) -> dict:
    text = clean_text(body, "回复内容", required=True, maximum=1000)
    init_db()
    with connect() as con:
        if not con.execute("SELECT 1 FROM team_posts WHERE id=?", (post_id,)).fetchone():
            raise ValueError("帖子不存在或已被删除")
        reply_id = str(uuid.uuid4())
        now = utc_now()
        con.execute("INSERT INTO team_replies(id,post_id,author_id,body,created_at) VALUES(?,?,?,?,?)",
                    (reply_id, post_id, author_id, text, now))
        con.execute("UPDATE team_posts SET updated_at=? WHERE id=?", (now, post_id))
    return {"id": reply_id, "post_id": post_id, "body": text, "created_at": now}


def set_team_post_status(post_id: str, status: str) -> bool:
    if status not in ("open", "closed"):
        raise ValueError("状态只能是 open 或 closed")
    init_db()
    with connect() as con:
        cur = con.execute("UPDATE team_posts SET status=?,updated_at=? WHERE id=?", (status, utc_now(), post_id))
    return bool(cur.rowcount)


def delete_team_post(post_id: str) -> bool:
    init_db()
    with connect() as con:
        con.execute("DELETE FROM team_replies WHERE post_id=?", (post_id,))
        cur = con.execute("DELETE FROM team_posts WHERE id=?", (post_id,))
    return bool(cur.rowcount)


def ai_chat(api_key: str, model: str, prompt: str) -> tuple[bool, str]:
    """把一次生成请求转发给模型服务（默认 DeepSeek）。密钥只在本次请求中使用，不落库、不写日志。"""
    key = str(api_key or "").strip()
    if not key:
        return False, "缺少 API Key"
    if model not in AI_MODELS:
        model = AI_MODELS[0]
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 1600,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        AI_BASE_URL + "/chat/completions", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=AI_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = str((body.get("error") or {}).get("message") or "")[:300]
        except Exception:
            detail = ""
        if exc.code == 401:
            return False, "API Key 无效或已失效（服务返回 401）"
        if exc.code == 402:
            return False, "账户余额或额度不足（服务返回 402）"
        if exc.code == 429:
            return False, "请求过于频繁或额度用尽（服务返回 429）"
        return False, f"调用失败（HTTP {exc.code}）" + (f"：{detail}" if detail else "")
    except Exception as exc:
        return False, f"无法连接模型服务：{type(exc).__name__}"
    try:
        text = data["choices"][0]["message"]["content"]
    except Exception:
        return False, "返回内容无法解析"
    return True, str(text).strip()


def clean_text(value: object, key: str, *, required: bool = False, maximum: int = 4000) -> str:
    if not isinstance(value, str):
        if required:
            raise ValueError(f"{key} 必须填写")
        return ""
    text = value.strip()
    if required and not text:
        raise ValueError(f"{key} 必须填写")
    if len(text) > maximum:
        raise ValueError(f"{key} 不能超过 {maximum} 个字符")
    return text


def validate_news(raw: dict) -> dict:
    featured = raw.get("featured", False)
    if not isinstance(featured, bool):
        raise ValueError("头条状态须为布尔值")
    item = {
        "category": clean_text(raw.get("category"), "分类", maximum=120) or "班级动态 / CLASS UPDATE",
        "title_cn": clean_text(raw.get("title_cn"), "中文标题", required=True, maximum=180),
        "title_en": clean_text(raw.get("title_en"), "英文标题", maximum=220),
        "date": clean_text(raw.get("date"), "日期", maximum=32),
        "summary_cn": clean_text(raw.get("summary_cn"), "中文摘要", maximum=1200),
        "summary_en": clean_text(raw.get("summary_en"), "英文摘要", maximum=1600),
        "body_cn": clean_text(raw.get("body_cn"), "中文正文", maximum=10000),
        "body_en": clean_text(raw.get("body_en"), "英文正文", maximum=12000),
        "source_url": clean_text(raw.get("source_url"), "原文链接", maximum=1000),
        "image_url": clean_text(raw.get("image_url"), "图片路径", maximum=1000),
        "featured": featured,
    }
    if item["source_url"] and not item["source_url"].startswith(("https://", "http://")):
        raise ValueError("原文链接必须以 http:// 或 https:// 开头")
    return item


def validate_event(raw: dict) -> dict:
    phase = clean_text(raw.get("phase"), "活动阶段", maximum=20) or "upcoming"
    if phase not in ("upcoming", "past"):
        phase = "upcoming"
    item = {
        "title_cn": clean_text(raw.get("title_cn"), "活动中文标题", required=True, maximum=180),
        "title_en": clean_text(raw.get("title_en"), "活动英文标题", maximum=220),
        "date": clean_text(raw.get("date"), "活动日期", required=True, maximum=32),
        "category": clean_text(raw.get("category"), "活动分类", maximum=120),
        "location": clean_text(raw.get("location"), "地点", maximum=220),
        "summary_cn": clean_text(raw.get("summary_cn"), "中文简介", maximum=1200),
        "summary_en": clean_text(raw.get("summary_en"), "英文简介", maximum=1600),
        "source_url": clean_text(raw.get("source_url"), "原文链接", maximum=1000),
        "phase": phase,
    }
    if item["source_url"] and not item["source_url"].startswith(("https://", "http://")):
        raise ValueError("原文链接必须以 http:// 或 https:// 开头")
    return item


def validate_schedule(raw: dict) -> dict:
    term_start = clean_text(raw.get("term_start"), "教学周起始日期", required=True, maximum=10)
    date.fromisoformat(term_start)
    lessons = raw.get("lessons")
    if not isinstance(lessons, list) or len(lessons) > 120:
        raise ValueError("课表必须是最多 120 条课程的列表")
    result = []
    for entry in lessons:
        if not isinstance(entry, dict):
            raise ValueError("课表课程格式不正确")
        d, p = entry.get("d"), entry.get("p")
        if not isinstance(d, int) or not 0 <= d <= 6 or not isinstance(p, int) or not 1 <= p <= 13:
            raise ValueError("星期或节次超出允许范围")
        name = clean_text(entry.get("n"), "课程名", required=True, maximum=180)
        ranges = entry.get("weeks")
        if not isinstance(ranges, list) or not ranges or len(ranges) > 10:
            raise ValueError("课程周次范围格式不正确")
        normalized = []
        for pair in ranges:
            if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(x, int) for x in pair):
                raise ValueError("每段周次须为 [开始周, 结束周]")
            if not (1 <= pair[0] <= pair[1] <= 52):
                raise ValueError("周次须在第 1–52 周内，且开始周不晚于结束周")
            normalized.append(pair)
        result.append({"d": d, "p": p, "n": name, "weeks": normalized})
    return {"term_start": term_start, "lessons": result}


class Handler(BaseHTTPRequestHandler):
    server_version = "DMTClassSite/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # Keep routine request logs; avoid ever logging request bodies/passwords.
        super().log_message(fmt, *args)

    def send_json(self, status: int, value: object, *, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("请求内容为空或过大")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求格式必须为 JSON 对象")
        return value

    def client_key(self) -> str:
        return self.client_address[0]

    def limited(self, action: str, maximum: int = 8) -> bool:
        now = time.time()
        key = f"{action}:{self.client_key()}"
        hits = [x for x in RATE_LIMIT.get(key, []) if now - x < 900]
        if len(hits) >= maximum:
            RATE_LIMIT[key] = hits
            return True
        hits.append(now)
        RATE_LIMIT[key] = hits
        return False

    def origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return False
        configured = os.environ.get("SITE_ORIGIN", "").rstrip("/")
        if configured:
            return origin.rstrip("/") == configured
        scheme = self.headers.get("X-Forwarded-Proto", self.server.scheme).split(",")[0].strip()
        expected = f"{scheme}://{self.headers.get('Host', '')}".rstrip("/")
        return origin.rstrip("/") == expected

    def require_origin(self) -> bool:
        if not self.origin_allowed():
            self.send_error_json(HTTPStatus.FORBIDDEN, "请求来源校验失败")
            return False
        return True

    def session(self) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookies.get(COOKIE)
        if not morsel:
            return None, None
        token_hash = hashlib.sha256(morsel.value.encode()).hexdigest()
        with connect() as con:
            row = con.execute("SELECT s.*,u.id,u.display_name,u.email,u.role,u.email_verified,u.account_type,u.created_at AS account_created_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                              (token_hash, utc_now())).fetchone()
        if not row:
            return None, None
        return row, row

    def require_user(self, roles: set[str] | None = None) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        session, user = self.session()
        if not user:
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "请先登录")
            return None, None
        if roles is not None and user["role"] not in roles:
            self.send_error_json(HTTPStatus.FORBIDDEN, "当前账号没有此操作权限")
            return None, None
        return session, user

    def check_csrf(self, session: sqlite3.Row) -> bool:
        if not self.require_origin():
            return False
        supplied = self.headers.get("X-CSRF-Token", "")
        if not supplied or not hmac.compare_digest(supplied, session["csrf_token"]):
            self.send_error_json(HTTPStatus.FORBIDDEN, "安全校验已过期，请刷新后重试")
            return False
        return True

    def audit(self, actor: str, action: str, entity: str, entity_id: str) -> None:
        with connect() as con:
            con.execute("INSERT INTO audit_log(actor_id,action,entity,entity_id,created_at) VALUES(?,?,?,?,?)",
                        (actor, action, entity, entity_id, utc_now()))

    def read_items(self, table: str) -> list[dict]:
        with connect() as con:
            rows = con.execute(f"SELECT id,payload FROM {table}").fetchall()
        items = [json.loads(row["payload"]) | {"id": row["id"]} for row in rows]
        if table == "news":
            items.sort(key=lambda x: (bool(x.get("featured")), x.get("date", ""), x.get("id", "")), reverse=True)
        else:
            items.sort(key=lambda x: (x.get("date", ""), x.get("id", "")))
        return items

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
        if path == "/api/config":
            return self.send_json(200, {"registration_mode": registration_mode(),
                                        "email_verification": email_verification_mode(),
                                        "mailer_configured": mailer_configured()})
        if path == "/api/notifications":
            _, viewer = self.session()
            return self.send_json(200, notification_payload(viewer))
        if path == "/api/me":
            _, user = self.session()
            if not user:
                return self.send_json(200, {"user": None})
            return self.send_json(200, {"user": {"id": user["id"], "display_name": user["display_name"],
                "email": user["email"], "role": user["role"], "role_label": ROLE_LABELS[user["role"]],
                "email_verified": user["email_verified"], "account_type": account_type_of(user),
                "created_at": user["account_created_at"]},
                "csrf_token": user["csrf_token"]})
        if path == "/api/news":
            return self.send_json(200, self.read_items("news"))
        if path == "/api/events":
            return self.send_json(200, self.read_items("events"))
        if path == "/api/resources":
            _, viewer = self.session()
            if not is_member(viewer):
                return self.send_error_json(403, VISITOR_BLOCKED)
            with connect() as con:
                rows = con.execute("SELECT id,title,description,category,original_name,mime_type,size,uploaded_by,created_at FROM resources ORDER BY created_at DESC").fetchall()
            return self.send_json(200, [dict(row) for row in rows])
        match = re.fullmatch(r"/api/resources/([0-9a-f-]{36})/download", path)
        if match:
            _, download_user = self.session()
            if not is_member(download_user):
                return self.send_error_json(403, VISITOR_BLOCKED)
            if email_verification_mode() == "gate" and not (download_user and download_user["email_verified"]):
                return self.send_error_json(403, "请先完成邮箱验证后再下载资料。")
            with connect() as con:
                resource = con.execute("SELECT original_name,stored_name,mime_type FROM resources WHERE id=?", (match.group(1),)).fetchone()
            if not resource:
                return self.send_error_json(404, "资料不存在")
            target = (UPLOAD_DIR / resource["stored_name"]).resolve()
            if UPLOAD_DIR.resolve() not in target.parents or not target.is_file():
                return self.send_error_json(404, "资料文件不存在")
            from urllib.parse import quote
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f"attachment; filename=resource-{match.group(1)}{Path(resource['original_name']).suffix}; filename*=UTF-8''{quote(resource['original_name'])}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/schedule":
            _, viewer = self.session()
            if not is_member(viewer):
                return self.send_error_json(403, VISITOR_BLOCKED)
            with connect() as con:
                row = con.execute("SELECT value FROM site_settings WHERE key='schedule'").fetchone()
            return self.send_json(200, json.loads(row["value"]) if row else {"term_start": "2026-09-07", "lessons": []})
        if path == "/api/users":
            session, user = self.require_user({"admin"})
            if not user:
                return
            with connect() as con:
                rows = con.execute("SELECT id,display_name,email,role,email_verified,account_type,created_at FROM users ORDER BY created_at").fetchall()
            return self.send_json(200, [dict(row) | {"role_label": ROLE_LABELS[row["role"]]} for row in rows])
        if path == "/api/invites":
            _, user = self.require_user({"admin"})
            if not user:
                return
            return self.send_json(200, {"registration_mode": registration_mode(), "invites": invite_list(),
                                        "email_verification": email_verification_mode(),
                                        "email_domain_check": email_domain_check_mode(),
                                        "email_allowed_domains": email_allowed_domains(),
                                        "mailer_configured": mailer_configured(),
                                        "roles": [{"value": r, "label": INVITE_ROLE_LABELS[r]} for r in INVITE_ROLES]})
        if path == "/api/team":
            # 组队大厅对未登录访客开放只读浏览（发帖人显示为“班级成员”），发帖与回复仍需登录
            _, viewer = self.session()
            return self.send_json(200, {"posts": team_post_list(mask_authors=not viewer),
                                        "tracks": list(TEAM_TRACKS),
                                        "viewer": account_type_of(viewer) or "guest"})
        team_view = re.fullmatch(r"/api/team/([0-9a-f-]{36})", path)
        if team_view:
            _, viewer = self.session()
            detail = team_post_detail(team_view.group(1), mask_authors=not viewer)
            if not detail:
                return self.send_error_json(404, "帖子不存在或已被删除")
            return self.send_json(200, detail)
        if path == "/api/feedback":
            _, user = self.require_user()
            if not user:
                return
            if is_visitor(user):
                return self.send_error_json(403, VISITOR_BLOCKED)
            if user["role"] != "admin":
                mine = feedback_list(user_id=user["id"])
                return self.send_json(200, {"items": mine, "scope": "mine",
                                            "mailer_configured": mailer_configured()})
            return self.send_json(200, {"items": feedback_list(), "scope": "all",
                                        "mailer_configured": mailer_configured(),
                                        "recipients": feedback_admin_emails()})
        if path == "/api/audit":
            _, user = self.require_user({"admin"})
            if not user:
                return
            with connect() as con:
                rows = con.execute("SELECT a.action,a.entity,a.entity_id,a.created_at,u.display_name AS actor FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 100").fetchall()
            return self.send_json(200, [dict(row) for row in rows])
        if path.startswith("/api/"):
            return self.send_error_json(404, "找不到此接口")
        return self.serve_file(path)

    def serve_file(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (ROOT / rel).resolve()
        if ROOT not in target.parents and target != ROOT:
            return self.send_error_json(404, "找不到页面")
        if not target.is_file() or target == DB_PATH or "var" in target.parts:
            return self.send_error_json(404, "找不到页面")
        data = target.read_bytes()
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript", "application/json"):
            mime += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/resources":
            session, user = self.require_user(CONTENT_ROLES)
            if not user or not self.check_csrf(session):
                return
            if self.limited("upload", 20):
                return self.send_error_json(429, "上传操作太频繁，请稍后重试")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_RESOURCE_BYTES + 1024 * 1024:
                    raise ValueError("文件为空或超过 25 MB 限制")
                content_type = self.headers.get("Content-Type", "")
                if not content_type.lower().startswith("multipart/form-data;"):
                    raise ValueError("请使用文件上传表单")
                raw = self.rfile.read(length)
                message = BytesParser(policy=email_policy).parsebytes(
                    f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + raw
                )
                fields = {}
                uploaded = None
                if message.is_multipart():
                    for part in message.iter_parts():
                        if part.get_content_disposition() != "form-data":
                            continue
                        name = part.get_param("name", header="content-disposition")
                        filename = part.get_filename()
                        payload = part.get_payload(decode=True) or b""
                        if name == "file" and filename:
                            uploaded = (filename, payload)
                        elif name in {"title", "description", "category"}:
                            fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                if not uploaded or not uploaded[1]:
                    raise ValueError("请选择要上传的文件")
                original_name = Path(uploaded[0].replace("\\", "/")).name.strip()
                suffix = Path(original_name).suffix.lower()
                if not original_name or suffix not in RESOURCE_EXTENSIONS:
                    raise ValueError("该文件类型暂不支持")
                if len(uploaded[1]) > MAX_RESOURCE_BYTES:
                    raise ValueError("单个文件不能超过 25 MB")
                title = clean_text(fields.get("title") or Path(original_name).stem, "资料名称", required=True, maximum=120)
                description = clean_text(fields.get("description", ""), "资料说明", maximum=1000)
                category = clean_text(fields.get("category", "班级共享"), "资料分类", maximum=60) or "班级共享"
                resource_id = str(uuid.uuid4())
                stored_name = resource_id + suffix
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                (UPLOAD_DIR / stored_name).write_bytes(uploaded[1])
                mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
                created_at = utc_now()
                try:
                    with connect() as con:
                        con.execute("INSERT INTO resources(id,title,description,category,original_name,stored_name,mime_type,size,uploaded_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                    (resource_id, title, description, category, original_name, stored_name, mime_type, len(uploaded[1]), user["id"], created_at))
                except Exception:
                    (UPLOAD_DIR / stored_name).unlink(missing_ok=True)
                    raise
                self.audit(user["id"], "upload", "resource", resource_id)
                return self.send_json(201, {"id": resource_id, "title": title, "description": description, "category": category,
                    "original_name": original_name, "mime_type": mime_type, "size": len(uploaded[1]), "uploaded_by": user["id"], "created_at": created_at})
            except (ValueError, UnicodeError) as exc:
                return self.send_error_json(400, str(exc))
        if path == "/api/logout":
            session, user = self.require_user()
            if not user or not self.check_csrf(session):
                return
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookies.get(COOKIE)
            if morsel:
                token_hash = hashlib.sha256(morsel.value.encode()).hexdigest()
                with connect() as con:
                    con.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            return self.send_json(200, {"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
        if path == "/api/invites":
            session, user = self.require_user({"admin"})
            if not user or not self.check_csrf(session):
                return
            try:
                raw = self.body()
                invite = create_invite(raw.get("label", ""), str(raw.get("role", "member")),
                                       int(raw.get("max_uses", 1)), int(raw.get("expires_in_days", 0)), user["id"])
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc) or "邀请码参数不正确")
            self.audit(user["id"], "create-invite:" + invite["role"], "invite", invite["id"])
            return self.send_json(201, invite)
        if path in ("/api/verify/request", "/api/verify/confirm", "/api/verify/test"):
            if not self.require_origin():
                return
            if path == "/api/verify/test":
                session, user = self.require_user({"admin"})
                if not user or not self.check_csrf(session):
                    return
                if self.limited("mail-test", 5):
                    return self.send_error_json(429, "测试邮件发送过于频繁，请稍后再试")
                try:
                    raw = self.body()
                except (ValueError, json.JSONDecodeError) as exc:
                    return self.send_error_json(400, str(exc))
                target = clean_text(raw.get("email"), "邮箱", required=True, maximum=254).lower()
                if not valid_email(target):
                    return self.send_error_json(400, "请输入有效邮箱")
                sent, detail = send_mail(target, "DMT CLASS 01 测试邮件 / Test message",
                                         "这是一封来自 DMT CLASS 01 班级网站的测试邮件。\n\n"
                                         "If you received this message, the class site mailer works.")
                self.audit(user["id"], "test-mail", "email", target)
                return self.send_json(200, {"sent": sent, "detail": detail})
            try:
                raw = self.body()
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc))
            if path == "/api/verify/request":
                if self.limited("verify-request", 6):
                    return self.send_error_json(429, "操作太频繁，请 15 分钟后再试")
                email = clean_text(raw.get("email"), "邮箱", required=True, maximum=254).lower()
                if not valid_email(email):
                    return self.send_error_json(400, "请输入有效邮箱")
                with connect() as con:
                    target_user = con.execute("SELECT id,email,email_verified FROM users WHERE email=?", (email,)).fetchone()
                if not target_user:
                    return self.send_json(200, {"ok": True, "sent": False})
                if target_user["email_verified"]:
                    return self.send_json(200, {"ok": True, "sent": False, "already_verified": True})
                if email_verification_mode() == "off":
                    return self.send_error_json(400, "当前未开启邮箱验证。")
                if not mailer_configured():
                    return self.send_error_json(503, "服务器尚未配置发信账号，请联系管理员。")
                code = issue_email_code(target_user["id"])
                sent, detail = send_verification_email(email, code)
                self.audit(target_user["id"], "request-verify", "user", target_user["id"])
                if not sent:
                    print(f"[email] 重发验证码失败 {email}: {detail}", file=sys.stderr)
                    return self.send_error_json(502, "验证码发送失败，请稍后重试或联系管理员。")
                return self.send_json(200, {"ok": True, "sent": True, "expires_minutes": EMAIL_CODE_TTL_MINUTES})
            if self.limited("verify-confirm", 20):
                return self.send_error_json(429, "尝试次数过多，请 15 分钟后再试")
            email = clean_text(raw.get("email"), "邮箱", required=True, maximum=254).lower()
            ok, message, verified_user = confirm_email_code(email, raw.get("code"))
            if not ok:
                return self.send_error_json(400, message)
            if verified_user:
                self.audit(verified_user, "verify-email", "user", verified_user)
            return self.send_json(200, {"ok": True, "message": message})
        if path == "/api/ai/resume":
            session, user = self.require_user()
            if not user or not self.check_csrf(session):
                return
            if self.limited("ai-resume", 30):
                return self.send_error_json(429, "生成请求太频繁，请过一会儿再试")
            try:
                raw = self.body()
                api_key = clean_text(raw.get("apiKey"), "API Key", required=True, maximum=200)
                prompt = clean_text(raw.get("prompt"), "提示词", required=True, maximum=AI_MAX_PROMPT)
                model = str(raw.get("model") or AI_MODELS[0]).strip()
                if model not in AI_MODELS:
                    model = AI_MODELS[0]
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc))
            ok, result = ai_chat(api_key, model, prompt)
            if not ok:
                return self.send_error_json(502, result)
            self.audit(user["id"], "ai-generate:" + model, "ai", model)
            return self.send_json(200, {"text": result, "model": model})
        if path == "/api/team" or re.fullmatch(r"/api/team/([0-9a-f-]{36})/replies", path):
            session, user = self.require_user()
            if not user or not self.check_csrf(session):
                return
            reply_match = re.fullmatch(r"/api/team/([0-9a-f-]{36})/replies", path)
            try:
                raw = self.body()
                if reply_match:
                    if self.limited("team-reply", 30):
                        return self.send_error_json(429, "回复太频繁，请稍后再试")
                    reply = add_team_reply(reply_match.group(1), user["id"], raw.get("body"))
                    return self.send_json(201, reply)
                if self.limited("team-post", 10):
                    return self.send_error_json(429, "发帖太频繁，请稍后再试")
                post = create_team_post(user["id"], raw)
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc))
            self.audit(user["id"], "create", "team-post", post["id"])
            return self.send_json(201, post)
        if path == "/api/feedback":
            session, user = self.require_user()
            if not user or not self.check_csrf(session):
                return
            if self.limited("feedback", 10):
                return self.send_error_json(429, "提交太频繁，请稍后再试")
            try:
                raw = self.body()
                result = post_feedback(user, raw)
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc))
            self.audit(user["id"], "feedback:" + result["category"], "feedback", result["id"])
            return self.send_json(201, result)
        if path not in ("/api/register", "/api/login"):
            session, user = self.require_user(CONTENT_ROLES)
            if not user:
                return
            if not self.check_csrf(session):
                return
            return self.handle_editor_post(path, user)
        if not self.require_origin():
            return
        action = "auth" if path.endswith("login") else "register"
        if self.limited(action, 8):
            return self.send_error_json(429, "操作太频繁，请 15 分钟后重试")
        try:
            raw = self.body()
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self.send_error_json(400, str(exc))
        if path == "/api/register":
            try:
                return self.register(raw)
            except ValueError as exc:
                return self.send_error_json(400, str(exc))
        try:
            return self.login(raw)
        except ValueError as exc:
            return self.send_error_json(400, str(exc))

    def register(self, raw: dict) -> None:
        mode = registration_mode()
        if mode == "closed":
            return self.send_error_json(403, "注册已关闭，请联系管理员邀请。")
        display_name = clean_text(raw.get("display_name"), "姓名或昵称", required=True, maximum=60)
        email = clean_text(raw.get("email"), "邮箱", required=True, maximum=254).lower()
        password = raw.get("password", "")
        if not valid_email(email):
            return self.send_error_json(400, "请输入有效邮箱")
        domain_ok, domain_note = check_email_address(email)
        if not domain_ok:
            return self.send_error_json(400, domain_note)
        if domain_note:
            print(f"[email] {email} 域名校验提示：{domain_note}", file=sys.stderr)
        if not isinstance(password, str) or len(password) < 12 or len(password) > 128:
            return self.send_error_json(400, "密码长度须为 12–128 个字符")
        account_type = str(raw.get("account_type") or "member").strip().lower()
        if account_type not in ACCOUNT_TYPES:
            account_type = "member"
        invite = None
        if mode == "invite" and account_type != "visitor":
            code = normalize_invite_code(raw.get("invite_code"))
            if not code:
                return self.send_error_json(403, "请填写邀请码；邀请码可向管理员索取。")
            with connect() as con:
                invite = con.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
            if not invite or not invite_available(invite):
                return self.send_error_json(403, "邀请码无效、已用尽或已过期，请向管理员确认。")
        role = invite["role"] if invite else "member"
        if account_type == "visitor":
            role = "member"
        salt, digest = hash_password(password)
        user_id = str(uuid.uuid4())
        try:
            with connect() as con:
                con.execute("INSERT INTO users(id,display_name,email,password_salt,password_hash,role,created_at,account_type)"
                            " VALUES(?,?,?,?,?,?,?,?)",
                            (user_id, display_name, email, salt, digest, role, utc_now(), account_type))
                if invite:
                    con.execute("UPDATE invite_codes SET used_count=used_count+1 WHERE id=?", (invite["id"],))
        except sqlite3.IntegrityError:
            return self.send_error_json(409, "此邮箱已注册，请直接登录")
        self.audit(user_id, "register", "user", user_id)
        if invite:
            self.audit(user_id, "register-invite:" + (invite["label"] or "未命名邀请码"), "invite", invite["id"])
        extra: dict = {}
        if email_verification_mode() != "off":
            code = issue_email_code(user_id)
            sent, detail = send_verification_email(email, code)
            extra["email_verification"] = "sent" if sent else "failed"
            if not sent:
                print(f"[email] 向 {email} 发送验证码失败：{detail}", file=sys.stderr)
        return self.create_session(user_id, extra)

    def login(self, raw: dict) -> None:
        email = clean_text(raw.get("email"), "邮箱", required=True, maximum=254).lower()
        password = raw.get("password", "")
        with connect() as con:
            user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        valid = False
        if user and isinstance(password, str):
            _, attempt = hash_password(password, user["password_salt"])
            valid = hmac.compare_digest(attempt, user["password_hash"])
        if not valid:
            return self.send_error_json(401, "邮箱或密码不正确")
        with connect() as con:
            con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        self.audit(user["id"], "login", "user", user["id"])
        return self.create_session(user["id"])

    def create_session(self, user_id: str, extra: dict | None = None) -> None:
        token = secrets.token_urlsafe(40)
        csrf = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with connect() as con:
            con.execute("DELETE FROM sessions WHERE expires_at<=?", (utc_now(),))
            con.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)",
                        (token_hash, user_id, csrf, expires, now.isoformat(timespec="seconds")))
            user = con.execute("SELECT id,display_name,email,role,email_verified,account_type,created_at FROM users WHERE id=?",
                               (user_id,)).fetchone()
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", self.server.scheme).split(",")[0].strip() == "https" else ""
        cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS*86400}{secure}"
        payload = {"user": dict(user) | {"role_label": ROLE_LABELS[user["role"]]}, "csrf_token": csrf}
        payload.update(extra or {})
        return self.send_json(200, payload, headers={"Set-Cookie": cookie})

    def handle_editor_post(self, path: str, user: sqlite3.Row) -> None:
        try:
            raw = self.body()
            if path == "/api/news":
                item = validate_news(raw)
                item_id = str(uuid.uuid4())
                item["id"] = item_id
                with connect() as con:
                    if item["featured"]:
                        for row in con.execute("SELECT id,payload FROM news").fetchall():
                            old = json.loads(row["payload"])
                            if old.get("featured"):
                                old["featured"] = False
                                con.execute("UPDATE news SET payload=?,updated_at=?,updated_by=? WHERE id=?",
                                            (json.dumps(old, ensure_ascii=False), utc_now(), user["id"], row["id"]))
                    con.execute("INSERT INTO news(id,payload,updated_at,updated_by) VALUES(?,?,?,?)",
                                (item_id, json.dumps(item, ensure_ascii=False), utc_now(), user["id"]))
                self.audit(user["id"], "create", "news", item_id)
                return self.send_json(201, item)
            if path == "/api/events":
                item = validate_event(raw)
                item_id = str(uuid.uuid4()); item["id"] = item_id
                with connect() as con:
                    con.execute("INSERT INTO events(id,payload,updated_at,updated_by) VALUES(?,?,?,?)",
                                (item_id, json.dumps(item, ensure_ascii=False), utc_now(), user["id"]))
                self.audit(user["id"], "create", "event", item_id)
                return self.send_json(201, item)
            return self.send_error_json(404, "找不到此接口")
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_error_json(400, str(exc))

    def do_PUT(self) -> None:
        path = unquote(urlparse(self.path).path)
        session, user = self.require_user(CONTENT_ROLES)
        if not user or not self.check_csrf(session):
            return
        try:
            raw = self.body()
            if path == "/api/schedule":
                schedule = validate_schedule(raw)
                with connect() as con:
                    con.execute("UPDATE site_settings SET value=?,updated_at=?,updated_by=? WHERE key='schedule'",
                                (json.dumps(schedule, ensure_ascii=False), utc_now(), user["id"]))
                self.audit(user["id"], "update", "schedule", "term")
                return self.send_json(200, schedule)
            match = re.fullmatch(r"/api/(news|events)/([0-9a-zA-Z_-]+)", path)
            if match:
                table, item_id = match.groups()
                payload = validate_news(raw) if table == "news" else validate_event(raw)
                payload["id"] = item_id
                with connect() as con:
                    exists = con.execute(f"SELECT 1 FROM {table} WHERE id=?", (item_id,)).fetchone()
                    if not exists:
                        return self.send_error_json(404, "内容不存在")
                    if table == "news" and payload["featured"]:
                        for row in con.execute("SELECT id,payload FROM news WHERE id<>?", (item_id,)).fetchall():
                            old = json.loads(row["payload"])
                            if old.get("featured"):
                                old["featured"] = False
                                con.execute("UPDATE news SET payload=?,updated_at=?,updated_by=? WHERE id=?",
                                            (json.dumps(old, ensure_ascii=False), utc_now(), user["id"], row["id"]))
                    con.execute(f"UPDATE {table} SET payload=?,updated_at=?,updated_by=? WHERE id=?",
                                (json.dumps(payload, ensure_ascii=False), utc_now(), user["id"], item_id))
                self.audit(user["id"], "update", "news" if table == "news" else "event", item_id)
                return self.send_json(200, payload)
            return self.send_error_json(404, "找不到此接口")
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_error_json(400, str(exc))

    def do_PATCH(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/email-verification":
            session, user = self.require_user({"admin"})
            if not user or not self.check_csrf(session):
                return
            try:
                raw = self.body()
                mode = set_email_verification_mode(str(raw.get("mode", "")).strip().lower())
                if "domain_check" in raw:
                    set_email_domain_check_mode(str(raw.get("domain_check", "")).strip().lower())
                if "domains" in raw:
                    set_email_allowed_domains(raw.get("domains"))
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc))
            self.audit(user["id"], "set-email-verification:" + mode, "setting", "email_verification")
            return self.send_json(200, {"email_verification": mode,
                                        "email_domain_check": email_domain_check_mode(),
                                        "email_allowed_domains": email_allowed_domains(),
                                        "mailer_configured": mailer_configured()})
        if path == "/api/site-notice":
            session, user = self.require_user({"admin"})
            if not user or not self.check_csrf(session):
                return
            try:
                raw = self.body()
                value = set_site_notice(raw.get("notice", ""))
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc))
            self.audit(user["id"], "set-notice", "setting", "site_notice")
            return self.send_json(200, {"ok": True, "notice": value})
        feedback_patch = re.fullmatch(r"/api/feedback/([0-9a-f-]{36})", path)
        if feedback_patch:
            session, user = self.require_user({"admin"})
            if not user or not self.check_csrf(session):
                return
            try:
                raw = self.body()
                ok = handle_feedback(feedback_patch.group(1), str(raw.get("status", "")).strip(),
                                     raw.get("response", ""), user["id"])
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc))
            if not ok:
                return self.send_error_json(404, "反馈不存在")
            self.audit(user["id"], "handle-feedback:" + str(raw.get("status", "")).strip(), "feedback", feedback_patch.group(1))
            return self.send_json(200, {"ok": True})
        team_status = re.fullmatch(r"/api/team/([0-9a-f-]{36})", path)
        if team_status:
            session, user = self.require_user()
            if not user or not self.check_csrf(session):
                return
            with connect() as con:
                post = con.execute("SELECT author_id FROM team_posts WHERE id=?", (team_status.group(1),)).fetchone()
            if not post:
                return self.send_error_json(404, "帖子不存在或已被删除")
            if post["author_id"] != user["id"] and user["role"] not in CONTENT_ROLES:
                return self.send_error_json(403, "只有发帖人或管理员可以修改状态")
            try:
                raw = self.body()
                status = str(raw.get("status", "")).strip().lower()
                set_team_post_status(team_status.group(1), status)
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc))
            self.audit(user["id"], "set-status:" + status, "team-post", team_status.group(1))
            return self.send_json(200, {"ok": True, "status": status})
        if path == "/api/registration":
            session, user = self.require_user({"admin"})
            if not user or not self.check_csrf(session):
                return
            try:
                raw = self.body()
                mode = set_registration_mode(str(raw.get("mode", "")).strip().lower())
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_error_json(400, str(exc))
            self.audit(user["id"], "set-registration:" + mode, "setting", "registration_mode")
            return self.send_json(200, {"registration_mode": mode})
        if path == "/api/me":
            session, user = self.require_user()
            if not user or not self.check_csrf(session):
                return
            try:
                raw = self.body()
                name = clean_text(raw.get("display_name"), "姓名或昵称", required=True, maximum=60)
            except ValueError as exc:
                return self.send_error_json(400, str(exc))
            with connect() as con:
                con.execute("UPDATE users SET display_name=? WHERE id=?", (name, user["id"]))
            self.audit(user["id"], "update", "profile", user["id"])
            return self.send_json(200, {"display_name": name})
        if path.startswith("/api/users/"):
            session, user = self.require_user({"admin"})
            if not user or not self.check_csrf(session):
                return
            target_id = path.rsplit("/", 1)[1]
            try:
                raw = self.body(); role = raw.get("role")
                if role not in ROLES:
                    raise ValueError("未知的账号角色")
            except ValueError as exc:
                return self.send_error_json(400, str(exc))
            with connect() as con:
                target = con.execute("SELECT id,role FROM users WHERE id=?", (target_id,)).fetchone()
                if not target:
                    return self.send_error_json(404, "账号不存在")
                admins = con.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
                if target["role"] == "admin" and role != "admin" and admins <= 1:
                    return self.send_error_json(400, "至少保留一名管理员")
                con.execute("UPDATE users SET role=? WHERE id=?", (role, target_id))
            self.audit(user["id"], "set-role:" + role, "user", target_id)
            return self.send_json(200, {"id": target_id, "role": role, "role_label": ROLE_LABELS[role]})
        return self.send_error_json(404, "找不到此接口")

    def do_DELETE(self) -> None:
        path = unquote(urlparse(self.path).path)
        team_delete = re.fullmatch(r"/api/team/([0-9a-f-]{36})", path)
        if team_delete:
            session, user = self.require_user()
            if not user or not self.check_csrf(session):
                return
            with connect() as con:
                post = con.execute("SELECT author_id FROM team_posts WHERE id=?", (team_delete.group(1),)).fetchone()
            if not post:
                return self.send_error_json(404, "帖子不存在或已被删除")
            if post["author_id"] != user["id"] and user["role"] not in CONTENT_ROLES:
                return self.send_error_json(403, "只有发帖人或管理员可以删除")
            if not delete_team_post(team_delete.group(1)):
                return self.send_error_json(404, "帖子不存在或已被删除")
            self.audit(user["id"], "delete", "team-post", team_delete.group(1))
            return self.send_json(200, {"ok": True})
        invite_match = re.fullmatch(r"/api/invites/([0-9a-f-]{36})", path)
        if invite_match:
            session, user = self.require_user({"admin"})
            if not user or not self.check_csrf(session):
                return
            with connect() as con:
                cur = con.execute("DELETE FROM invite_codes WHERE id=?", (invite_match.group(1),))
            if not cur.rowcount:
                return self.send_error_json(404, "邀请码不存在")
            self.audit(user["id"], "delete", "invite", invite_match.group(1))
            return self.send_json(200, {"ok": True})
        session, user = self.require_user(CONTENT_ROLES)
        if not user or not self.check_csrf(session):
            return
        resource_match = re.fullmatch(r"/api/resources/([0-9a-f-]{36})", path)
        if resource_match:
            resource_id = resource_match.group(1)
            with connect() as con:
                item = con.execute("SELECT stored_name FROM resources WHERE id=?", (resource_id,)).fetchone()
                if not item:
                    return self.send_error_json(404, "资料不存在")
                con.execute("DELETE FROM resources WHERE id=?", (resource_id,))
            (UPLOAD_DIR / item["stored_name"]).unlink(missing_ok=True)
            self.audit(user["id"], "delete", "resource", resource_id)
            return self.send_json(200, {"ok": True})
        match = re.fullmatch(r"/api/(news|events)/([0-9a-zA-Z_-]+)", path)
        if not match:
            return self.send_error_json(404, "找不到此接口")
        table, item_id = match.groups()
        with connect() as con:
            cur = con.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        if not cur.rowcount:
            return self.send_error_json(404, "内容不存在")
        self.audit(user["id"], "delete", "news" if table == "news" else "event", item_id)
        return self.send_json(200, {"ok": True})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.end_headers()


def bootstrap_admin() -> None:
    init_db()
    with connect() as con:
        if con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            print("已存在管理员账号；为安全起见，不再创建新管理员。")
            return
    name = input("管理员显示名称: ").strip()
    email = input("管理员邮箱: ").strip().lower()
    password = getpass.getpass("设置密码（至少 12 位）: ")
    confirm = getpass.getpass("再次输入密码: ")
    if not name or len(name) > 60 or not valid_email(email):
        raise SystemExit("姓名或邮箱格式无效。")
    if len(password) < 12 or len(password) > 128 or password != confirm:
        raise SystemExit("密码不匹配或长度不在 12–128 位范围内。")
    salt, digest = hash_password(password)
    user_id = str(uuid.uuid4())
    with connect() as con:
        con.execute("INSERT INTO users(id,display_name,email,password_salt,password_hash,role,created_at) VALUES(?,?,?,?,?,'admin',?)",
                    (user_id, name, email, salt, digest, utc_now()))
    print("管理员账号已创建。请启动服务并使用该账号登录。")


def reset_password(email: str) -> None:
    init_db()
    password = getpass.getpass("新密码（至少 12 位）: ")
    confirm = getpass.getpass("再次输入新密码: ")
    if len(password) < 12 or len(password) > 128 or password != confirm:
        raise SystemExit("密码不匹配或长度不在 12–128 位范围内。")
    salt, digest = hash_password(password)
    with connect() as con:
        cur = con.execute("UPDATE users SET password_salt=?,password_hash=? WHERE email=?", (salt, digest, email.lower()))
        if not cur.rowcount:
            raise SystemExit("未找到该邮箱对应的账号。")
        user = con.execute("SELECT id FROM users WHERE email=?", (email.lower(),)).fetchone()
        con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    print("密码已重置，现有会话已退出。")


def serve() -> None:
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.scheme = "http"
    print(f"DMT Class site listening on http://{host}:{port}")
    print("Use Ctrl+C to stop. For public deployment, place behind HTTPS and set SITE_ORIGIN.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "create-admin":
        bootstrap_admin()
    elif len(sys.argv) > 1 and sys.argv[1] == "create-invite":
        role = sys.argv[2] if len(sys.argv) > 2 else "member"
        uses = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        note = sys.argv[4] if len(sys.argv) > 4 else ""
        invite = create_invite(note, role, uses, 0)
        print(f"邀请码：{invite['code']}")
        print(f"身份：{invite['role_label']} · 可用次数：{invite['max_uses']} · 无到期时间")
        print("请把这个邀请码发给本班同学、班委或导员；管理员后台可随时查看或删除。")
    elif len(sys.argv) > 2 and sys.argv[1] == "reset-password":
        reset_password(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "check-email":
        address = sys.argv[2].strip().lower()
        verdict, detail = email_domain_verdict(address.rsplit("@", 1)[-1].strip().lower())
        ok, note = check_email_address(address)
        print(f"域名记录：{verdict}（{detail}）")
        print(f"结论：{'可以通过注册校验' if ok else '会被拒绝'}{'（' + note + '）' if note else ''}")
    elif len(sys.argv) > 2 and sys.argv[1] == "test-mail":
        sent, detail = send_mail(sys.argv[2], "DMT CLASS 01 测试邮件 / Test message",
                                 "这是一封来自 DMT CLASS 01 班级网站的测试邮件。\n\n"
                                 "If you received this message, the class site mailer works.")
        print(("发送成功：" if sent else "发送失败：") + detail)
    elif len(sys.argv) > 2 and sys.argv[1] == "verify-email":
        print("已标记为已验证。" if mark_email_verified(sys.argv[2]) else "未找到该邮箱对应的账号。")
    else:
        serve()
