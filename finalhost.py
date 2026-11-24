"""TeleNexusIo VPS bot v2: a native, no-Docker Telegram runner.

This is a convenience runner, not a security sandbox. User programs execute as the
bot OS account and can access any permissions granted to that account.
"""
from __future__ import annotations
import asyncio, contextlib, html, json, logging, os, re, shutil, signal, sqlite3, sys, time, uuid, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, MessageEntity
    from telegram.constants import ChatMemberStatus, ParseMode
    from telegram.error import Forbidden, RetryAfter, TelegramError
    from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
except ImportError:
    InlineKeyboardButton = InlineKeyboardMarkup = Update = MessageEntity = None
    Application = ApplicationBuilder = CallbackQueryHandler = CommandHandler = ContextTypes = MessageHandler = filters = None

# ============================================================
# BOT CONFIGURATION — edit these two lines only
# ============================================================
BOT_TOKEN = "9191832438:AAGKKdk77FIc2ZjPDMpMJnLcpJVYp6IrGN0"
ADMIN_ID = 6858000955
# ============================================================

REQUIRED_CHANNEL = "@TeleNexusIo"
REQUIRED_CHANNEL_URL = "https://t.me/TeleNexusIo"
DATA = Path(os.getenv("DATA_DIR", "/app/data")).expanduser().resolve()
DB = DATA / "runner.sqlite3"
PROJECTS, LOGS, BACKUPS, TMP = (DATA / x for x in ("projects", "logs", "backups", "tmp"))
SCHEMA_VERSION = 3
FREE_PROJECTS, PREMIUM_PROJECTS = 1, 5
FREE_QUOTA, PREMIUM_QUOTA = 500 * 1024**2, 5 * 1024**3
FREE_UPLOAD, PREMIUM_UPLOAD = 100 * 1024**2, 500 * 1024**2
FREE_PROCESSES, PREMIUM_PROCESSES = 1, 3
MAX_FILES, MAX_ARCHIVE_BYTES, MAX_MEMBER_BYTES = 10_000, 600 * 1024**2, 200 * 1024**2
PROCESS_TIMEOUT, DEP_TIMEOUT, MAX_RESTARTS = 24 * 3600, 600, 5
PAYMENT_PRICE = 5000
PREMIUM_EMOJI_IDS: dict[str, str] = {
    "🤖": "5197501488674416758", "👤": "5967411695453213733",
    "📂": "5875431869842985304", "📤": "5231200819986047254",
    "📊": "6042137469204303531", "💾": "5456187398977247949",
    "⚙️": "5193046387817753858", "💎": "6298557526560479072",
    "🔔": "5296369303661067030", "❓": "5465443379917629504",
    "🛡️": "5407025283456835913", "📨": "5253742260054409879",
    "🟢": "5226929552319594190", "🔴": "5765140653029724793",
    "▶️": "5373012449597335010", "⏹️": "5416081784641168838",
    "🔄": "5411225014148014586", "📜": "5780922471704173144",
    "📁": "5877341274863832725", "♻️": "5445267414562389170",
    "🗑️": "4990318000096675490", "⬅️": "6300738038506984251",
    "🏠": "5210952531676504517", "💳": "6199645073978692518",
    "📢": "6010608582435803344", "✅": "5934029049260150525",
    "🚫": "5397916757333654639", "🛠️": "6030656587830399914",
    "👥": "5217822164362739968", "🩺": "5231012545799666522",
    "🧹": "5271604874419647061", "📋": "5145427681680032825",
    "📄": "5348125953090403204", "📦": "5359543311897998264",
    "🧩": "5371057809980943077", "🧾": "5134537521518085000",
    "🗃️": "5870791093255147680", "🔒": "5244837092042750681",
    "🔐": "5246762912428603768", "⚠️": "5373236586760651455",
    "⏳": "5454415424319931791", "❌": "5440621591387980068",
    "🔍": "5447644880824181073", "🧪": "5240241223632954241",
    "📥": "5463107823946717464", "✏️": "5188705588925702510",
    "📝": "5370952626231879743", "🧠": "5229095839334410849",
    "🔗": "5388632425314140043", "📈": "5375410054960795477",
}
USE_CUSTOM_EMOJI_BUTTONS = os.getenv("USE_CUSTOM_EMOJI_BUTTONS", "1").lower() not in {"0", "false", "no"}
USE_CUSTOM_EMOJI_MESSAGES = os.getenv("USE_CUSTOM_EMOJI_MESSAGES", "1").lower() not in {"0", "false", "no"}
SECRET_RE = re.compile(r"(?i)(token|password|secret|api[_-]?key|authorization|private[_-]?key)\s*[=:]\s*[^\s,;]+")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
log = logging.getLogger("telenexus.runner")
processes: dict[str, asyncio.subprocess.Process] = {}
run_tasks: dict[str, asyncio.Task] = {}
states: dict[int, dict[str, Any]] = {}
broadcast_tasks: dict[int, asyncio.Task] = {}

_owner_premium = False
_owner_premium_checked = False


def now() -> str: return datetime.now(timezone.utc).isoformat(timespec="seconds")
def esc(v: Any) -> str: return html.escape(str(v), quote=False)
def mask(v: Any) -> str: return SECRET_RE.sub(lambda m: m.group(1) + "=<masked>", str(v))
def safe_name(v: str) -> str: return re.sub(r"[^A-Za-z0-9_.-]", "_", v.strip())[:48].strip("._-") or "project"
def ensure_dirs() -> None:
    for p in (DATA, PROJECTS, LOGS, BACKUPS, TMP): p.mkdir(parents=True, exist_ok=True)
def db() -> sqlite3.Connection:
    ensure_dirs(); c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c

MIGRATIONS = {
1: """CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,name TEXT NOT NULL DEFAULT '',username TEXT NOT NULL DEFAULT '',approved INTEGER NOT NULL DEFAULT 0,blocked INTEGER NOT NULL DEFAULT 0,plan TEXT NOT NULL DEFAULT 'free',premium_until TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,reviewed_at TEXT,reviewed_by INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,name TEXT NOT NULL,path TEXT NOT NULL,runtime TEXT NOT NULL,entrypoint TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'stopped',auto_restart INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,name));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,user_id INTEGER NOT NULL,status TEXT NOT NULL,pid INTEGER,exit_code INTEGER,log_path TEXT,started_at TEXT,finished_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS env(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,UNIQUE(project_id,key));
CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,months INTEGER NOT NULL,amount INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',receipt_type TEXT,receipt_file_id TEXT,reason TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,reviewed_at TEXT,reviewed_by INTEGER);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,read INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,action TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS broadcasts(id TEXT PRIMARY KEY,admin_id INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,finished_at TEXT);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id,status); CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);""",
2: """ALTER TABLE users ADD COLUMN last_seen TEXT;""",
3: """CREATE TABLE IF NOT EXISTS backups(id TEXT PRIMARY KEY,path TEXT NOT NULL,created_at TEXT NOT NULL,size INTEGER NOT NULL DEFAULT 0);""",
}
def migrate() -> None:
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone() else None
        current = int(row[0]) if row else 0
        for version in range(current + 1, SCHEMA_VERSION + 1):
            try: c.executescript(MIGRATIONS[version])
            except sqlite3.OperationalError as e:
                if version != 2 or "duplicate column" not in str(e): raise
            c.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('join_enabled','1')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('required_channel',?)", (REQUIRED_CHANNEL,))
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('maintenance','0')")
def setting(k: str, default: str = "") -> str:
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone(); return r[0] if r else default
def set_setting(k: str, v: Any) -> None:
    with db() as c: c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
def audit(uid: int | None, action: str, detail: str = "") -> None:
    with db() as c: c.execute("INSERT INTO audit(user_id,action,detail,created_at) VALUES(?,?,?,?)", (uid, action, mask(detail)[:1000], now()))
def get_user(uid: int, tg_user: Any = None) -> sqlite3.Row:
    with db() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not r:
            name = getattr(tg_user, "full_name", "") if tg_user else ""
            username = (getattr(tg_user, "username", "") or "") if tg_user else ""
            c.execute("INSERT INTO users(user_id,name,username,created_at,updated_at,last_seen) VALUES(?,?,?,?,?,?)", (uid,name,username,now(),now(),now()))
        elif tg_user:
            c.execute("UPDATE users SET name=?,username=?,updated_at=?,last_seen=? WHERE user_id=?", (tg_user.full_name,tg_user.username or "",now(),now(),uid))
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
def is_admin(uid: int) -> bool: return uid == ADMIN_ID
def is_premium(u: sqlite3.Row) -> bool:
    if is_admin(u["user_id"]): return True
    try: return u["plan"] == "premium" and u["premium_until"] and datetime.fromisoformat(u["premium_until"]) > datetime.now(timezone.utc)
    except (TypeError, ValueError): return False
def limits(u: sqlite3.Row) -> tuple[int,int,int,int]: return (PREMIUM_PROJECTS,PREMIUM_QUOTA,PREMIUM_UPLOAD,PREMIUM_PROCESSES) if is_premium(u) else (FREE_PROJECTS,FREE_QUOTA,FREE_UPLOAD,FREE_PROCESSES)
def directory_size(root: Path) -> int:
    total = 0
    try:
        for p in root.rglob("*"):
            if p.is_file() and ".venv" not in p.parts: total += p.stat().st_size
    except OSError: pass
    return total
def project_count(uid: int) -> int:
    with db() as c: return int(c.execute("SELECT COUNT(*) FROM projects WHERE user_id=?", (uid,)).fetchone()[0])
def used_quota(uid: int) -> int:
    with db() as c: paths = [Path(r[0]) for r in c.execute("SELECT path FROM projects WHERE user_id=?", (uid,))]
    return sum(directory_size(p) for p in paths)
def valid_relative(root: Path, rel: str) -> Path:
    if not rel or "\\" in rel or Path(rel).is_absolute(): raise ValueError("path must be root-relative")
    p = (root / rel).resolve(); rr = root.resolve()
    if p != rr and rr not in p.parents: raise ValueError("path escapes project root")
    return p

def scan_project(root: Path) -> dict[str, Any]:
    files = [p for p in root.rglob("*") if p.is_file() and ".venv" not in p.parts and "__pycache__" not in p.parts]
    cfg = root / "runner.json"; runtime = entry = ""
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8")); runtime, entry = str(data.get("runtime", "")).lower(), str(data.get("entrypoint", ""))
            if runtime not in {"python", "node", "shell"}: raise ValueError("runner.json runtime must be python, node, or shell")
            valid_relative(root, entry)
            if not (root / entry).is_file(): raise ValueError("runner.json entrypoint does not exist")
        except json.JSONDecodeError as e: raise ValueError("invalid runner.json") from e
    candidates = {"python": ("main.py","bot.py","app.py","run.py","start.py"), "node": ("index.js","main.js","app.js","bot.js","server.js"), "shell": ("run.sh","start.sh")}
    for rt, names in candidates.items():
        if not entry:
            q = next((p for p in files if p.name in names), None)
            if q: runtime, entry = rt, str(q.relative_to(root)); break
    if not entry:
        for p in files:
            if p.suffix == ".py": runtime, entry = "python", str(p.relative_to(root)); break
            if p.suffix in {".js", ".mjs", ".cjs"}: runtime, entry = "node", str(p.relative_to(root)); break
            if p.suffix == ".sh": runtime, entry = "shell", str(p.relative_to(root)); break
    if not entry: raise ValueError("no supported entrypoint found")
    return {"runtime": runtime, "entry": entry, "files": len(files), "docker": any(p.name.lower() in {"dockerfile","compose.yml","docker-compose.yml","docker-compose.yaml"} for p in files)}
def safe_extract_zip(src: Path, dst: Path, max_files: int = MAX_FILES, max_bytes: int = MAX_ARCHIVE_BYTES) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        infos = z.infolist()
        if len(infos) > max_files or sum(max(0, i.file_size) for i in infos) > max_bytes: raise ValueError("archive quota exceeded")
        root = dst.resolve()
        for i in infos:
            name = i.filename
            if not name or name.startswith("/") or "\\" in name or Path(name).drive: raise ValueError("unsafe ZIP path")
            mode = (i.external_attr >> 16) & 0o170000
            if mode in {0o120000, 0o010000, 0o060000, 0o020000}: raise ValueError("symlinks and special files are not allowed")
            if i.file_size > MAX_MEMBER_BYTES: raise ValueError("ZIP member too large")
            target = (dst / name).resolve()
            if target != root and root not in target.parents: raise ValueError("ZIP traversal detected")
        for i in infos:
            if i.is_dir(): (dst / i.filename).mkdir(parents=True, exist_ok=True)
            else:
                target = valid_relative(dst, i.filename); target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as r, target.open("wb") as w: shutil.copyfileobj(r, w, length=1024 * 1024)

def build_env(project_id: str) -> dict[str, str]:
    env = {"PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(DATA / "home" / project_id), "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"}
    with db() as c:
        for r in c.execute("SELECT key,value FROM env WHERE project_id=?", (project_id,)):
            if r[0] not in {"BOT_TOKEN", "ADMIN_ID", "DATA_DIR"} and ENV_KEY.fullmatch(r[0]): env[r[0]] = r[1]
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True); return env

def rotate_log(path: Path, max_bytes: int = 5 * 1024 * 1024, keep: int = 3) -> None:
    if path.exists() and path.stat().st_size > max_bytes:
        for i in range(keep - 1, 0, -1):
            old, new = path.with_suffix(path.suffix + f".{i}"), path.with_suffix(path.suffix + f".{i+1}")
            if old.exists(): old.replace(new)
        path.replace(path.with_suffix(path.suffix + ".1"))
def runtime_command(p: sqlite3.Row) -> list[str]:
    if p["runtime"] == "python": return [sys.executable, "-u", p["entrypoint"]]
    if p["runtime"] == "node":
        if not shutil.which("node"): raise RuntimeError("Node.js runtime unavailable")
        return ["node", p["entrypoint"]]
    if p["runtime"] == "shell": return ["bash", p["entrypoint"]]
    raise RuntimeError("unsupported runtime")
async def terminate_process(pid: str) -> None:
    proc = processes.get(pid)
    if proc and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError): os.killpg(proc.pid, signal.SIGTERM)
        try: await asyncio.wait_for(proc.wait(), 8)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError): os.killpg(proc.pid, signal.SIGKILL)
async def run_project(p: sqlite3.Row) -> None:
    pid, root, log_path = p["id"], Path(p["path"]), LOGS / f"{p['id']}.log"; restarts = 0
    try:
        while True:
            rotate_log(log_path); cmd = runtime_command(p); log_path.parent.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=root, env=build_env(pid), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True); processes[pid] = proc
            job_id = uuid.uuid4().hex
            with db() as c:
                c.execute("UPDATE projects SET status='running',updated_at=? WHERE id=?", (now(),pid)); c.execute("INSERT INTO jobs(id,project_id,user_id,status,pid,log_path,started_at,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id,pid,p["user_id"],"running",proc.pid,str(log_path),now(),now()))
            async def drain() -> None:
                assert proc.stdout
                with log_path.open("a", encoding="utf-8", errors="replace") as f:
                    async for line in proc.stdout: f.write(mask(line.decode(errors="replace"))[:10000]); f.flush(); rotate_log(log_path)
            try: await asyncio.wait_for(asyncio.gather(drain(), proc.wait()), PROCESS_TIMEOUT); status, code = "finished", proc.returncode
            except asyncio.TimeoutError: await terminate_process(pid); status, code = "timeout", None
            with db() as c:
                c.execute("UPDATE jobs SET status=?,exit_code=?,finished_at=? WHERE id=?", (status,code,now(),job_id)); c.execute("UPDATE projects SET status='stopped',updated_at=? WHERE id=?", (now(),pid))
            processes.pop(pid, None)
            if status == "finished" and code != 0 and p["auto_restart"] and restarts < MAX_RESTARTS and pid in run_tasks:
                restarts += 1; await asyncio.sleep(min(60, 2 ** restarts)); continue
            break
    except asyncio.CancelledError: await terminate_process(pid); raise
    except Exception as e:
        log.exception("runner failed")
        with db() as c: c.execute("UPDATE projects SET status='stopped',updated_at=? WHERE id=?", (now(),pid)); c.execute("UPDATE jobs SET status='failed',finished_at=? WHERE project_id=? AND status='running'", (now(),pid))
        with log_path.open("a", encoding="utf-8") as f: f.write("\nERROR " + mask(e) + "\n")
    finally: processes.pop(pid, None); run_tasks.pop(pid, None)

# ============================================================
# CUSTOM EMOJI HELPERS
# ============================================================
def button_icon_id(label: str) -> str | None:
    if not USE_CUSTOM_EMOJI_BUTTONS or not _owner_premium: return None
    for glyph in sorted(PREMIUM_EMOJI_IDS, key=len, reverse=True):
        if label.startswith(glyph):
            value = PREMIUM_EMOJI_IDS[glyph]
            return value if value.isdigit() else None
    return None

def make_button(label: str, data: str) -> Any:
    icon = button_icon_id(label)
    if icon and InlineKeyboardButton:
        try: return InlineKeyboardButton(label, callback_data=data, icon_custom_emoji_id=icon)
        except TypeError: pass
    return InlineKeyboardButton(label, callback_data=data)

def kb(rows: list[list[tuple[str,str]]]) -> Any:
    return InlineKeyboardMarkup([[make_button(t, d) for t,d in row] for row in rows])
def url_kb(label: str, url: str) -> Any:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)]])
def back(dest="menu"): return [[("⬅️ Back", dest), ("🏠 Main Menu", "menu")]]
def menu_kb(uid: int) -> Any:
    rows = [[("📂 Projects","projects"),("📤 Upload","upload")],[("📊 Status","status"),("💾 Storage","storage")],[("💎 Premium","premium"),("🔔 Notifications","notifs")],[("❓ Help","help")]]
    if is_admin(uid): rows.append([("🛡️ Admin","admin")])
    return kb(rows)
def admin_kb() -> Any: return kb([[('👥 Users','users:0'),('💳 Payments','payments:0')],[('📢 Broadcast','broadcast'),('📈 Audit','audit:0')],[('💾 Backup','backup'),('🩺 Health','health')],[('⚙️ Join/Maintenance','admin_settings')],*back()])

def premium_text(text: str, glyph: str = "💎") -> dict[str, Any]:
    if not USE_CUSTOM_EMOJI_MESSAGES or not _owner_premium or MessageEntity is None:
        return {"text": text}
    emoji_id = PREMIUM_EMOJI_IDS.get(glyph)
    if not emoji_id or not str(emoji_id).isdigit():
        return {"text": text}
    body = glyph + " " + text
    return {"text": body, "entities": [MessageEntity(type="custom_emoji", offset=0, length=len(glyph), custom_emoji_id=str(emoji_id))]}

def premium_reply(text: str, glyph: str = "💎", **kwargs) -> dict[str, Any]:
    base = premium_text(text, glyph)
    base.update(kwargs)
    return base


async def retry(call, tries=4):
    for i in range(tries):
        try: return await call()
        except RetryAfter as e: await asyncio.sleep(min(e.retry_after,60))
        except (TelegramError, asyncio.TimeoutError):
            if i == tries - 1: raise
            await asyncio.sleep(2 ** i)
async def joined(context: ContextTypes.DEFAULT_TYPE, uid: int) -> bool:
    if is_admin(uid) or setting("join_enabled","1") != "1": return True
    try:
        m = await context.bot.get_chat_member(setting("required_channel", REQUIRED_CHANNEL), uid)
        return m.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
    except Exception: return False
async def authorized(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str = "use") -> tuple[bool, sqlite3.Row]:
    uid = update.effective_user.id; u = get_user(uid, update.effective_user)
    msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if is_admin(uid): return True, u
    if u["blocked"]:
        if msg: await msg.reply_text("🚫 Account blocked.")
        return False,u
    if setting("maintenance") == "1":
        if msg: await msg.reply_text("🛠️ Maintenance mode is active.")
        return False,u
    if not await joined(context, uid):
        if msg: await msg.reply_text("🔒 Join the required channel first.", reply_markup=kb([[('📢 Join Channel','join')],[('✅ I Joined','verify')]]))
        return False,u
    if not u["approved"]:
        if msg: await msg.reply_text("🛡️ Access approval is required.", reply_markup=kb([[('📨 Request Access','request')]]))
        return False,u
    return True,u

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok,u = await authorized(update,context)
    if not ok: return
    plan = 'Premium' if is_premium(u) else 'Free'
    text = f"<b>TeleNexusIo VPS</b>\n\nPlan: <b>{plan}</b>\nProjects: {project_count(u['user_id'])}/{limits(u)[0]}\nStorage: {used_quota(u['user_id'])/1024/1024:.1f}/{limits(u)[1]/1024/1024:.0f} MB"
    await update.effective_message.reply_text(**premium_reply(text, "🤖", parse_mode=ParseMode.HTML, reply_markup=menu_kb(u["user_id"])))

async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid=update.effective_user.id
    with db() as c:
        c.execute("INSERT INTO access_requests(user_id,created_at) VALUES(?,?)",(uid,now())); c.execute("UPDATE users SET approved=0 WHERE user_id=?",(uid,))
    audit(uid,"access_request")
    await retry(lambda: context.bot.send_message(ADMIN_ID,f"📨 Access request from <code>{uid}</code>",parse_mode=ParseMode.HTML,reply_markup=kb([[('✅ Approve',f'ua:{uid}'),('🚫 Block',f'ub:{uid}')]])))
    msg = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if msg: await msg.reply_text("✅ Request sent to admin.")

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q=update.callback_query; await q.answer(); d=q.data or ""; uid=q.from_user.id; u=get_user(uid,q.from_user)
    if d=='join':
        url = setting('required_channel_url', REQUIRED_CHANNEL_URL)
        return await q.edit_message_text("📢 Join the required channel to continue:", reply_markup=url_kb("📢 Join Channel", url))
    if d=='verify':
        return await q.edit_message_text('✅ Channel verified.' if await joined(context,uid) else '❌ Join not found.')
    if d=='request': return await request_access(update,context)
    if not is_admin(uid) and d not in {'menu'}:
        ok,_=await authorized(update,context)
        if not ok: return
    if d=='menu': return await q.edit_message_text('🏠 Main menu',reply_markup=menu_kb(uid))
    if d=='admin' and is_admin(uid): return await q.edit_message_text('🛡️ Admin panel',reply_markup=admin_kb())
    if d=='admin_settings' and is_admin(uid): return await q.edit_message_text(f"Join gate: {setting('join_enabled')}\nChannel: {setting('required_channel')}\nMaintenance: {setting('maintenance')}",reply_markup=kb([[('🔁 Toggle Join','toggle_join'),('🛠️ Toggle Maintenance','toggle_maint')],[('✏️ Set Channel','set_channel')],*back('admin')]))
    if d=='set_channel' and is_admin(uid): states[uid]={'step':'set_channel'}; return await q.edit_message_text('Send the required channel username, e.g. @TeleNexusIo.',reply_markup=kb(back('admin_settings')))
    if d in {'toggle_join','toggle_maint'} and is_admin(uid):
        key='join_enabled' if d=='toggle_join' else 'maintenance'; set_setting(key,'0' if setting(key)=='1' else '1'); audit(uid,'setting_change',key); return await q.edit_message_text(f'{key}: {setting(key)}',reply_markup=admin_kb())
    if d=='status' or d=='health':
        du=shutil.disk_usage(DATA); return await q.edit_message_text(f'🩺 Native runner online\nProcesses: {len(processes)}\nFree disk: {du.free//1024//1024} MB\nSchema: {SCHEMA_VERSION}',reply_markup=kb(back('admin' if d=='health' else 'menu')))
    if d=='storage': return await q.edit_message_text(f'💾 Used: {used_quota(uid)/1024/1024:.1f} MB\nFree: {shutil.disk_usage(DATA).free//1024//1024} MB',reply_markup=kb(back()))
    if d=='help': return await q.edit_message_text('Upload a ZIP or source file. runner.json may declare python/node/shell and a root-relative entrypoint. Native execution is not OS isolation.',reply_markup=kb(back()))
    if d=='premium': return await q.edit_message_text('💎 Premium plans',reply_markup=kb([[('📅 1 month','months:1')],[('📅 2 months','months:2')],[('📅 3 months','months:3')],*back()]))
    if d=='broadcast' and is_admin(uid): states[uid]={'step':'broadcast'}; return await q.edit_message_text('📢 Send text for a preview, then confirm.',reply_markup=kb(back('admin')))
    if d=='broadcast_confirm' and is_admin(uid):
        text=states.get(uid,{}).get('text',''); sent=failed=0
        with db() as c: recipients=c.execute('SELECT user_id FROM users WHERE approved=1 AND blocked=0').fetchall()
        for r in recipients:
            try: await retry(lambda r=r: context.bot.send_message(r['user_id'],text)); sent += 1
            except (Forbidden, TelegramError): failed += 1
        audit(uid,'broadcast',f'sent={sent},failed={failed}'); states.pop(uid,None); return await q.edit_message_text(f'✅ Broadcast complete. Sent: {sent}; failed: {failed}.',reply_markup=admin_kb())
    if d.startswith('pa:') and is_admin(uid):
        payment_id=d[3:]
        with db() as c:
            payment=c.execute("SELECT * FROM payments WHERE id=? AND status='pending'",(payment_id,)).fetchone()
            if payment:
                u2=c.execute('SELECT premium_until FROM users WHERE user_id=?',(payment['user_id'],)).fetchone(); base_dt=datetime.fromisoformat(u2[0]) if u2 and u2[0] else datetime.now(timezone.utc); until=max(base_dt,datetime.now(timezone.utc))+timedelta(days=30*payment['months'])
                c.execute("UPDATE payments SET status='approved',reviewed_at=?,reviewed_by=? WHERE id=?",(now(),uid,payment_id)); c.execute("UPDATE users SET plan='premium',premium_until=? WHERE user_id=?",(until.isoformat(timespec='seconds'),payment['user_id'])); c.execute("INSERT INTO notifications(user_id,title,body,created_at) VALUES(?,?,?,?)",(payment['user_id'],'Premium approved',f'Valid until {until.date()}',now()))
        audit(uid,'payment_approve',payment_id); return await q.edit_message_text('✅ Payment approved.',reply_markup=admin_kb())
    if d.startswith('months:'):
        m=int(d.split(':')[1]); states[uid]={'step':'receipt','months':m}; return await q.edit_message_text(f'💳 {m} month(s): {m*PAYMENT_PRICE:,} MMK\nSend a receipt photo/document.',reply_markup=kb(back('premium')))
    if d=='projects':
        with db() as c: rows=c.execute('SELECT * FROM projects WHERE user_id=? ORDER BY name',(uid,)).fetchall()
        return await q.edit_message_text('📂 Projects\n'+('\n'.join(f"{'🟢' if r['status']=='running' else '🔴'} {esc(r['name'])}" for r in rows) or 'No projects yet.'),parse_mode=ParseMode.HTML,reply_markup=kb([[('📤 Upload','upload')],*back()]))
    if d=='upload': states[uid]={'step':'upload'}; return await q.edit_message_text('📤 Send a ZIP or supported source document. /cancel to stop.',reply_markup=kb(back()))
    if d=='notifs':
        with db() as c: rows=c.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20',(uid,)).fetchall(); c.execute('UPDATE notifications SET read=1 WHERE user_id=?',(uid,))
        return await q.edit_message_text('🔔 Notifications\n'+('\n'.join(f"{r['created_at'][:16]} — {esc(r['title'])}: {esc(r['body'])}" for r in rows) or 'None'),parse_mode=ParseMode.HTML,reply_markup=kb(back()))
    if d=='backup' and is_admin(uid):
        path=BACKUPS/f"backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.sqlite3"
        with db() as src, sqlite3.connect(path) as dst: src.backup(dst)
        with db() as c: c.execute('INSERT INTO backups VALUES(?,?,?,?)',(path.stem,str(path),now(),path.stat().st_size))
        audit(uid,'backup',path.name); return await q.edit_message_text(f'✅ Backup created: {path.name}',reply_markup=admin_kb())
    if d.startswith('ua:') and is_admin(uid): return await admin_user_action(q,int(d[3:]),True)
    if d.startswith('ub:') and is_admin(uid): return await admin_user_action(q,int(d[3:]),False)
    await q.edit_message_text('This control is available from the current menu.',reply_markup=menu_kb(uid))

async def admin_user_action(q: Any, target: int, approve: bool) -> None:
    with db() as c:
        c.execute('UPDATE users SET approved=?,blocked=? WHERE user_id=?',(1 if approve else 0,0 if approve else 1,target)); c.execute("UPDATE access_requests SET status=?,reviewed_at=?,reviewed_by=? WHERE user_id=? AND status='pending'",('approved' if approve else 'blocked',now(),ADMIN_ID,target)); c.execute('INSERT INTO notifications(user_id,title,body,created_at) VALUES(?,?,?,?)',(target,'Access update','Approved' if approve else 'Blocked',now()))
    audit(ADMIN_ID,'access_approve' if approve else 'access_block',str(target)); await q.edit_message_text(('✅ Approved ' if approve else '🚫 Blocked ')+str(target),reply_markup=admin_kb())

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid=update.effective_user.id; text=(update.effective_message.text or '').strip(); s=states.get(uid,{})
    if text=='/cancel': states.pop(uid,None); return await update.effective_message.reply_text('Cancelled.')
    if s.get('step')=='set_channel' and is_admin(uid): set_setting('required_channel',text); audit(uid,'channel_change',text); states.pop(uid,None); return await update.effective_message.reply_text('✅ Channel updated.')
    if s.get('step')=='name': return await create_project(update,s,text)
    if s.get('step')=='broadcast' and is_admin(uid): states[uid].update({'text':text,'step':'broadcast_confirm'}); return await update.effective_message.reply_text(f'📢 Preview:\n\n{esc(text)}',parse_mode=ParseMode.HTML,reply_markup=kb([[('✅ Confirm','broadcast_confirm'),('❌ Cancel','cancel')]]))
    await update.effective_message.reply_text('Use /start.')

async def create_project(update: Update, s: dict[str,Any], name: str) -> None:
    uid=update.effective_user.id; u=get_user(uid,update.effective_user); root=None; name=safe_name(name)
    try:
        if project_count(uid)>=limits(u)[0]: raise ValueError('project limit reached')
        with db() as c:
            if c.execute('SELECT 1 FROM projects WHERE user_id=? AND name=?',(uid,name)).fetchone(): raise ValueError('duplicate project name')
        root=PROJECTS/f'{uid}-{uuid.uuid4().hex[:12]}'; root.mkdir(); src=Path(s['src'])
        if src.suffix.lower()=='.zip': safe_extract_zip(src,root)
        else: shutil.copy2(src,root/src.name)
        if directory_size(root)>limits(u)[1]: raise ValueError('storage quota exceeded')
        info=scan_project(root); pid=root.name.split('-',1)[1]
        with db() as c: c.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)',(pid,uid,name,str(root),info['runtime'],info['entry'],'stopped',0,now(),now()))
        audit(uid,'project_create',pid); states.pop(uid,None); shutil.rmtree(s['tmp'],ignore_errors=True); await update.effective_message.reply_text(f'✅ Created {name} ({info["runtime"]} {info["entry"]})')
    except Exception as e:
        if root: shutil.rmtree(root,ignore_errors=True)
        await update.effective_message.reply_text('❌ '+esc(e),parse_mode=ParseMode.HTML)

async def document_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid=update.effective_user.id; s=states.get(uid,{})
    if s.get('step') not in {'upload','name'}: return
    d=update.effective_message.document; u=get_user(uid,update.effective_user); limit=limits(u)[2]
    if d.file_size and d.file_size>limit: return await update.effective_message.reply_text('❌ Upload quota exceeded.')
    if not (d.file_name or '').lower().endswith(('.zip','.py','.js','.mjs','.cjs','.sh')): return await update.effective_message.reply_text('❌ Unsupported file.')
    tmp=TMP/f'{uuid.uuid4().hex}'; tmp.mkdir(); path=tmp/safe_name(d.file_name or 'upload.zip'); await (await d.get_file()).download_to_drive(str(path)); states[uid]={'step':'name','tmp':str(tmp),'src':str(path)}; await update.effective_message.reply_text('✅ File received. Send a project name.')

async def media_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid=update.effective_user.id; s=states.get(uid,{})
    if s.get('step')=='receipt':
        msg=update.effective_message; obj=msg.photo[-1] if msg.photo else (msg.video or msg.sticker or msg.document)
        fid=obj.file_id if obj else ''; m=s['months']; pid='PM-'+uuid.uuid4().hex[:10].upper()
        with db() as c: c.execute('INSERT INTO payments(id,user_id,months,amount,status,receipt_type,receipt_file_id,created_at) VALUES(?,?,?,?,?,?,?,?)',(pid,uid,m,m*PAYMENT_PRICE,'pending',type(obj).__name__ if obj else 'unknown',fid,now()))
        states.pop(uid,None); audit(uid,'payment_submit',pid); await update.effective_message.reply_text(f'✅ Receipt submitted: {pid}')

async def recovery() -> None:
    with db() as c:
        c.execute("UPDATE projects SET status='stopped',updated_at=? WHERE status='running'",(now(),)); c.execute("UPDATE jobs SET status='recovered',finished_at=? WHERE status='running'",(now(),))

async def check_owner_premium(app: Any) -> None:
    global _owner_premium, _owner_premium_checked
    try:
        owner = await app.bot.get_chat(ADMIN_ID)
        _owner_premium = bool(getattr(owner, "is_premium", False))
        _owner_premium_checked = True
        log.info("Owner premium status: %s", _owner_premium)
    except Exception as e:
        log.warning("Could not determine owner premium status: %s", e)
        _owner_premium = False
        _owner_premium_checked = True

def build_application() -> Any:
    if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN is empty')
    migrate(); ensure_dirs(); app=ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('cancel',lambda u,c: text_router(u,c)))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.Document.ALL, document_router))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Sticker.ALL,media_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_router))
    return app

async def main() -> None:
    migrate(); await recovery(); app=build_application(); await app.initialize(); await check_owner_premium(app); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
    try: await asyncio.Event().wait()
    finally: await app.updater.stop(); await app.stop(); await app.shutdown()

if __name__ == '__main__':
    logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(name)s %(message)s'); asyncio.run(main())