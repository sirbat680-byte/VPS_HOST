"""Small callback-independent regression suite for finalhost_v2.py."""
from __future__ import annotations
import json, sqlite3, tempfile, zipfile
from pathlib import Path

import finalhost_v2 as v


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
            with v.db() as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                required = {"users", "access_requests", "settings", "projects", "jobs", "env",
                            "payments", "notifications", "audit", "broadcasts", "backups"}
                assert required <= tables, tables
                assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(v.SCHEMA_VERSION)

            v.migrate()
            assert v.setting("join_enabled") == "1"

            safe = root / "safe.zip"
            make_zip(safe, "runner.py")
            target = root / "target"
            v.safe_extract_zip(safe, target)
            (target / "runner.json").write_text(json.dumps({"runtime": "python", "entrypoint": "runner.py"}))
            info = v.scan_project(target)
            assert info["runtime"] == "python" and info["entry"] == "runner.py"
            assert v.valid_relative(target, "runner.py") == target / "runner.py"

            try:
                v.valid_relative(target, "../escape")
            except ValueError:
                pass
            else:
                raise AssertionError("root escape accepted")

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
            assert v.limits(user)[0] == 1
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
            with v.db() as c:
                c.execute("INSERT INTO env(project_id,key,value) VALUES(?,?,?)", (pid, "HELLO", "world"))
                c.execute("INSERT INTO env(project_id,key,value) VALUES(?,?,?)", (pid, "BOT_TOKEN", "do-not-pass"))
            env = v.build_env(pid)
            assert env["HELLO"] == "world" and "BOT_TOKEN" not in env and "DATA_DIR" not in env
            assert v.safe_name("../../my project") == "my_project"
            print("selftest_v2: PASS")
        finally:
            v.DATA, v.DB, v.PROJECTS, v.LOGS, v.BACKUPS, v.TMP = old


if __name__ == "__main__":
    main()