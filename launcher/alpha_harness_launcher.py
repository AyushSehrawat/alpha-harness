"""Alpha Harness on Windows: keep a Python and a venv, run the app, apply updates.

Frozen with PyInstaller into ``AlphaHarness.exe``. It carries ``uv.exe`` and nothing else —
the app's own dependencies are a third of a gigabyte, and a user who updates weekly should
not re-download ``pyarrow`` every week. So the heavy half is installed once into
``%LOCALAPPDATA%\\AlphaHarness`` and an update is just this project's wheel, a few megabytes.

The launcher is the *parent* of the app, which is what makes updating safe. Windows keeps a
running process's extension modules open, so an install that replaces ``duckdb`` under a live
app fails half-way. Here the app writes the version it wants and exits; the install happens
with nothing running; then the app starts again.

Deliberately standard library only: it is frozen separately from the app and must keep
working when the venv it manages does not.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

#: Written in by the release workflow; the version a fresh machine installs.
BUILD_VERSION = "0.0.0"
REPOSITORY = "residual-lab/alpha-harness"
DOWNLOAD = f"https://github.com/{REPOSITORY}/releases/download"

HOME_VARIABLE = "ALPHA_HARNESS_HOME"
REQUEST_FILE = "update-request.json"
ERROR_FILE = "update-error.json"
ACTIVE_FILE = "active-slot.txt"
LOCK_FILE = "running.lock"
LOG_FILE = "launcher.log"
#: Where the app serves; kept in step with ``alpha_harness.__main__``.
APP_PORT = 8000

#: Two environments, one live and one spare. An update builds the spare and flips the
#: pointer, so a failed install cannot touch what is currently working and a version that
#: will not start can be stepped back out of.
SLOTS = ("a", "b")

#: Long enough for a cold ``uv`` to fetch CPython and ~330 MB of wheels on a slow line.
INSTALL_TIMEOUT = 1800
#: An app that exits faster than this, right after an update, never came up at all. A real
#: session outlives it even if the user closes the browser immediately.
BOOT_SECONDS = 20.0


def home() -> Path:
    """Where everything the launcher owns lives. Per-user, so updating needs no elevation."""
    override = os.environ.get(HOME_VARIABLE)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    return Path(base) / "AlphaHarness"


def say(root: Path, message: str) -> None:
    """Append to the log. The exe is windowed, so this file is the only account of a run."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with (root / LOG_FILE).open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def fail(root: Path, message: str) -> None:
    """Report a failure the user can act on. Windowed processes have nowhere else to speak."""
    say(root, f"FATAL {message}")
    # Looked up rather than imported: ``ctypes.windll`` exists only on Windows, and this
    # module is read and exercised on other platforms.
    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        windll.user32.MessageBoxW(
            None, f"{message}\n\nDetails: {root / LOG_FILE}", "Alpha Harness", 0x10
        )


#: Held open for the life of the process; the OS drops it however this one ends.
_lock: int | None = None


def claim(root: Path) -> bool:
    """Whether this process may run, or another copy already holds the lock.

    First start fetches a Python and ~330 MB of wheels behind a windowed exe that shows
    nothing, so the user double-clicks again. Two launchers then run ``uv`` over one venv,
    race for port 8000 and open DuckDB twice, which is a corrupted install rather than a
    slow one.
    """
    global _lock
    if sys.platform != "win32":
        return True  # The launcher is exercised here, never shipped here.

    import msvcrt

    try:
        # Never ``open(..., "wb")``. That asks Windows for CREATE_ALWAYS, and truncating a
        # file whose first byte another process has locked is refused with a lock violation —
        # which would surface as an unhandled PermissionError in a process that has no
        # console to print it to. O_RDWR|O_CREAT opens fine and lets the lock do the deciding.
        handle = os.open(root / LOCK_FILE, os.O_RDWR | os.O_CREAT)
    except OSError as exc:
        # A lock we cannot create is not a reason to refuse to start.
        say(root, f"could not open the lock file, running unguarded: {exc}")
        return True
    try:
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(handle)
        return False
    _lock = handle
    return True


def bundled_uv() -> Path:
    """``uv.exe`` as PyInstaller unpacked it, or beside this script when run from source."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base or Path(__file__).parent) / "uv.exe"


def ensure_uv(root: Path) -> Path:
    """Copy the bundled uv out once, so an install is not reading from a temporary folder."""
    target = root / "uv.exe"
    source = bundled_uv()
    if source.exists() and (not target.exists() or source.stat().st_size != target.stat().st_size):
        target.write_bytes(source.read_bytes())
    return target


def other(slot: str) -> str:
    return "b" if slot == "a" else "a"


def active(root: Path) -> str:
    recorded = (
        (root / ACTIVE_FILE).read_text(encoding="utf-8").strip()
        if (root / ACTIVE_FILE).exists()
        else ""
    )
    return recorded if recorded in SLOTS else "a"


def activate(root: Path, slot: str) -> None:
    """Point at a slot. One small write, which is what makes the swap atomic enough."""
    (root / ACTIVE_FILE).write_text(slot, encoding="utf-8")
    say(root, f"active slot -> {slot}")


def venv_python(root: Path, slot: str) -> Path:
    """The console interpreter, which is what ``uv`` expects to be pointed at."""
    return root / f"venv-{slot}" / "Scripts" / "python.exe"


def venv_pythonw(root: Path, slot: str) -> Path:
    """The windowed interpreter, so running the app flashes no console at the user."""
    return root / f"venv-{slot}" / "Scripts" / "pythonw.exe"


def slot_version(root: Path, slot: str) -> str | None:
    """What is installed in a slot, or ``None`` when it holds nothing usable."""
    if not venv_python(root, slot).exists():
        return None
    recorded = read_json(root / f"slot-{slot}.json").get("version")
    return recorded if isinstance(recorded, str) else None


def read_json(path: Path) -> dict[str, object]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def requested(root: Path) -> str | None:
    wanted = read_json(root / REQUEST_FILE).get("version")
    return wanted if isinstance(wanted, str) else None


def requested_wheel(root: Path) -> str | None:
    """The wheel URL the app read off the release, rather than one built from the version."""
    url = read_json(root / REQUEST_FILE).get("wheelUrl")
    return url if isinstance(url, str) and url.startswith(DOWNLOAD) else None


def note_failure(root: Path, version: str, reason: str) -> None:
    """Leave the app something to say. Starting the old version again with the same Update
    button on screen and no account of why is the one outcome a user cannot make sense of."""
    (root / ERROR_FILE).write_text(
        json.dumps({"version": version, "error": reason[:400], "time": time.time()}),
        encoding="utf-8",
    )


def install(root: Path, uv: Path, slot: str, version: str, wheel: str | None = None) -> None:
    """Build ``slot`` from scratch at exactly ``version``. Raises on failure.

    Always the slot that is *not* running. Installing over a live environment is how an
    interrupted upgrade bricks it: ``uv`` has already replaced half the files when the network
    drops, and "keeping the old version" is then a claim about a tree that no longer works.
    Here a failure leaves the running slot untouched by construction.

    Rebuilding rather than upgrading in place costs no download — the wheels are in
    ``UV_CACHE_DIR`` from the last install and uv hardlinks them — and it means a slot never
    carries a package left behind by a version two releases ago.

    ``wheel`` is the URL the app read off the release. Only a first install, which has no
    release to read, falls back to the conventional name.
    """
    wheel = wheel or f"{DOWNLOAD}/v{version}/alpha_harness-{version}-py3-none-any.whl"
    constraints = f"{DOWNLOAD}/v{version}/constraints.txt"
    say(root, f"installing {version} from {wheel}")

    # uv keeps its Python builds and cache inside the same directory, so uninstalling the app
    # is deleting one folder and nothing is left behind in the user's profile.
    environment = os.environ | {
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "UV_CACHE_DIR": str(root / "cache"),
        "UV_NO_CONFIG": "1",
    }

    def run(*arguments: str) -> None:
        done = subprocess.run(  # noqa: S603 - fixed argv, version comes from our own release
            [str(uv), *arguments],
            capture_output=True,
            text=True,
            # Named, never inherited. ``text=True`` otherwise decodes with the console code
            # page — CP1252 on most Windows machines — and CP1252 has undefined bytes, so one
            # multi-byte character in uv's output raises UnicodeDecodeError and fails an
            # install that actually worked.
            encoding="utf-8",
            errors="replace",
            timeout=INSTALL_TIMEOUT,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        say(root, f"uv {arguments[0]} -> {done.returncode} {done.stderr.strip()[-2000:]}")
        if done.returncode != 0:
            raise RuntimeError(f"uv {arguments[0]} failed: {done.stderr.strip()[-400:]}")

    # The marker goes first: a half-built slot must never read as a working one.
    (root / f"slot-{slot}.json").unlink(missing_ok=True)
    shutil.rmtree(root / f"venv-{slot}", ignore_errors=True)
    run("venv", "--python", "3.14", str(root / f"venv-{slot}"))
    # Constrained to the versions the release was locked against, so a transitive dependency
    # publishing a breaking version cannot break an install that worked yesterday.
    run("pip", "install", "--python", str(venv_python(root, slot)), "-c", constraints, wheel)
    (root / f"slot-{slot}.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    say(root, f"installed {version} into slot {slot}")


def start(root: Path, slot: str) -> int:
    """Run the app to completion, with its output in the log. Returns its exit code."""
    environment = os.environ | {HOME_VARIABLE: str(root), "PYTHONUTF8": "1"}
    say(root, f"starting app from slot {slot}")
    with (root / LOG_FILE).open("a", encoding="utf-8") as handle:
        done = subprocess.run(  # noqa: S603 - the venv this launcher built
            [str(venv_pythonw(root, slot)), "-m", "alpha_harness"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    say(root, f"app exited {done.returncode}")
    return done.returncode


def main() -> int:
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    say(root, f"launcher {BUILD_VERSION}")
    if not claim(root):
        # Double-clicking a second time is not an error, it is someone asking to see the app.
        # Unless the first copy is still doing its first install, in which case there is
        # nothing to show yet and a browser tab would just fail to connect.
        if any(slot_version(root, slot) for slot in SLOTS):
            say(root, "already running; opening the browser at it")
            webbrowser.open(f"http://127.0.0.1:{APP_PORT}")
        else:
            fail(
                root,
                "Alpha Harness is still installing.\n\nThis takes a couple of minutes the "
                "first time. It opens in your browser when it is ready.",
            )
        return 0
    try:
        uv = ensure_uv(root)
    except OSError as exc:
        fail(root, f"Could not unpack uv: {exc}")
        return 1

    while True:
        slot = active(root)
        running = slot_version(root, slot)
        wanted = requested(root) or running or BUILD_VERSION
        swapped = False

        if running != wanted:
            # The empty slot, so the one in use survives a failure untouched. With nothing
            # installed at all there is no "in use" and the first slot is the only choice.
            target = other(slot) if running is not None else slot
            try:
                install(root, uv, target, wanted, requested_wheel(root))
                activate(root, target)
                slot, swapped = target, True
            except Exception as exc:  # noqa: BLE001 - every failure ends the same way
                if running is None:
                    fail(root, f"Could not install Alpha Harness {wanted}.\n{exc}")
                    return 1
                say(root, f"install failed, staying on {running}: {exc}")
                note_failure(root, wanted, str(exc))
        # Cleared whether or not it was applied: left behind, a request that cannot install
        # would retry on every start for good.
        (root / REQUEST_FILE).unlink(missing_ok=True)

        began = time.monotonic()
        code = start(root, slot)
        alive = time.monotonic() - began

        # A version that dies on the way up cannot report anything itself: no server, no page,
        # no button. The launcher is the only thing left that can notice, so it does.
        if swapped and code != 0 and alive < BOOT_SECONDS and slot_version(root, other(slot)):
            previous = other(slot)
            say(root, f"{wanted} exited {code} after {alive:.1f}s; reverting to {previous}")
            activate(root, previous)
            note_failure(
                root,
                wanted,
                f"It closed straight after starting (exit code {code}). "
                f"Alpha Harness went back to {slot_version(root, previous)}.",
            )
            continue

        if requested(root) is None:
            return code
        say(root, "update requested; restarting")


if __name__ == "__main__":
    raise SystemExit(main())
