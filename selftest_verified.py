"""Callback-independent regression tests for finalhost_verified.py."""
from __future__ import annotations
import asyncio, json, tempfile, zipfile
from pathlib import Path
import finalhost_verified as v
import finalhost as base


def make_zip(path: Path, name: str, data: bytes = b"print('ok')\n") -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, data)


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old = (
            base.DATA, base.DB, base.PROJECTS, base.LOGS, base.BACKUPS, base.TMP,
            v.DATA, v.DB, v.PROJECTS, v.LOGS, v.BACKUPS, v.TMP,
        )
        base.DATA = root / "data"
        base.DB = base.DATA / "runner.sqlite3"
        base.PROJECTS, base.LOGS, base.BACKUPS, base.TMP = (
            base.DATA / x for x in ("projects", "logs", "backups", "tmp")
        )
        v.DATA, v.DB, v.PROJECTS, v.LOGS, v.BACKUPS, v.TMP = (
            base.DATA, base.DB, base.PROJECTS, base.LOGS, base.BACKUPS, base.TMP
        )
        try:
            v.migrate()
            v.migrate()
            with v.db() as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {"users", "access_requests", "settings", "projects", "jobs", "env",
                            "payments", "notifications", "audit", "broadcasts", "backups"}
                assert required <= tables, tables
                assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"

            assert v.request_access_durable(42) is True
            assert v.request_access_durable(42) is False

            v.set_required_channel("@TeleNexusIo")
            assert v.required_channel_url().endswith("/TeleNexusIo")

            safe = root / "safe.zip"
            make_zip(safe, "runner.py")
            target = root / "target"
            v.safe_extract_zip(safe, target)
            (target / "runner.json").write_text(json.dumps({"runtime": "python", "entrypoint": "runner.py"}))
            assert v.scan_project(target)["entry"] == "runner.py"

            try:
                v.valid_relative(target, "../escape")
            except ValueError:
                pass
            else:
                raise AssertionError("path traversal accepted")

            traversal = root / "traversal.zip"
            make_zip(traversal, "../escape.txt")
            try:
                v.safe_extract_zip(traversal, root / "bad")
            except ValueError:
                pass
            else:
                raise AssertionError("ZIP traversal accepted")

            user = v.get_user(42)
            pid = "p1"
            project = base.PROJECTS / pid
            project.mkdir(parents=True)
            (project / "main.py").write_text("print(1)\n")
            with v.db() as c:
                c.execute(
                    "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (pid, 42, "demo", str(project), "python", "main.py", "stopped", 0, v.now(), v.now()),
                )

            v.new_file(pid, 42, "notes.txt", "hello")
            assert "notes.txt" in v.list_files(pid, 42)
            v.rename_file(pid, 42, "notes.txt", "renamed.txt")
            v.delete_file(pid, 42, "renamed.txt")

            v.env_set(pid, 42, "HELLO", "world")
            try:
                v.env_set(pid, 42, "BOT_TOKEN", "blocked")
            except ValueError:
                pass
            else:
                raise AssertionError("host secret accepted")

            env = v.build_env(pid)
            assert env["HELLO"] == "world" and "BOT_TOKEN" not in env and "DATA_DIR" not in env
            assert v.runtime_command(v.project_row(pid)) == [v.sys.executable, "-u", "main.py"]

            with v.db() as c:
                c.execute(
                    "INSERT INTO payments(id,user_id,months,amount,status,created_at) VALUES(?,?,?,?,?,?)",
                    ("PM-1", 42, 1, 5000, "pending", v.now()),
                )
            assert asyncio.run(v._payment("PM-1", True, 1)) is True
            assert v.is_premium(v.get_user(42))

            backup = v.backup_database()
            assert backup.exists()
            v.restore_database(backup)

            assert v.premium_message("x") == {"text": "x"}
            assert v.safe_name("../../my project") == "my_project"
            asyncio.run(v.recovery())

            source = Path(v.__file__).read_text(encoding="utf-8")
            for token in (
                "pstart:", "pstop:", "prestart:", "pdelete:", "logs:", "files:",
                "payapprove:", "payreject:", "copy_message", "broadcast_confirm", "broadcast_cancel",
            ):
                assert token in source, token
            print("selftest_verified: PASS")
        finally:
            (
                base.DATA, base.DB, base.PROJECTS, base.LOGS, base.BACKUPS, base.TMP,
                v.DATA, v.DB, v.PROJECTS, v.LOGS, v.BACKUPS, v.TMP,
            ) = old


if __name__ == "__main__":
    main()