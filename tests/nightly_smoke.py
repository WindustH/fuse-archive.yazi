#!/usr/bin/env python3
"""Exercise the plugin in an installed Yazi.

Run: python3 tests/nightly_smoke.py
Use --real-fuse to exercise actual FUSE mounts instead of simulated commands.
Requires yazi, xxh128sum, bash, and Python 3 on Linux. All test files and
configuration live in a temporary directory.
"""

import argparse
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import zipfile


MOCK_COMMAND = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, zipfile
root = pathlib.Path(os.environ["FUSE_SMOKE_ROOT"])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with (root / "commands.jsonl").open("a") as f:
    f.write(json.dumps([name, args]) + "\n")
if os.environ.get("FUSE_SMOKE_REAL") and name != "pgrep":
    os.execv(os.environ["FUSE_SMOKE_REAL_" + name.upper().replace("-", "_")], sys.argv)
if name == "fuse-archive":
    if args == ["--version"]:
        print("fuse-archive 1.26")
    else:
        archive, target = args[-2:]
        target = pathlib.Path(target)
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            z.extractall(target)
        (target / ".mock-mounted").touch()
elif name == "mountpoint":
    sys.exit(0 if (pathlib.Path(args[-1]) / ".mock-mounted").exists() else 1)
elif name == "pgrep":
    print("1")
elif name == "findmnt":
    for p in (root / "mounts").rglob(".mock-mounted"):
        print(p.parent)
elif name == "umount":
    (pathlib.Path(args[-1]) / ".mock-mounted").unlink()
'''


def run(root, real_fuse=False):
    plugin = Path(__file__).resolve().parents[1]
    config = root / "config"
    installed = config / "plugins" / "fuse-archive.yazi"
    shutil.copytree(plugin, installed, ignore=shutil.ignore_patterns(".git", "tests"))

    # Surface asynchronous Lua failures in the log as well as the Tasks panel.
    main = installed / "main.lua"
    source = main.read_text().replace(
        "entry = function(_, job)\n",
        "entry = function(_, job)\n\t\tlocal function execute()\n",
        1,
    ).replace(
        "\n\tend,\n\tsetup = setup,",
        '\n\t\tend\n\t\tlocal ok, err = xpcall(execute, tostring)\n'
        '\t\tif not ok then ya.err("FUSE_SMOKE_FAILURE", err) end\n'
        '\tend,\n\tsetup = setup,',
    )
    main.write_text(source)

    for name in ("bin", "work", "mounts", "cache", "state", "runtime", "data"):
        (root / name).mkdir(mode=0o700)
    for name in ("fuse-archive", "mountpoint", "pgrep", "findmnt", "umount"):
        path = root / "bin" / name
        path.write_text(MOCK_COMMAND)
        path.chmod(0o755)

    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("subdir/hello.txt", "nightly smoke test\n")
    archive = root / "work" / "outer '中文 archive.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("inner.zip", inner.getvalue())

    (config / "init.lua").write_text(
        'require("fuse-archive"):setup({ mount_root_dir = '
        + json.dumps(str(root / "mounts"))
        + ' })\n'
    )
    bindings = {
        "m": "plugin fuse-archive -- mount",
        "l": "plugin fuse-archive -- leave",
        "e": "plugin fuse-archive -- mount --url=" + shlex.quote(str(archive)),
        "s": "plugin smoke-snapshot",
    }
    (config / "keymap.toml").write_text(
        "[mgr]\nprepend_keymap = [\n"
        + "".join(
            "{ on = " + json.dumps(key) + ", run = " + json.dumps(action) + " },\n"
            for key, action in bindings.items()
        )
        + "]\n"
    )
    snapshot = config / "plugins" / "smoke-snapshot.yazi"
    snapshot.mkdir()
    (snapshot / "main.lua").write_text(
        '--- @sync entry\nreturn { entry = function()\n'
        'ya.dbg("FUSE_SMOKE_CWD=" .. tostring(cx.active.current.cwd))\nend }\n'
    )

    env = os.environ.copy()
    for key in (
        "ZELLIJ", "ZELLIJ_SESSION_NAME", "TMUX", "TERM_PROGRAM",
        "KITTY_WINDOW_ID", "WEZTERM_PANE", "YAZI_ID", "YAZI_LEVEL",
    ):
        env.pop(key, None)
    env.update(
        YAZI_CONFIG_HOME=str(config),
        XDG_CACHE_HOME=str(root / "cache"),
        XDG_STATE_HOME=str(root / "state"),
        XDG_RUNTIME_DIR=str(root / "runtime"),
        XDG_DATA_HOME=str(root / "data"),
        PATH=str(root / "bin") + os.pathsep + env["PATH"],
        TERM="xterm-256color",
        YAZI_LOG="debug",
        FUSE_SMOKE_ROOT=str(root),
    )
    if real_fuse:
        assert Path("/dev/fuse").exists(), "--real-fuse requires /dev/fuse"
        env["FUSE_SMOKE_REAL"] = "1"
        for name in ("fuse-archive", "mountpoint", "findmnt", "umount"):
            program = shutil.which(name)
            assert program, f"--real-fuse requires {name}"
            env["FUSE_SMOKE_REAL_" + name.upper().replace("-", "_")] = program
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 960, 640))
    process = subprocess.Popen(
        ["yazi", "--client-id", str(time.time_ns() // 1000), str(root / "work")],
        stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
    )
    os.close(slave)
    terminal = bytearray()
    log_file = root / "state" / "yazi" / "yazi.log"

    def log():
        return log_file.read_text() if log_file.exists() else ""

    def pump(seconds=0.15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and process.poll() is None:
            if not select.select([master], [], [], 0.05)[0]:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            terminal.extend(data)
            for query, reply in (
                (b"\x1b[c", b"\x1b[?1;2c"),
                (b"\x1b[0c", b"\x1b[?1;2c"),
                (b"\x1b[6n", b"\x1b[1;1R"),
                (b"\x1b[16t", b"\x1b[6;20;8t"),
                (b"\x1b[14t", b"\x1b[4;640;960t"),
            ):
                if query in data:
                    os.write(master, reply)

    def key(value):
        os.write(master, value.encode())
        pump(0.35)

    def cwd():
        previous = log().count("FUSE_SMOKE_CWD=")
        key("s")
        deadline = time.monotonic() + 3
        while log().count("FUSE_SMOKE_CWD=") <= previous:
            assert process.poll() is None, "Yazi exited unexpectedly"
            assert time.monotonic() < deadline, "Snapshot timed out"
            pump()
        line = log().rsplit("FUSE_SMOKE_CWD=", 1)[1].splitlines()[0]
        return Path(line.removesuffix('"'))

    try:
        pump(1.5)
        assert cwd() == root / "work"
        key("m")
        outer_mount = cwd()
        assert outer_mount.parent == root / "mounts/yazi/fuse-archive", outer_mount
        assert outer_mount.name.startswith(archive.name + ".tmp."), outer_mount
        assert (outer_mount / "inner.zip").is_file()
        key("m")
        inner_mount = cwd()
        assert inner_mount != outer_mount
        assert (inner_mount / "subdir/hello.txt").read_text() == "nightly smoke test\n"
        key("m")
        assert cwd() == inner_mount / "subdir", "Directory entry fallback failed"
        key("m")
        assert cwd() == inner_mount / "subdir", "Non-archive entry changed directory"
        for expected in (inner_mount, outer_mount, root / "work"):
            key("l")
            assert cwd() == expected, "Archive leave failed"
        for action in ("m", "e"):
            key(action)
            assert cwd() == outer_mount, "Remount/explicit URL entry failed"
            key("l")
            assert cwd() == root / "work"
        key("e")
        assert cwd() == outer_mount
        commands = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
        mounts = [args for name, args in commands if name == "fuse-archive" and args != ["--version"]]
        assert len(mounts) == 2, f"Existing mounts were mounted again: {mounts}"
        key("q")
        deadline = time.monotonic() + 3
        while process.poll() is None and time.monotonic() < deadline:
            pump()
        assert process.poll() == 0, "Quit hook prevented Yazi from exiting"
        assert not list((root / "mounts").rglob(".mock-mounted")), "Quit cleanup failed"
        if real_fuse:
            assert not os.path.ismount(outer_mount), "Outer archive is still mounted"
            assert not os.path.ismount(inner_mount), "Inner archive is still mounted"
        assert "FUSE_SMOKE_FAILURE" not in log(), log()
        assert "cancelled by `fuse-archive`" not in log(), log()
        print("PASS: nightly API, quoted/Unicode paths, nested archives, directory/file fallback,")
        mode = "real FUSE" if real_fuse else "simulated FUSE"
        print(f"      leave, mount reuse, explicit URL entry, and quit cleanup ({mode}).")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        os.close(master)
        (root / "terminal.bin").write_bytes(terminal)
        if real_fuse:
            mount_dir = root / "mounts" / "yazi" / "fuse-archive"
            for path in sorted(mount_dir.glob("*"), reverse=True):
                if os.path.ismount(path):
                    subprocess.run(["umount", str(path)], check=True, timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-fuse", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="yazi-fuse-smoke-") as directory:
        try:
            run(Path(directory), real_fuse=args.real_fuse)
        except Exception:
            retained = Path(tempfile.mkdtemp(prefix="yazi-fuse-smoke-failed-"))
            shutil.copytree(
                directory, retained, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("runtime"),
            )
            print(f"Failure logs: {retained}")
            raise
