"""Verified TeleNexusIo bot wiring.

Imports finalhost as a dependency. Native runner, not a Docker/security sandbox.
"""
from __future__ import annotations
import asyncio, contextlib, json, os, re, shutil, sqlite3, uuid
from urllib.parse import quote, unquote
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import finalhost as base
from finalhost import *  # noqa: F401,F403

SCHEMA_VERSION = max(base.SCHEMA_VERSION, 4)
MIGRATIONS = dict(base.MIGRATIONS)
MIGRATIONS[4] = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_access_pending_user ON access_requests(user_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_access_user_status ON access_requests(user_id,status);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,read);
"""
PREMIUM_EMOJI_IDS: dict[str, str] = {
    "stop": "5260293700088511294",
    "stop_sign": "5228807917611816928",
    "important": "5373059848856421989",
    "info": "5334544901428229844",
    "warning": "5456140674028019486",
    "fire": "5406922479119673692",
    "archive": "5346288231073723227",
    "recycle": "5911100572508885928",
}

try:
    from telegram import MessageEntity
except ImportError:
    MessageEntity = None


def premium_message(text: str, key: str = "premium") -> dict[str, Any]:
    emoji_id = PREMIUM_EMOJI_IDS.get(key)
    if MessageEntity is None or not emoji_id or not str(emoji_id).isdigit() or not base._owner_premium:
        return {"text": text}
    body = "💎 " + text
    return {"text": body, "entities": [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id=str(emoji_id))]}


def migrate() -> None:
    base.SCHEMA_VERSION = SCHEMA_VERSION
    base.MIGRATIONS = MIGRATIONS
    base.migrate()


def required_channel_url() -> str:
    value = setting("required_channel_url", REQUIRED_CHANNEL_URL)
    return value if re.fullmatch(r"https://t\.me/[A-Za-z0-9_+-]+", value) else REQUIRED_CHANNEL_URL


def set_required_channel(value: str) -> None:
    value = value.strip()
    if value.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]+", value):
        set_setting("required_channel", value); set_setting("required_channel_url", "https://t.me/" + value[1:]); return
    if re.fullmatch(r"https://t\.me/[A-Za-z0-9_+-]+", value):
        set_setting("required_channel_url", value); set_setting("required_channel", "@" + value.rsplit("/", 1)[1]); return
    raise ValueError("channel must be @username or https://t.me/username")


def request_access_durable(uid: int) -> bool:
    get_user(uid)
    try:
        with db() as c:
            cur = c.execute("INSERT INTO access_requests(user_id,created_at,status) VALUES(?,?,'pending') ON CONFLICT DO NOTHING", (uid, now()))
            created = cur.rowcount == 1
            if created: c.execute("UPDATE users SET approved=0,updated_at=? WHERE user_id=?", (now(), uid))
    except sqlite3.IntegrityError:
        created = False
    if created: audit(uid, "access_request")
    return created


def admin_users(page: int = 0, page_size: int = 8):
    page = max(0, int(page))
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = c.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?", (page_size, page * page_size)).fetchall()
    return rows, total


def admin_payments(page: int = 0, page_size: int = 8):
    page = max(0, int(page))
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
        rows = c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY created_at LIMIT ? OFFSET ?", (page_size, page * page_size)).fetchall()
    return rows, total


def project_row(pid: str, uid: int | None = None):
    with db() as c:
        if uid is None:
            return c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return c.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, uid)).fetchone()


def assert_project_owner(pid: str, uid: int):
    row = project_row(pid, uid)
    if not row: raise ValueError("project not found")
    return row


def process_limit_reached(uid: int) -> bool:
    count = 0
    for pid in list(base.run_tasks.keys()):
        row = project_row(pid)
        task = base.run_tasks.get(pid)
        if row and row["user_id"] == uid and task and not task.done():
            count += 1
    return count >= limits(get_user(uid))[3]


def start_project(pid: str, uid: int) -> str:
    p = assert_project_owner(pid, uid)
    if pid in base.run_tasks and not base.run_tasks[pid].done(): return "already running"
    if process_limit_reached(uid): raise ValueError("process limit reached")
    scan_project(Path(p["path"]))
    base.run_tasks[pid] = asyncio.create_task(run_project(p))
    audit(uid, "project_start", pid)
    return "started"


async def stop_project(pid: str, uid: int) -> None:
    assert_project_owner(pid, uid)
    task = base.run_tasks.get(pid)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    else:
        await terminate_process(pid)
    with db() as c:
        c.execute("UPDATE projects SET status='stopped',updated_at=? WHERE id=?", (now(), pid))
        c.execute("UPDATE jobs SET status='stopped',finished_at=? WHERE project_id=? AND status='running'", (now(), pid))
    audit(uid, "project_stop", pid)


async def restart_project(pid: str, uid: int) -> None:
    await stop_project(pid, uid)
    start_project(pid, uid)
    audit(uid, "project_restart", pid)


async def delete_project(pid: str, uid: int) -> None:
    await stop_project(pid, uid)
    p = assert_project_owner(pid, uid)
    shutil.rmtree(Path(p["path"]), ignore_errors=True)
    with db() as c:
        c.execute("DELETE FROM env WHERE project_id=?", (pid,))
        c.execute("DELETE FROM jobs WHERE project_id=?", (pid,))
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
    audit(uid, "project_delete", pid)


def list_files(pid: str, uid: int):
    p = assert_project_owner(pid, uid); root = Path(p["path"])
    return sorted(str(x.relative_to(root)) for x in root.rglob("*") if x.is_file() and ".venv" not in x.parts)


def read_project_log(pid: str, uid: int, limit: int = 12000) -> str:
    assert_project_owner(pid, uid); path = LOGS / (pid + ".log")
    if not path.exists(): return "(no log output)"
    return path.read_bytes()[-limit:].decode("utf-8", "replace")


def file_path(pid: str, uid: int, rel: str) -> Path:
    p = assert_project_owner(pid, uid); target = valid_relative(Path(p["path"]), rel)
    if not target.is_file() or target.is_symlink(): raise ValueError("regular file required")
    return target


def new_file(pid: str, uid: int, rel: str, content: str = "") -> None:
    p = assert_project_owner(pid, uid); dst = valid_relative(Path(p["path"]), rel)
    if dst.exists() or dst.is_symlink(): raise ValueError("file exists")
    dst.parent.mkdir(parents=True, exist_ok=True); dst.write_text(content, encoding="utf-8"); audit(uid, "file_create", f"{pid}:{rel}")


def rename_file(pid: str, uid: int, old: str, new: str) -> None:
    src = file_path(pid, uid, old); dst = valid_relative(Path(assert_project_owner(pid, uid)["path"]), new)
    if dst.exists() or dst.is_symlink(): raise ValueError("destination exists")
    dst.parent.mkdir(parents=True, exist_ok=True); src.rename(dst); audit(uid, "file_rename", f"{pid}:{old}->{new}")


def delete_file(pid: str, uid: int, rel: str) -> None:
    file_path(pid, uid, rel).unlink(); audit(uid, "file_delete", f"{pid}:{rel}")


def env_set(pid: str, uid: int, key: str, value: str) -> None:
    assert_project_owner(pid, uid)
    if not ENV_KEY.fullmatch(key) or key in {"BOT_TOKEN", "ADMIN_ID", "DATA_DIR", "PATH", "HOME"}: raise ValueError("reserved or invalid environment key")
    with db() as c: c.execute("INSERT INTO env(project_id,key,value) VALUES(?,?,?) ON CONFLICT(project_id,key) DO UPDATE SET value=excluded.value", (pid, key, value))
    audit(uid, "env_set", f"{pid}:{key}")


def env_delete(pid: str, uid: int, key: str) -> None:
    assert_project_owner(pid, uid)
    with db() as c: c.execute("DELETE FROM env WHERE project_id=? AND key=?", (pid, key))
    audit(uid, "env_delete", f"{pid}:{key}")


def expire_premium() -> int:
    with db() as c: return c.execute("UPDATE users SET plan='free',premium_until=NULL WHERE plan='premium' AND premium_until IS NOT NULL AND premium_until<=?", (now(),)).rowcount


def backup_database() -> Path:
    ensure_dirs(); path = BACKUPS / f"backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.sqlite3"
    with db() as src, sqlite3.connect(path) as dst: src.backup(dst)
    with db() as c: c.execute("INSERT INTO backups VALUES(?,?,?,?)", (path.stem, str(path), now(), path.stat().st_size))
    audit(ADMIN_ID, "backup", path.name)
    return path


def restore_database(path: Path) -> None:
    if path.resolve().parent != BACKUPS.resolve() or path.suffix != ".sqlite3" or not path.is_file(): raise ValueError("backup path rejected")
    with sqlite3.connect(path) as src, db() as dst: src.backup(dst)
    migrate(); audit(ADMIN_ID, "restore", path.name)


async def recovery() -> None:
    if asyncio.iscoroutinefunction(base.recovery):
        await base.recovery()
    else:
        base.recovery()
    expire_premium()


def _back(dest="menu"): return [[("⬅️ Back", dest), ("🏠 Main Menu", "menu")]]


async def _render_projects(q, uid):
    with db() as c: rows = c.execute("SELECT * FROM projects WHERE user_id=? ORDER BY name", (uid,)).fetchall()
    buttons = [[("📂 " + p["name"], "pdetail:" + p["id"])] for p in rows] or [[("📤 Upload", "upload")]]
    buttons += [[("📤 Upload", "upload")], *_back()]
    await q.edit_message_text("📂 Projects\nSelect a project:" if rows else "📂 Projects\nNo projects yet.", reply_markup=kb(buttons))


async def _render_users(q, page):
    rows, total = admin_users(page)
    body = "👥 Users\n" + ("\n".join(f"{r['user_id']} — {'✅' if r['approved'] else '⏳'} {'🚫' if r['blocked'] else ''} {esc(r['username'])}" for r in rows) or "None")
    buttons = [[("✅ Approve", f"ua:{r['user_id']}"), ("🚫 Block", f"ub:{r['user_id']}")] for r in rows]
    nav = []
    if page: nav.append(("⬅️ Previous", f"users:{page-1}"))
    if (page + 1) * 8 < total: nav.append(("➡️ Next", f"users:{page+1}"))
    if nav: buttons.append(nav)
    buttons += _back("admin")
    await q.edit_message_text(body, reply_markup=kb(buttons))


async def _render_payments(q, page):
    rows, total = admin_payments(page)
    body = "💳 Pending payments\n" + ("\n".join(f"{r['id']} — {r['user_id']} — {r['amount']:,}" for r in rows) or "None")
    buttons = [[("✅ Approve", f"payapprove:{r['id']}"), ("🚫 Reject", f"payreject:{r['id']}")] for r in rows]
    nav = []
    if page: nav.append(("⬅️ Previous", f"payments:{page-1}"))
    if (page + 1) * 8 < total: nav.append(("➡️ Next", f"payments:{page+1}"))
    if nav: buttons.append(nav)
    buttons += _back("admin")
    await q.edit_message_text(body, reply_markup=kb(buttons))


async def _admin_user(target: int, approve: bool, admin: int):
    with db() as c:
        c.execute("UPDATE users SET approved=?,blocked=?,updated_at=? WHERE user_id=?", (1 if approve else 0, 0 if approve else 1, now(), target))
        c.execute("UPDATE access_requests SET status=?,reviewed_at=?,reviewed_by=? WHERE user_id=? AND status='pending'", ("approved" if approve else "blocked", now(), admin, target))
        c.execute("INSERT INTO notifications(user_id,title,body,created_at) VALUES(?,?,?,?)", (target, "Access update", "Approved" if approve else "Blocked", now()))
    audit(admin, "access_approve" if approve else "access_block", str(target))


async def _payment(payment_id: str, approve: bool, admin: int):
    with db() as c:
        p = c.execute("SELECT * FROM payments WHERE id=? AND status='pending'", (payment_id,)).fetchone()
        if not p: return False
        if approve:
            u = c.execute("SELECT premium_until FROM users WHERE user_id=?", (p["user_id"],)).fetchone()
            base_date = datetime.fromisoformat(u[0]) if u and u[0] else datetime.now(timezone.utc)
            until = max(base_date, datetime.now(timezone.utc)) + timedelta(days=30 * p["months"])
            c.execute("UPDATE users SET plan='premium',premium_until=? WHERE user_id=?", (until.isoformat(timespec="seconds"), p["user_id"]))
            title, body = "Premium approved", f"Valid until {until.date()}"
        else:
            title, body = "Payment rejected", "Your receipt was rejected by an administrator."
        c.execute("UPDATE payments SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?", ("approved" if approve else "rejected", now(), admin, payment_id))
        c.execute("INSERT INTO notifications(user_id,title,body,created_at) VALUES(?,?,?,?)", (p["user_id"], title, body, now()))
    audit(admin, "payment_approve" if approve else "payment_reject", payment_id)
    return True


async def request_access(update, context):
    uid = update.effective_user.id
    created = request_access_durable(uid)
    msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if created:
        await retry(lambda: context.bot.send_message(
            ADMIN_ID,
            f"📨 Access request from <code>{uid}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([[('✅ Approve', f'ua:{uid}'), ('🚫 Block', f'ub:{uid}')]])
        ))
    if msg:
        await msg.reply_text("✅ Request recorded." if created else "ℹ️ A request is already pending.")


async def callback(update, context):
    q = update.callback_query
    await q.answer()
    d = q.data or ""
    uid = q.from_user.id
    get_user(uid, q.from_user)
    if d == "join":
        return await q.edit_message_text("📢 Join the required channel to continue:", reply_markup=url_kb("📢 Join Channel", required_channel_url()))
    if d == "request": return await request_access(update, context)
    if d == "menu": return await q.edit_message_text("🏠 Main menu", reply_markup=menu_kb(uid))
    if d == "projects": return await _render_projects(q, uid)
    if d.startswith("pdetail:"):
        p = project_row(d.split(":", 1)[1], uid)
        if not p: return await q.edit_message_text("❌ Project not found.", reply_markup=kb(_back("projects")))
        return await q.edit_message_text(
            f"📂 <b>{esc(p['name'])}</b>\nRuntime: {esc(p['runtime'])}\nStatus: {esc(p['status'])}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb([
                [('▶️ Run', f'pstart:{p["id"]}'), ('⏹️ Stop', f'pstop:{p["id"]}')],
                [('🔄 Restart', f'prestart:{p["id"]}'), ('🗑️ Delete', f'pdelete:{p["id"]}')],
                [('📜 Logs', f'logs:{p["id"]}'), ('📁 Files', f'files:{p["id"]}')],
                * _back('projects')
            ])
        )
    if d.startswith("logs:"):
        pid = d.split(":", 1)[1]
        return await q.edit_message_text("📜 <b>Logs</b>\n<pre>" + esc(read_project_log(pid, uid)[-10000:]) + "</pre>", parse_mode=ParseMode.HTML, reply_markup=kb(_back("pdetail:" + pid)))
    if d.startswith("files:"):
        pid = d.split(":", 1)[1]
        names = list_files(pid, uid)
        buttons = [[('📥 ' + name[:24], f'fdownload:{pid}:{quote(name, safe="")}'), ('🗑️', f'fdelete:{pid}:{quote(name, safe="")}')] for name in names]
        buttons += [[('➕ Create file', f'fcreate:{pid}'), ('✏️ Rename file', f'frename:{pid}')], *_back('projects')]
        return await q.edit_message_text("📁 Files\n" + ("\n".join(names) or "None"), reply_markup=kb(buttons))
    if d.startswith("fdownload:"):
        _, pid, rel = d.split(":", 2)
        path = file_path(pid, uid, unquote(rel))
        with path.open('rb') as fh:
            return await context.bot.send_document(uid, document=fh, filename=path.name, caption='📥 ' + path.name)
    if d.startswith("fdelete:"):
        _, pid, rel = d.split(":", 2)
        delete_file(pid, uid, unquote(rel))
        return await q.edit_message_text('✅ File deleted.', reply_markup=kb(_back('files:' + pid)))
    if d.startswith("fcreate:"):
        states[uid] = {'step': 'file_create', 'pid': d.split(':', 1)[1]}
        return await q.edit_message_text('➕ Send path and content as: path | content', reply_markup=kb(_back('files:' + states[uid]['pid'])))
    if d.startswith("frename:"):
        states[uid] = {'step': 'file_rename', 'pid': d.split(':', 1)[1]}
        return await q.edit_message_text('✏️ Send old path and new path as: old | new', reply_markup=kb(_back('files:' + states[uid]['pid'])))
    if d.startswith("env:"):
        pid = d.split(':', 1)[1]
        return await q.edit_message_text('🔐 Environment variables', reply_markup=kb([[('➕ Add/Update', f'envadd:{pid}'), ('🗑️ Delete', f'envdel:{pid}')], *_back('pdetail:' + pid)]))
    if d.startswith("envadd:") or d.startswith("envdel:"):
        states[uid] = {'step': 'env_add' if d.startswith('envadd:') else 'env_delete', 'pid': d.split(':', 1)[1]}
        return await q.edit_message_text('🔐 Send KEY=VALUE' if d.startswith('envadd:') else '🗑️ Send KEY to delete', reply_markup=kb(_back('pdetail:' + states[uid]['pid'])))
    if d.startswith("pstart:"):
        try: start_project(d[7:], uid)
        except Exception as e: return await q.edit_message_text("❌ " + esc(e), parse_mode=ParseMode.HTML, reply_markup=kb(_back("projects")))
        return await q.edit_message_text("▶️ Started.", reply_markup=kb(_back("projects")))
    if d.startswith("pstop:"):
        await stop_project(d[6:], uid)
        return await q.edit_message_text("⏹️ Stopped.", reply_markup=kb(_back("projects")))
    if d.startswith("prestart:"):
        try: await restart_project(d[9:], uid)
        except Exception as e: return await q.edit_message_text("❌ " + esc(e), parse_mode=ParseMode.HTML, reply_markup=kb(_back("projects")))
        return await q.edit_message_text("🔄 Restarted.", reply_markup=kb(_back("projects")))
    if d.startswith("pdelete:"):
        await delete_project(d[8:], uid)
        return await q.edit_message_text("🗑️ Deleted.", reply_markup=kb(_back("projects")))
    if d == "admin" and is_admin(uid): return await q.edit_message_text("🛡️ Admin panel", reply_markup=admin_kb())
    if d == "health" and is_admin(uid):
        return await q.edit_message_text(f"🩺 Health\nProcesses: {len(base.processes)}\nSchema: {SCHEMA_VERSION}", reply_markup=kb(_back('admin')))
    if d == "audit:0" and is_admin(uid):
        with db() as c: rows = c.execute("SELECT created_at,action,detail FROM audit ORDER BY id DESC LIMIT 20").fetchall()
        return await q.edit_message_text('📈 Audit\n' + ('\n'.join(f'{r[0]} — {r[1]} {esc(r[2])}' for r in rows) or 'None'), parse_mode=ParseMode.HTML, reply_markup=kb(_back('admin')))
    if d == "backup" and is_admin(uid):
        return await q.edit_message_text('✅ Backup created: ' + backup_database().name, reply_markup=admin_kb())
    if d.startswith("users:") and is_admin(uid): return await _render_users(q, int(d.split(":")[1]))
    if d.startswith("payments:") and is_admin(uid): return await _render_payments(q, int(d.split(":")[1]))
    if d.startswith("ua:") and is_admin(uid):
        await _admin_user(int(d[3:]), True, uid)
        return await q.edit_message_text("✅ User approved.", reply_markup=admin_kb())
    if d.startswith("ub:") and is_admin(uid):
        await _admin_user(int(d[3:]), False, uid)
        return await q.edit_message_text("🚫 User blocked.", reply_markup=admin_kb())
    if d.startswith("payapprove:") and is_admin(uid):
        await _payment(d[11:], True, uid)
        return await _render_payments(q, 0)
    if d.startswith("payreject:") and is_admin(uid):
        await _payment(d[10:], False, uid)
        return await _render_payments(q, 0)
    if d == "admin_settings" and is_admin(uid):
        return await q.edit_message_text(
            f"Join gate: {setting('join_enabled')}\nChannel: {setting('required_channel')}\nMaintenance: {setting('maintenance')}",
            reply_markup=kb([[('🔁 Toggle Join', 'toggle_join'), ('🛠️ Toggle Maintenance', 'toggle_maint')], [('✏️ Set Channel', 'set_channel')], *_back('admin')])
        )
    if d in {"toggle_join", "toggle_maint"} and is_admin(uid):
        key = 'join_enabled' if d == 'toggle_join' else 'maintenance'
        set_setting(key, '0' if setting(key) == '1' else '1')
        audit(uid, 'setting_change', key)
        return await q.edit_message_text('✅ Setting updated.', reply_markup=admin_kb())
    if d == "set_channel" and is_admin(uid):
        states[uid] = {'step': 'set_channel'}
        return await q.edit_message_text('✏️ Send @channel or https://t.me/channel.', reply_markup=kb(_back('admin_settings')))
    if d == "broadcast" and is_admin(uid):
        states[uid] = {'step': 'broadcast'}
        return await q.edit_message_text('📢 Send text, photo, video, sticker, or document for preview.', reply_markup=kb(_back('admin')))
    if d == "broadcast_confirm" and is_admin(uid): return await send_broadcast(q, context, uid)
    if d == "broadcast_cancel":
        states.pop(uid, None)
        return await q.edit_message_text('❌ Broadcast cancelled.', reply_markup=admin_kb())
    if d.startswith("months:"):
        states[uid] = {'step': 'receipt', 'months': int(d.split(':')[1])}
        return await q.edit_message_text('💳 Send receipt media.', reply_markup=kb(_back('premium')))
    if d == "premium":
        return await q.edit_message_text("💎 Premium plans", reply_markup=kb([[('📅 1 month', 'months:1')], [('📅 2 months', 'months:2')], [('📅 3 months', 'months:3')], *_back()]))
    return await q.edit_message_text("🏠 Use the menu.", reply_markup=menu_kb(uid))


async def send_broadcast(q, context, uid):
    s = states.get(uid, {})
    with db() as c:
        recipients = [r[0] for r in c.execute("SELECT user_id FROM users WHERE approved=1 AND blocked=0").fetchall()]
    sent = failed = 0
    for target in recipients:
        try:
            await retry(lambda target=target: context.bot.copy_message(target, uid, s['message_id']))
            sent += 1
        except Exception:
            failed += 1
    with db() as c:
        c.execute("INSERT INTO broadcasts VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, uid, s.get('kind', 'unknown'), json.dumps({'message_id': s.get('message_id')}), 'sent', now()))
    audit(uid, 'broadcast', f'sent={sent},failed={failed}')
    states.pop(uid, None)
    await q.edit_message_text(f'✅ Broadcast complete. Sent: {sent}; failed: {failed}.', reply_markup=admin_kb())


async def text_router(update, context):
    uid = update.effective_user.id
    text = (update.effective_message.text or '').strip()
    s = states.get(uid, {})
    if text == '/cancel':
        states.pop(uid, None)
        return await update.effective_message.reply_text('❌ Cancelled.')
    if s.get('step') == 'set_channel' and is_admin(uid):
        states.pop(uid, None)
        try: set_required_channel(text)
        except Exception as e: return await update.effective_message.reply_text('❌ ' + esc(e), parse_mode=ParseMode.HTML)
        return await update.effective_message.reply_text('✅ Channel updated.')
    if s.get('step') == 'broadcast' and is_admin(uid):
        states[uid].update(step='broadcast_confirm', message_id=update.effective_message.id, kind='text')
        return await update.effective_message.reply_text(f'📢 Preview:\n\n{esc(text)}', parse_mode=ParseMode.HTML, reply_markup=kb([[('✅ Confirm', 'broadcast_confirm'), ('❌ Cancel', 'broadcast_cancel')]]))
    if s.get('step') == 'file_create':
        try:
            rel, content = (x.strip() for x in text.split('|', 1))
            new_file(s['pid'], uid, rel, content)
            states.pop(uid, None)
            return await update.effective_message.reply_text('✅ File created.')
        except Exception as e:
            return await update.effective_message.reply_text('❌ ' + esc(e), parse_mode=ParseMode.HTML)
    if s.get('step') == 'file_rename':
        try:
            old, new = (x.strip() for x in text.split('|', 1))
            rename_file(s['pid'], uid, old, new)
            states.pop(uid, None)
            return await update.effective_message.reply_text('✅ File renamed.')
        except Exception as e:
            return await update.effective_message.reply_text('❌ ' + esc(e), parse_mode=ParseMode.HTML)
    if s.get('step') == 'env_add':
        try:
            key, value = text.split('=', 1)
            env_set(s['pid'], uid, key.strip(), value)
            states.pop(uid, None)
            return await update.effective_message.reply_text('✅ Environment variable saved.')
        except Exception as e:
            return await update.effective_message.reply_text('❌ ' + esc(e), parse_mode=ParseMode.HTML)
    if s.get('step') == 'env_delete':
        try:
            env_delete(s['pid'], uid, text)
            states.pop(uid, None)
            return await update.effective_message.reply_text('✅ Environment variable deleted.')
        except Exception as e:
            return await update.effective_message.reply_text('❌ ' + esc(e), parse_mode=ParseMode.HTML)
    return await base.text_router(update, context)


async def media_router(update, context):
    uid = update.effective_user.id
    msg = update.effective_message
    s = states.get(uid, {})
    if s.get('step') == 'receipt':
        obj = msg.photo[-1] if msg.photo else (msg.video or msg.sticker or msg.document)
        if not obj: return
        pid = 'PM-' + uuid.uuid4().hex[:10].upper()
        typ = type(obj).__name__
        fid = obj.file_id
        with db() as c:
            c.execute("INSERT INTO payments(id,user_id,months,amount,status,receipt_type,receipt_file_id,created_at) VALUES(?,?,?,?,?,?,?,?)", (pid, uid, s['months'], s['months'] * PAYMENT_PRICE, 'pending', typ, fid, now()))
        states.pop(uid, None)
        audit(uid, 'payment_submit', pid)
        return await msg.reply_text(f'✅ Receipt submitted: {pid}')
    if s.get('step') == 'broadcast' and is_admin(uid):
        kind = 'photo' if msg.photo else 'video' if msg.video else 'sticker' if msg.sticker else 'document' if msg.document else None
        if kind:
            states[uid].update(step='broadcast_confirm', message_id=msg.id, kind=kind)
            return await msg.reply_text(f'📢 {kind.title()} preview ready.', reply_markup=kb([[('✅ Confirm', 'broadcast_confirm'), ('❌ Cancel', 'broadcast_cancel')]]))


async def document_router(update, context):
    uid = update.effective_user.id
    s = states.get(uid, {})
    if s.get('step') in {'upload', 'name'}:
        return await base.document_router(update, context)
    return await media_router(update, context)


def build_application():
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN is empty; set the BOT_TOKEN environment variable before starting')
    migrate()
    ensure_dirs()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('cancel', text_router))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.Document.ALL, document_router))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Sticker.ALL, media_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    return app


async def main():
    migrate()
    await recovery()
    app = build_application()
    await app.initialize()
    await base.check_owner_premium(app)
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        for task in list(base.run_tasks.values()):
            task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
    asyncio.run(main())