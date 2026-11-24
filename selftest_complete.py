"""Callback-independent regression tests for finalhost_complete.py."""
from __future__ import annotations
import asyncio, json, tempfile, zipfile
from pathlib import Path
import finalhost_complete as v


def make_zip(path: Path, name: str, data: bytes = b"print('ok')\n") -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, data)


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old = (v.DATA, v.DB, v.PROJECTS, v.LOGS, v.BACKUPS, v.TMP)
        v.DATA = root / "data"
        v.DB = v.DATA / "runner.sqlite3"
        v.PROJECTS, v.LOGS, v.BACKUPS, v.TMP = (
            v.DATA / x for x in ("projects", "logs", "backups", "tmp")
        )
        try:
            v.migrate()
            v.migrate()
            with v.db() as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                assert {"users", "access_requests", "settings", "projects", "jobs", "env",
                        "payments", "notifications", "audit", "broadcasts", "backups"} <= tables
                assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(v.SCHEMA_VERSION)

            assert v.request_access_durable(42) is True and v.request_access_durable(42) is False
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

            symlink = root / "symlink.zip"
            with zipfile.ZipFile(symlink, "w") as z:
                info = zipfile.ZipInfo("link")
                info.external_attr = (0o120777 << 16)
                z.writestr(info, "target")
            try:
                v.safe_extract_zip(symlink, root / "badlink")
            except ValueError:
                pass
            else:
                raise AssertionError("ZIP symlink accepted")

            user = v.get_user(42)
            pid = "p1"
            project = v.PROJECTS / pid
            project.mkdir(parents=True)
            (project / "main.py").write_text("print(1)\n")
            with v.db() as c:
                c.execute(
                    "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (pid, 42, "demo", str(project), "python", "main.py", "stopped", 0, v.now(), v.now()),
                )
            assert v.used_quota(42) >= 1
            v.env_set(pid, 42, "HELLO", "world")
            try:
                v.env_set(pid, 42, "BOT_TOKEN", "blocked")
            except ValueError:
                pass
            else:
                raise AssertionError("host secret accepted")
            assert v.build_env(pid)["HELLO"] == "world" and "BOT_TOKEN" not in v.build_env(pid)
            v.new_file(pid, 42, "notes.txt", "hello")
            assert "notes.txt" in v.list_files(pid, 42)
            v.rename_file(pid, 42, "notes.txt", "renamed.txt")
            v.delete_file(pid, 42, "renamed.txt")
            assert v.runtime_command(v.project_row(pid)) == [v.sys.executable, "-u", "main.py"]
            backup = v.backup_database()
            assert backup.exists()
            v.restore_database(backup)
            assert v.premium_message("x", "premium") == {"text": "x"}
            assert v.safe_name("../../my project") == "my_project"
            asyncio.run(v.recovery())
            print("selftest_complete: PASS")
        finally:
            v.DATA, v.DB, v.PROJECTS, v.LOGS, v.BACKUPS, v.TMP = old


if __name__ == "__main__":
    main()