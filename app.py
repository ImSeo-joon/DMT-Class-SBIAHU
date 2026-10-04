#!/usr/bin/env python3
"""Production WSGI entry point for the DMT Class site."""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from flask import Flask, jsonify, make_response, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

import server as core

ROOT = Path(__file__).resolve().parent
COOKIE = core.COOKIE
SESSION_DAYS = core.SESSION_DAYS
RATE_LIMIT: dict[str, list[float]] = {}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = core.MAX_RESOURCE_BYTES + 1024 * 1024
# Waitress is bound to localhost; only the local Nginx reverse proxy can supply these headers.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def api_error(status: int, message: str):
    return jsonify({"error": message}), status


def body() -> dict:
    if request.content_length is None or request.content_length <= 0 or request.content_length > 1_000_000:
        raise ValueError("请求内容为空或过大")
    value = request.get_json(silent=False)
    if not isinstance(value, dict):
        raise ValueError("请求格式必须为 JSON 对象")
    return value


def origin_allowed() -> bool:
    origin = request.headers.get("Origin", "").rstrip("/")
    configured = os.environ.get("SITE_ORIGIN", "").rstrip("/")
    expected = configured or f"{request.scheme}://{request.host}".rstrip("/")
    return bool(origin and hmac.compare_digest(origin, expected))


def require_origin():
    if not origin_allowed():
        return api_error(403, "请求来源校验失败")
    return None


def get_session():
    token = request.cookies.get(COOKIE)
    if not token:
        return None, None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with core.connect() as con:
        row = con.execute(
            "SELECT s.*,u.id,u.display_name,u.email,u.role,u.email_verified,u.account_type,u.created_at AS account_created_at "
            "FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?", (token_hash, core.utc_now())
        ).fetchone()
    return (row, row) if row else (None, None)


def require_user(roles: set[str] | None = None):
    session, user = get_session()
    if not user:
        return None, None, api_error(401, "请先登录")
    if roles is not None and user["role"] not in roles:
        return None, None, api_error(403, "当前账号没有此操作权限")
    return session, user, None


def check_write(session) -> bool:
    if not origin_allowed():
        return False
    supplied = request.headers.get("X-CSRF-Token", "")
    return bool(supplied and hmac.compare_digest(supplied, session["csrf_token"]))


def csrf_error():
    return api_error(403, "安全校验失败或已过期，请刷新后重试")


RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "900") or 900)


def limited(action: str, maximum: int = 8) -> bool:
    now = time.time()
    key = f"{action}:{request.remote_addr or 'unknown'}"
    hits = [stamp for stamp in RATE_LIMIT.get(key, []) if now - stamp < RATE_WINDOW]
    if len(hits) >= maximum:
        RATE_LIMIT[key] = hits
        return True
    hits.append(now)
    RATE_LIMIT[key] = hits
    return False


def audit(actor: str, action: str, entity: str, entity_id: str) -> None:
    with core.connect() as con:
        con.execute("INSERT INTO audit_log(actor_id,action,entity,entity_id,created_at) VALUES(?,?,?,?,?)",
                    (actor, action, entity, entity_id, core.utc_now()))


def read_items(table: str) -> list[dict]:
    with core.connect() as con:
        rows = con.execute(f"SELECT id,payload FROM {table}").fetchall()
    items = [json.loads(row["payload"]) | {"id": row["id"]} for row in rows]
    if table == "news":
        items.sort(key=lambda item: (bool(item.get("featured")), item.get("date", ""), item.get("id", "")), reverse=True)
    else:
        items.sort(key=lambda item: (item.get("date", ""), item.get("id", "")))
    return items


def new_session(user_id: str, extra: dict | None = None):
    token = secrets.token_urlsafe(40)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with core.connect() as con:
        con.execute("DELETE FROM sessions WHERE expires_at<=?", (core.utc_now(),))
        con.execute("INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (token_hash, user_id, csrf, expires, now.isoformat(timespec="seconds")))
        user = con.execute("SELECT id,display_name,email,role,email_verified,account_type,created_at FROM users WHERE id=?",
                           (user_id,)).fetchone()
    payload = {"user": dict(user) | {"role_label": core.ROLE_LABELS[user["role"]]}, "csrf_token": csrf}
    payload.update(extra or {})
    response = jsonify(payload)
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=request.is_secure, samesite="Strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.errorhandler(400)
def bad_request(_error):
    return api_error(400, "请求格式不正确")


@app.errorhandler(413)
def too_large(_error):
    return api_error(413, "单个文件不能超过 25 MB")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/config")
def site_config():
    return jsonify({"registration_mode": core.registration_mode(),
                    "mailer_configured": core.mailer_configured()})


@app.get("/api/me")
def me():
    _, user = get_session()
    if not user:
        return jsonify({"user": None})
    return jsonify({"user": {"id": user["id"], "display_name": user["display_name"],
        "email": user["email"], "role": user["role"], "role_label": core.ROLE_LABELS[user["role"]],
        "email_verified": user["email_verified"], "account_type": core.account_type_of(user),
        "created_at": user["account_created_at"]},
        "csrf_token": user["csrf_token"]})


@app.get("/api/news")
def news():
    return jsonify(read_items("news"))


@app.get("/api/events")
def events():
    return jsonify(read_items("events"))


@app.get("/api/resources")
def resources():
    _, viewer = get_session()
    if not core.is_member(viewer):
        return api_error(403, core.VISITOR_BLOCKED)
    with core.connect() as con:
        rows = con.execute("SELECT id,title,description,category,original_name,mime_type,size,uploaded_by,created_at,folder_id FROM resources ORDER BY created_at DESC").fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/folders")
def folders():
    _, viewer = get_session()
    if not core.is_member(viewer):
        return api_error(403, core.VISITOR_BLOCKED)
    return jsonify(core.list_folders())


@app.get("/api/nearby-food")
def nearby_food():
    if limited("nearby-food", 40):
        return api_error(429, "查询太频繁，请稍后再试")
    keyword = (request.args.get("keyword") or "餐厅").strip()[:20] or "餐厅"
    try:
        radius = int(request.args.get("radius") or 1500)
    except (TypeError, ValueError):
        radius = 1500
    radius = max(300, min(core.FOOD_RADIUS_MAX, radius))
    try:
        items = core.nearby_food(keyword, radius)
    except ValueError as exc:
        return api_error(503, str(exc))
    return jsonify({"keyword": keyword, "radius": radius,
                    "campus": core.CAMPUS_LNG_LAT, "items": items})


@app.get("/api/resources/<resource_id>/download")
def download_resource(resource_id):
    _, download_user = get_session()
    if not core.is_member(download_user):
        return api_error(403, core.VISITOR_BLOCKED)
    with core.connect() as con:
        item = con.execute("SELECT original_name,stored_name FROM resources WHERE id=?", (resource_id,)).fetchone()
    if not item:
        return api_error(404, "资料不存在")
    target = (core.UPLOAD_DIR / item["stored_name"]).resolve()
    if core.UPLOAD_DIR.resolve() not in target.parents or not target.is_file():
        return api_error(404, "资料文件不存在")
    return send_from_directory(core.UPLOAD_DIR, item["stored_name"], as_attachment=True,
                               download_name=item["original_name"], mimetype="application/octet-stream",
                               conditional=True, etag=True, max_age=0)


@app.get("/api/schedule")
def schedule():
    _, viewer = get_session()
    if not core.is_member(viewer):
        return api_error(403, core.VISITOR_BLOCKED)
    with core.connect() as con:
        row = con.execute("SELECT value FROM site_settings WHERE key='schedule'").fetchone()
    return jsonify(json.loads(row["value"]) if row else {"term_start": "2026-09-07", "lessons": []})


@app.get("/api/users")
def users():
    _, user, error = require_user({"admin"})
    if error:
        return error
    with core.connect() as con:
        rows = con.execute("SELECT id,display_name,email,role,email_verified,account_type,created_at FROM users ORDER BY created_at").fetchall()
    return jsonify([dict(row) | {"role_label": core.ROLE_LABELS[row["role"]]} for row in rows])


@app.get("/api/team")
def team_list():
    # 未登录访客可以只读浏览组队大厅，发帖人显示为“班级成员”
    _, viewer = get_session()
    return jsonify({"posts": core.team_post_list(mask_authors=not viewer),
                    "tracks": list(core.TEAM_TRACKS),
                    "viewer": core.account_type_of(viewer) or "guest"})


@app.get("/api/team/<post_id>")
def team_detail(post_id):
    _, viewer = get_session()
    detail = core.team_post_detail(post_id, mask_authors=not viewer)
    if not detail:
        return api_error(404, "帖子不存在或已被删除")
    return jsonify(detail)


@app.get("/api/notifications")
def notifications():
    _, viewer = get_session()
    return jsonify(core.notification_payload(viewer))


@app.get("/api/feedback")
def feedback_admin_list():
    _, user, error = require_user()
    if error:
        return error
    if core.is_visitor(user):
        return api_error(403, core.VISITOR_BLOCKED)
    if user["role"] != "admin":
        return jsonify({"items": core.feedback_list(user_id=user["id"]), "scope": "mine",
                        "mailer_configured": core.mailer_configured()})
    return jsonify({"items": core.feedback_list(), "scope": "all",
                    "mailer_configured": core.mailer_configured(),
                    "recipients": core.feedback_admin_emails()})


@app.get("/api/messages")
def messages_list():
    _, user, error = require_user()
    if error:
        return error
    if user["role"] in core.CONTENT_ROLES:
        return jsonify({"items": core.message_list(), "scope": "all",
                        "statuses": list(core.FEEDBACK_STATUSES),
                        "mailer_configured": core.mailer_configured(),
                        "recipients": core.staff_emails()})
    return jsonify({"items": core.message_list(user_id=user["id"]), "scope": "mine",
                    "statuses": list(core.FEEDBACK_STATUSES)})


@app.get("/api/audit")
def audit_log():
    _, user, error = require_user({"admin"})
    if error:
        return error
    with core.connect() as con:
        rows = con.execute("SELECT a.action,a.entity,a.entity_id,a.created_at,u.display_name AS actor "
                           "FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 100").fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/invites")
def invites():
    _, user, error = require_user({"admin"})
    if error:
        return error
    return jsonify({"registration_mode": core.registration_mode(), "invites": core.invite_list(),
                    "mailer_configured": core.mailer_configured(),
                    "roles": [{"value": r, "label": core.INVITE_ROLE_LABELS[r]} for r in core.INVITE_ROLES]})


@app.post("/api/contact")
def contact_submit():
    """咨询与合作：未登录访客也能提交，写入数据库并转发到班委信箱。"""
    error = require_origin()
    if error:
        return error
    if limited("contact", 20):
        return api_error(429, "提交太频繁，请稍后再试")
    _, author = get_session()
    try:
        result = core.create_message(body(), author)
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    audit(author["id"] if author else None, "create", "message", result["id"])
    return jsonify(result), 201


@app.post("/api/feedback")
def feedback_submit():
    session, user, error = require_user()
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    if limited("feedback", 20):
        return api_error(429, "提交太频繁，请稍后再试")
    try:
        result = core.post_feedback(user, body())
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    audit(user["id"], "feedback:" + result["category"], "feedback", result["id"])
    return jsonify(result), 201


@app.post("/api/team")
def team_create():
    session, user, error = require_user()
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    if limited("team-post", 30):
        return api_error(429, "发帖太频繁，请稍后再试")
    try:
        post = core.create_team_post(user["id"], body())
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    audit(user["id"], "create", "team-post", post["id"])
    return jsonify(post), 201


@app.post("/api/team/<post_id>/replies")
def team_reply(post_id):
    session, user, error = require_user()
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    if limited("team-reply", 60):
        return api_error(429, "回复太频繁，请稍后再试")
    try:
        reply = core.add_team_reply(post_id, user["id"], body().get("body"))
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    return jsonify(reply), 201


@app.post("/api/register")
def register():
    error = require_origin()
    if error:
        return error
    # 校园网、宿舍 Wi-Fi 常常共用一个出口 IP，认证类阈值放宽；单账号暴力破解由登录锁定兜底
    if limited("register", 40):
        return api_error(429, "操作太频繁，请稍后再试（同一网络下的同学会共用额度，可换用手机流量或等几分钟再试）")
    mode = core.registration_mode()
    if mode == "closed":
        return api_error(403, "注册已关闭，请联系管理员邀请。")
    try:
        raw = body()
        display_name = core.clean_text(raw.get("display_name"), "姓名或昵称", required=True, maximum=60)
        email = core.clean_text(raw.get("email"), "邮箱", required=True, maximum=254).lower()
        password = raw.get("password", "")
        if not core.valid_email(email):
            return api_error(400, "请输入有效邮箱")
        if not isinstance(password, str) or len(password) < 12 or len(password) > 128:
            return api_error(400, "密码长度须为 12–128 个字符")
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    account_type = str(raw.get("account_type") or "member").strip().lower()
    if account_type not in core.ACCOUNT_TYPES:
        account_type = "member"
    invite = None
    if mode == "invite" and account_type != "visitor":
        code = core.normalize_invite_code(raw.get("invite_code"))
        if not code:
            return api_error(403, "请填写邀请码；邀请码可向管理员索取。")
        with core.connect() as con:
            invite = con.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
        if not invite or not core.invite_available(invite):
            return api_error(403, "邀请码无效、已用尽或已过期，请向管理员确认。")
    role = invite["role"] if invite else "member"
    if account_type == "visitor":
        role = "member"
    salt, digest = core.hash_password(password)
    user_id = str(uuid.uuid4())
    try:
        with core.connect() as con:
            con.execute("INSERT INTO users(id,display_name,email,password_salt,password_hash,role,created_at,account_type)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (user_id, display_name, email, salt, digest, role, core.utc_now(), account_type))
            if invite:
                con.execute("UPDATE invite_codes SET used_count=used_count+1 WHERE id=?", (invite["id"],))
    except sqlite3.IntegrityError:
        return api_error(409, "此邮箱已注册，请直接登录")
    audit(user_id, "register", "user", user_id)
    if invite:
        audit(user_id, "register-invite:" + (invite["label"] or "未命名邀请码"), "invite", invite["id"])
    return new_session(user_id)


@app.post("/api/login")
def login():
    error = require_origin()
    if error:
        return error
    if limited("auth", 40):
        return api_error(429, "操作太频繁，请稍后再试（同一网络下的同学会共用额度，可换用手机流量或等几分钟再试）")
    try:
        raw = body()
        email = core.clean_text(raw.get("email"), "邮箱", required=True, maximum=254).lower()
        password = raw.get("password", "")
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    with core.connect() as con:
        user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    lock_note = core.account_locked(user)
    if lock_note:
        return api_error(429, lock_note)
    valid = False
    if user and isinstance(password, str):
        _, attempt = core.hash_password(password, user["password_salt"])
        valid = hmac.compare_digest(attempt, user["password_hash"])
    if not valid:
        if user:
            core.register_failed_login(user["id"])
        return api_error(401, "邮箱或密码不正确")
    with core.connect() as con:
        con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        con.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?", (user["id"],))
    audit(user["id"], "login", "user", user["id"])
    return new_session(user["id"])


@app.post("/api/invites")
def create_invite_route():
    session, user, error = require_user({"admin"})
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        raw = body()
        invite = core.create_invite(raw.get("label", ""), str(raw.get("role", "member")),
                                    int(raw.get("max_uses", 1)), int(raw.get("expires_in_days", 0)), user["id"])
    except (ValueError, TypeError) as exc:
        return api_error(400, str(exc) or "邀请码参数不正确")
    audit(user["id"], "create-invite:" + invite["role"], "invite", invite["id"])
    return jsonify(invite), 201


@app.post("/api/test-mail")
def verify_test():
    session, user, error = require_user({"admin"})
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    if limited("mail-test", 5):
        return api_error(429, "测试邮件发送过于频繁，请稍后再试")
    try:
        target = core.clean_text(body().get("email"), "邮箱", required=True, maximum=254).lower()
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    if not core.valid_email(target):
        return api_error(400, "请输入有效邮箱")
    sent, detail = core.send_mail(target, "DMT CLASS 01 测试邮件 / Test message",
                                  "这是一封来自 DMT CLASS 01 班级网站的测试邮件。\n\n"
                                  "If you received this message, the class site mailer works.")
    audit(user["id"], "test-mail", "email", target)
    return jsonify({"sent": sent, "detail": detail})


@app.post("/api/ai/polish")
def ai_polish():
    session, user, error = require_user()
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    if limited("ai-polish", 60):
        return api_error(429, "修改请求太频繁，请过一会儿再试")
    try:
        raw = body()
        api_key = core.clean_text(raw.get("apiKey"), "API Key", required=True, maximum=200)
        article = core.clean_text(raw.get("text"), "文章正文", required=True, maximum=core.AI_MAX_PROMPT)
        mode = str(raw.get("mode") or "both").strip().lower()
        if mode not in core.POLISH_MODES:
            mode = "both"
        model = str(raw.get("model") or core.AI_MODELS[0]).strip()
        if model not in core.AI_MODELS:
            model = core.AI_MODELS[0]
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    ok, result = core.ai_chat(api_key, model, core.polish_article_prompt(article, mode),
                              max_tokens=8000, temperature=0.2)
    if not ok:
        return api_error(502, result)
    audit(user["id"], "ai-polish:" + mode, "ai", model)
    return jsonify({"text": result, "model": model, "mode": mode})


@app.post("/api/ai/resume")
def ai_resume():
    session, user, error = require_user()
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    if limited("ai-resume", 60):
        return api_error(429, "生成请求太频繁，请过一会儿再试")
    try:
        raw = body()
        api_key = core.clean_text(raw.get("apiKey"), "API Key", required=True, maximum=200)
        prompt = core.clean_text(raw.get("prompt"), "提示词", required=True, maximum=core.AI_MAX_PROMPT)
        model = str(raw.get("model") or core.AI_MODELS[0]).strip()
        if model not in core.AI_MODELS:
            model = core.AI_MODELS[0]
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    ok, result = core.ai_chat(api_key, model, prompt)
    if not ok:
        return api_error(502, result)
    audit(user["id"], "ai-generate:" + model, "ai", model)
    return jsonify({"text": result, "model": model})


@app.post("/api/resources")
def upload_resource():
    session, user, error = require_user(core.CONTENT_ROLES)
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    if limited("upload", 40):
        return api_error(429, "上传操作太频繁，请稍后重试")
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return api_error(400, "请选择要上传的文件")
    original_name = Path(uploaded.filename.replace("\\", "/")).name.strip()
    suffix = Path(original_name).suffix.lower()
    if not original_name or suffix not in core.RESOURCE_EXTENSIONS:
        return api_error(400, "该文件类型暂不支持")
    data = uploaded.read(core.MAX_RESOURCE_BYTES + 1)
    if not data:
        return api_error(400, "文件为空")
    if len(data) > core.MAX_RESOURCE_BYTES:
        return api_error(413, "单个文件不能超过 25 MB")
    try:
        title = core.clean_text(request.form.get("title") or Path(original_name).stem,
                                "资料名称", required=True, maximum=120)
        description = core.clean_text(request.form.get("description", ""), "资料说明", maximum=1000)
        category = core.clean_text(request.form.get("category", "班级共享"), "资料分类", maximum=60) or "班级共享"
        folder_id = core.clean_folder_id(request.form.get("folder_id"))
        if not core.folder_exists(folder_id):
            raise ValueError("选定的文件夹不存在")
    except ValueError as exc:
        return api_error(400, str(exc))
    resource_id = str(uuid.uuid4())
    stored_name = resource_id + suffix
    core.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (core.UPLOAD_DIR / stored_name).write_bytes(data)
    mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    created_at = core.utc_now()
    try:
        with core.connect() as con:
            con.execute("INSERT INTO resources(id,title,description,category,original_name,stored_name,mime_type,size,uploaded_by,created_at,folder_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (resource_id, title, description, category, original_name, stored_name,
                         mime_type, len(data), user["id"], created_at, folder_id))
    except Exception:
        (core.UPLOAD_DIR / stored_name).unlink(missing_ok=True)
        raise
    audit(user["id"], "upload", "resource", resource_id)
    return jsonify({"id": resource_id, "title": title, "description": description, "category": category,
                    "original_name": original_name, "mime_type": mime_type, "size": len(data),
                    "uploaded_by": user["id"], "created_at": created_at, "folder_id": folder_id}), 201


@app.post("/api/logout")
def logout():
    session, user, error = require_user()
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    token = request.cookies.get(COOKIE, "")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with core.connect() as con:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
    response = make_response(jsonify({"ok": True}))
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=request.is_secure, samesite="Strict")
    return response


def editor_post(path: str, user):
    try:
        raw = body()
        if path == "/api/folders":
            name = core.clean_text(raw.get("name"), "文件夹名称", required=True, maximum=60)
            folder_id = str(uuid.uuid4())
            created_at = core.utc_now()
            with core.connect() as con:
                con.execute("INSERT INTO resource_folders(id,name,created_by,created_at) VALUES(?,?,?,?)",
                            (folder_id, name, user["id"], created_at))
            audit(user["id"], "create", "folder", folder_id)
            return jsonify({"id": folder_id, "name": name, "created_at": created_at, "count": 0}), 201
        if path == "/api/news":
            item = core.validate_news(raw)
            item_id = str(uuid.uuid4())
            item["id"] = item_id
            with core.connect() as con:
                if item["featured"]:
                    for row in con.execute("SELECT id,payload FROM news").fetchall():
                        old = json.loads(row["payload"])
                        if old.get("featured"):
                            old["featured"] = False
                            con.execute("UPDATE news SET payload=?,updated_at=?,updated_by=? WHERE id=?",
                                        (json.dumps(old, ensure_ascii=False), core.utc_now(), user["id"], row["id"]))
                con.execute("INSERT INTO news(id,payload,updated_at,updated_by) VALUES(?,?,?,?)",
                            (item_id, json.dumps(item, ensure_ascii=False), core.utc_now(), user["id"]))
            audit(user["id"], "create", "news", item_id)
            return jsonify(item), 201
        if path == "/api/events":
            item = core.validate_event(raw)
            item_id = str(uuid.uuid4())
            item["id"] = item_id
            with core.connect() as con:
                con.execute("INSERT INTO events(id,payload,updated_at,updated_by) VALUES(?,?,?,?)",
                            (item_id, json.dumps(item, ensure_ascii=False), core.utc_now(), user["id"]))
            audit(user["id"], "create", "event", item_id)
            return jsonify(item), 201
        return api_error(404, "找不到此接口")
    except (ValueError, json.JSONDecodeError) as exc:
        return api_error(400, str(exc))


@app.post("/api/<path:api_path>")
def editor_post_route(api_path):
    path = "/api/" + api_path
    if path in ("/api/register", "/api/login", "/api/logout"):
        return api_error(404, "找不到此接口")
    session, user, error = require_user(core.CONTENT_ROLES)
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    return editor_post(path, user)


@app.put("/api/<path:api_path>")
def update(api_path):
    path = unquote("/api/" + api_path)
    session, user, error = require_user(core.CONTENT_ROLES)
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        raw = body()
        if path == "/api/schedule":
            value = core.validate_schedule(raw)
            with core.connect() as con:
                con.execute("UPDATE site_settings SET value=?,updated_at=?,updated_by=? WHERE key='schedule'",
                            (json.dumps(value, ensure_ascii=False), core.utc_now(), user["id"]))
            audit(user["id"], "update", "schedule", "term")
            return jsonify(value)
        match = re.fullmatch(r"/api/(news|events)/([0-9a-zA-Z_-]+)", path)
        if not match:
            return api_error(404, "找不到此接口")
        table, item_id = match.groups()
        payload = core.validate_news(raw) if table == "news" else core.validate_event(raw)
        payload["id"] = item_id
        with core.connect() as con:
            exists = con.execute(f"SELECT 1 FROM {table} WHERE id=?", (item_id,)).fetchone()
            if not exists:
                return api_error(404, "内容不存在")
            if table == "news" and payload["featured"]:
                for row in con.execute("SELECT id,payload FROM news WHERE id<>?", (item_id,)).fetchall():
                    old = json.loads(row["payload"])
                    if old.get("featured"):
                        old["featured"] = False
                        con.execute("UPDATE news SET payload=?,updated_at=?,updated_by=? WHERE id=?",
                                    (json.dumps(old, ensure_ascii=False), core.utc_now(), user["id"], row["id"]))
            con.execute(f"UPDATE {table} SET payload=?,updated_at=?,updated_by=? WHERE id=?",
                        (json.dumps(payload, ensure_ascii=False), core.utc_now(), user["id"], item_id))
        audit(user["id"], "update", "news" if table == "news" else "event", item_id)
        return jsonify(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        return api_error(400, str(exc))


@app.patch("/api/me")
def update_profile():
    session, user, error = require_user()
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        name = core.clean_text(body().get("display_name"), "姓名或昵称", required=True, maximum=60)
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    with core.connect() as con:
        con.execute("UPDATE users SET display_name=? WHERE id=?", (name, user["id"]))
    audit(user["id"], "update", "profile", user["id"])
    return jsonify({"display_name": name})


@app.patch("/api/messages/<message_id>")
def message_handle(message_id):
    session, user, error = require_user(core.CONTENT_ROLES)
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        raw = body()
        handled = core.handle_message(message_id, str(raw.get("status", "")).strip(),
                                      raw.get("response", ""), user["id"])
    except Exception as exc:
        return api_error(400, str(exc) or "参数不正确")
    if not handled:
        return api_error(404, "留言不存在")
    audit(user["id"], "handle-message:" + str(raw.get("status", "")).strip(), "message", message_id)
    return jsonify({"ok": True})


@app.patch("/api/feedback/<feedback_id>")
def feedback_handle(feedback_id):
    session, user, error = require_user({"admin"})
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        raw = body()
        ok = core.handle_feedback(feedback_id, str(raw.get("status", "")).strip(), raw.get("response", ""), user["id"])
    except Exception as exc:
        return api_error(400, str(exc) or "参数不正确")
    if not ok:
        return api_error(404, "反馈不存在")
    audit(user["id"], "handle-feedback:" + str(raw.get("status", "")).strip(), "feedback", feedback_id)
    return jsonify({"ok": True})


@app.patch("/api/site-notice")
def update_site_notice():
    session, user, error = require_user({"admin"})
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        value = core.set_site_notice(body().get("notice", ""))
    except Exception as exc:
        return api_error(400, str(exc) or "公告内容不正确")
    audit(user["id"], "set-notice", "setting", "site_notice")
    return jsonify({"ok": True, "notice": value})


@app.patch("/api/team/<post_id>")
def team_status(post_id):
    session, user, error = require_user()
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    with core.connect() as con:
        post = con.execute("SELECT author_id FROM team_posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        return api_error(404, "帖子不存在或已被删除")
    if post["author_id"] != user["id"] and user["role"] not in core.CONTENT_ROLES:
        return api_error(403, "只有发帖人或管理员可以修改状态")
    try:
        status = str(body().get("status", "")).strip().lower()
        core.set_team_post_status(post_id, status)
    except Exception as exc:
        return api_error(400, str(exc) or "参数不正确")
    audit(user["id"], "set-status:" + status, "team-post", post_id)
    return jsonify({"ok": True, "status": status})


@app.patch("/api/registration")
def update_registration_mode():
    session, user, error = require_user({"admin"})
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        mode = core.set_registration_mode(str(body().get("mode", "")).strip().lower())
    except (ValueError, TypeError) as exc:
        return api_error(400, str(exc) or "注册方式不正确")
    audit(user["id"], "set-registration:" + mode, "setting", "registration_mode")
    return jsonify({"registration_mode": mode})


@app.patch("/api/users/<target_id>")
def update_role(target_id):
    session, user, error = require_user({"admin"})
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        role = body().get("role")
        if role not in core.ROLES:
            raise ValueError("未知的账号角色")
    except Exception as exc:
        return api_error(400, str(exc) or "请求格式不正确")
    with core.connect() as con:
        target = con.execute("SELECT id,role FROM users WHERE id=?", (target_id,)).fetchone()
        if not target:
            return api_error(404, "账号不存在")
        admins = con.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        if target["role"] == "admin" and role != "admin" and admins <= 1:
            return api_error(400, "至少保留一名管理员")
        con.execute("UPDATE users SET role=? WHERE id=?", (role, target_id))
    audit(user["id"], "set-role:" + role, "user", target_id)
    return jsonify({"id": target_id, "role": role, "role_label": core.ROLE_LABELS[role]})


@app.patch("/api/folders/<folder_id>")
def rename_folder(folder_id):
    session, user, error = require_user(core.CONTENT_ROLES)
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        name = core.clean_text(body().get("name"), "文件夹名称", required=True, maximum=60)
    except Exception as exc:
        return api_error(400, str(exc))
    with core.connect() as con:
        cur = con.execute("UPDATE resource_folders SET name=? WHERE id=?", (name, folder_id))
    if not cur.rowcount:
        return api_error(404, "文件夹不存在")
    audit(user["id"], "rename", "folder", folder_id)
    return jsonify({"id": folder_id, "name": name})


@app.patch("/api/resources/<resource_id>")
def move_resource(resource_id):
    session, user, error = require_user(core.CONTENT_ROLES)
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        folder_id = core.clean_folder_id(body().get("folder_id"))
        if not core.folder_exists(folder_id):
            raise ValueError("选定的文件夹不存在")
    except Exception as exc:
        return api_error(400, str(exc))
    with core.connect() as con:
        cur = con.execute("UPDATE resources SET folder_id=? WHERE id=?", (folder_id, resource_id))
    if not cur.rowcount:
        return api_error(404, "资料不存在")
    audit(user["id"], "move", "resource", resource_id)
    return jsonify({"id": resource_id, "folder_id": folder_id})


@app.delete("/api/users/<user_id>")
def delete_user_route(user_id):
    session, admin, error = require_user({"admin"})
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    try:
        result = core.delete_user(admin["id"], user_id)
    except ValueError as exc:
        return api_error(400, str(exc))
    audit(admin["id"], "delete-account", "user", user_id)
    return jsonify(result)


@app.delete("/api/<table>/<item_id>")
def delete_item(table, item_id):
    if not re.fullmatch(r"[0-9a-zA-Z_-]+", item_id):
        return api_error(404, "找不到此接口")
    if table == "team":
        session, user, error = require_user()
        if error:
            return error
        if not check_write(session):
            return csrf_error()
        with core.connect() as con:
            post = con.execute("SELECT author_id FROM team_posts WHERE id=?", (item_id,)).fetchone()
        if not post:
            return api_error(404, "帖子不存在或已被删除")
        if post["author_id"] != user["id"] and user["role"] not in core.CONTENT_ROLES:
            return api_error(403, "只有发帖人或管理员可以删除")
        core.delete_team_post(item_id)
        audit(user["id"], "delete", "team-post", item_id)
        return jsonify({"ok": True})
    if table == "invites":
        session, user, error = require_user({"admin"})
        if error:
            return error
        if not check_write(session):
            return csrf_error()
        with core.connect() as con:
            cur = con.execute("DELETE FROM invite_codes WHERE id=?", (item_id,))
        if not cur.rowcount:
            return api_error(404, "邀请码不存在")
        audit(user["id"], "delete", "invite", item_id)
        return jsonify({"ok": True})
    if table == "folders":
        session, user, error = require_user(core.CONTENT_ROLES)
        if error:
            return error
        if not check_write(session):
            return csrf_error()
        with core.connect() as con:
            exists = con.execute("SELECT 1 FROM resource_folders WHERE id=?", (item_id,)).fetchone()
            if not exists:
                return api_error(404, "文件夹不存在")
            con.execute("UPDATE resources SET folder_id=NULL WHERE folder_id=?", (item_id,))
            con.execute("DELETE FROM resource_folders WHERE id=?", (item_id,))
        audit(user["id"], "delete", "folder", item_id)
        return jsonify({"ok": True})
    if table not in ("news", "events", "resources"):
        return api_error(404, "找不到此接口")
    session, user, error = require_user(core.CONTENT_ROLES)
    if error:
        return error
    if not check_write(session):
        return csrf_error()
    stored_name = None
    with core.connect() as con:
        if table == "resources":
            item = con.execute("SELECT stored_name FROM resources WHERE id=?", (item_id,)).fetchone()
            stored_name = item["stored_name"] if item else None
        cur = con.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
    if not cur.rowcount:
        return api_error(404, "内容不存在")
    if stored_name:
        (core.UPLOAD_DIR / stored_name).unlink(missing_ok=True)
    entity = "news" if table == "news" else "event" if table == "events" else "resource"
    audit(user["id"], "delete", entity, item_id)
    return jsonify({"ok": True})


@app.route("/api/<path:api_path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def unknown_api(api_path):
    return api_error(404, "找不到此接口")


@app.get("/")
@app.get("/<path:asset_path>")
def static_file(asset_path=""):
    rel = "index.html" if not asset_path else unquote(asset_path)
    target = (ROOT / rel).resolve()
    is_public_asset = rel.startswith("assets/")
    if (rel != "index.html" and not is_public_asset) or ROOT not in target.parents or "var" in target.parts or not target.is_file():
        return api_error(404, "找不到页面")
    response = make_response(send_from_directory(ROOT, rel))
    response.headers["Cache-Control"] = "no-cache" if rel == "index.html" else "public, max-age=3600"
    return response


core.init_db()
