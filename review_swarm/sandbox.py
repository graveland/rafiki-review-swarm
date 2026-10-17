# review-swarm: the sandbox daemon (a rafiki build under test) and the live/sandbox target switch
"""The sandbox runs rafiki from binaries built out of a checkout's HEAD,
against its own database, with every XDG directory isolated under
<data>/.daemon so nothing leaks in from (or out to) the operator's real rafiki
setup. It exists to try a rafiki change before deploying it; real reviews run
on the live daemon through the operator's own profile."""

import datetime
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .common import Fail, run
from .rafiki_io import Rafiki

_RAFIKI_SRC_ENV = os.environ.get("REVIEW_RAFIKI_SRC")
RAFIKI_SRC = Path(_RAFIKI_SRC_ENV).expanduser() if _RAFIKI_SRC_ENV else None


def rafiki_src():
    if RAFIKI_SRC is None:
        raise Fail("the sandbox daemon builds rafiki from a checkout: set REVIEW_RAFIKI_SRC to its path")
    return RAFIKI_SRC
PG_ADMIN = os.environ.get("REVIEW_PG", "postgres://postgres@127.0.0.1:5432")
REAL_SERVICE_ENV = Path("~/.config/rafiki/service.env").expanduser()
PROXY_LISTEN = "127.0.0.1:8635"
EXECUTOR_NAME = "review-swarm"
# One token profile for everything: presets, git sources and children all
# belong to the bootstrap user, and the executor syncs that owner's git
# sources. A token-less profile may still exist on disk: children spawned
# before a profile switch are anonymous-owned.
PROFILE = "review"

# Inherited variables that must never reach the sandbox daemon or its client:
# rafiki's own settings (several are hard client errors, and any of them would
# point the tooling at the operator's real daemon) and provider credentials
# (the daemon gets exactly the ones service.env supplies).
STRIPPED_PREFIXES = ("RAFIKI_", "ANTHROPIC_", "OPENROUTER_", "XDG_")


def read_env_value(path, key):
    for line in path.read_text().splitlines():
        line = line.strip()
        line = line.removeprefix("export ")
        if not line.startswith(key + "="):
            continue
        v = line[len(key) + 1:].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        return v
    return None


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Sandbox:
    def __init__(self, home):
        self.dir = home.data / ".daemon"
        self.bin = self.dir / "bin"
        self.logs = self.dir / "logs"
        self.state_file = self.dir / "daemon.json"
        self.pid_file = self.dir / "rafikid.pid"
        self.exec_pid_file = self.dir / "executor.pid"

    def xdg_dirs(self):
        return {
            "XDG_CONFIG_HOME": self.dir / "config",
            "XDG_STATE_HOME": self.dir / "state",
            "XDG_DATA_HOME": self.dir / "data",
            "XDG_CACHE_HOME": self.dir / "cache",
            "XDG_RUNTIME_DIR": self.dir / "run",
        }

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(STRIPPED_PREFIXES)}
        for k, v in self.xdg_dirs().items():
            env[k] = str(v)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        return env

    def socket_path(self):
        return self.dir / "run" / "rafiki" / "controller.sock"

    def service_env_path(self):
        return self.dir / "config" / "rafiki" / "service.env"

    def load_state(self):
        if not self.state_file.exists():
            raise Fail("sandbox not initialised; run `review-swarm sandbox init` first")
        return json.loads(self.state_file.read_text())

    def save_state(self, state):
        self.state_file.write_text(json.dumps(state, indent=2) + "\n")

    def rafiki(self, *args, check=True, input=None, profile=True):
        cmd = [self.bin / "rafiki"] + (["-P", PROFILE] if profile else [])
        return run(cmd + list(args), env=self.env(), check=check, input=input)

    def running_pid(self, pid_file):
        if not pid_file.exists():
            return None
        pid = int(pid_file.read_text().strip())
        return pid if pid_alive(pid) else None

    def start_background(self, cmd, pid_file, log_name):
        # Popen dups the log fd into the child, so the handle is only needed for
        # the header line: closing it here does not stop the child's own writes.
        with open(self.logs / log_name, "a") as log:
            log.write(f"\n--- review-swarm start {datetime.datetime.now().isoformat()} ---\n")
            log.flush()
            proc = subprocess.Popen(
                [str(c) for c in cmd], env=self.env(), cwd=self.dir,
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        pid_file.write_text(f"{proc.pid}\n")
        return proc

    def stop_background(self, pid_file, what):
        pid = self.running_pid(pid_file)
        if not pid:
            pid_file.unlink(missing_ok=True)
            print(f"{what}: not running")
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 20
        while pid_alive(pid) and time.time() < deadline:
            time.sleep(0.25)
        if pid_alive(pid):
            raise Fail(f"{what} (pid {pid}) still alive 20s after SIGTERM; not escalating, investigate")
        pid_file.unlink(missing_ok=True)
        print(f"{what}: stopped (pid {pid})")

    def build_binaries(self):
        """Build both binaries from a clean export of the rafiki checkout's HEAD,
        never its working tree: other sessions edit that checkout, and a build of
        their uncommitted work would leave the sandbox on code no commit
        describes. Uncommitted edits there are reported, not built."""
        rafiki = rafiki_src()
        commit = run(["git", "-C", rafiki, "rev-parse", "HEAD"]).stdout.strip()
        dirty = bool(run(["git", "-C", rafiki, "status", "--porcelain"]).stdout.strip())
        if dirty:
            print(f"note: {rafiki} has uncommitted edits; building committed HEAD {commit[:12]} only")
        src = self.dir / "build-src" / commit
        if not src.exists():
            src.mkdir(parents=True)
            archive = subprocess.run(["git", "-C", str(rafiki), "archive", "HEAD"], capture_output=True, check=True)
            subprocess.run(["tar", "-x", "-C", str(src)], input=archive.stdout, check=True)
        self.bin.mkdir(parents=True, exist_ok=True)
        # The module path belongs to the checkout, not to this engine: read it
        # from the exported tree so a fork or a rename still builds.
        module = next(line.split()[1] for line in (src / "go.mod").read_text().splitlines()
                      if line.startswith("module "))
        ldflags = f"-X {module}/pkg/version.Version={commit[:12]}"
        for name in ("rafikid", "rafiki"):
            print(f"building {name} @ {commit[:12]}")
            run(["go", "build", "-ldflags", ldflags, "-o", self.bin / name, f"./cmd/{name}"], cwd=src, capture=False)
        return commit, False

    def init(self, db, reuse_db):
        if self.state_file.exists():
            raise Fail(f"already initialised ({self.state_file}); `sandbox status` shows it")
        if not REAL_SERVICE_ENV.exists():
            raise Fail(f"{REAL_SERVICE_ENV} not found; it's where OPENROUTER_API_KEY is copied from")
        api_key = read_env_value(REAL_SERVICE_ENV, "OPENROUTER_API_KEY")
        if not api_key:
            raise Fail(f"OPENROUTER_API_KEY not set in {REAL_SERVICE_ENV}")

        db = db or "rafiki_review_" + datetime.date.today().strftime("%Y%m%d")
        exists = psql(dsn_for("postgres"), f"select 1 from pg_database where datname = '{db}'").stdout.strip()
        if exists and not reuse_db:
            raise Fail(f"database {db} already exists; pass --reuse-db to use it, or --db for another name")

        for d in list(self.xdg_dirs().values()) + [self.logs]:
            d.mkdir(parents=True, exist_ok=True)
        (self.dir / "run").chmod(0o700)

        commit, dirty = self.build_binaries()
        if not exists:
            print(f"creating database {db}")
            psql(dsn_for("postgres"), f'create database "{db}"')
        print(f"migrating {db}")
        run([self.bin / "rafikid", "migrate", "--db", dsn_for(db)], env=self.env(), capture=False)

        envfile = self.service_env_path()
        envfile.parent.mkdir(parents=True, exist_ok=True)
        envfile.write_text(
            "# review-swarm sandbox daemon environment (written by `sandbox init`)\n"
            f"RAFIKI_DB={dsn_for(db)}\n"
            f"RAFIKI_PROXY_LISTEN={PROXY_LISTEN}\n"
            f"OPENROUTER_API_KEY={api_key}\n"
        )
        envfile.chmod(0o600)

        self.save_state({
            "db": db,
            "dsn": dsn_for(db),
            "rafiki_src": str(rafiki_src()),
            "rafiki_commit": commit,
            "rafiki_dirty": dirty,
            "initialised_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "user_created": False,
        })
        print(f"initialised: db={db} rafiki={commit[:12]}{' (dirty)' if dirty else ''}")
        print("next: review-swarm sandbox up")

    def rebuild(self):
        """Rebuild the binaries from the rafiki checkout's current HEAD and apply
        any new migrations. Refuses while the daemon runs: swapping binaries under
        a live daemon/executor pair leaves them on different versions."""
        state = self.load_state()
        if self.running_pid(self.pid_file) or self.running_pid(self.exec_pid_file):
            raise Fail("stop it first: review-swarm sandbox down")
        commit, dirty = self.build_binaries()
        print(f"migrating {state['db']}")
        run([self.bin / "rafikid", "migrate", "--db", state["dsn"]], env=self.env(), capture=False)
        history = state.setdefault("rebuilds", [])
        history.append({"from": state["rafiki_commit"], "to": commit, "dirty": dirty,
                        "at": datetime.datetime.now(datetime.UTC).isoformat()})
        state["rafiki_commit"], state["rafiki_dirty"] = commit, dirty
        self.save_state(state)
        print(f"rebuilt: rafiki={commit[:12]}{' (dirty)' if dirty else ''}; next: review-swarm sandbox up")

    def ensure_profile(self):
        # `rafiki profile list` has no JSON output, so read the file it edits.
        profiles = self.dir / "config" / "rafiki" / "profiles.toml"
        if not (profiles.exists() and f"[profile.{PROFILE}]" in profiles.read_text()):
            self.rafiki("profile", "add", PROFILE, "--socket", self.socket_path(), profile=False)
        self.rafiki("profile", "use", PROFILE, profile=False)

    def up(self):
        state = self.load_state()
        pid = self.running_pid(self.pid_file)
        if pid:
            print(f"already running (pid {pid})")
        else:
            sock = self.socket_path()
            if sock.exists():
                sock.unlink()  # stale socket from a crashed daemon; the pid file says nothing is serving it
            proc = self.start_background([self.bin / "rafikid"], self.pid_file, "rafikid.log")
            deadline = time.time() + 30
            while not sock.exists():
                if proc.poll() is not None:
                    raise Fail(f"rafikid exited ({proc.returncode}) during startup; see {self.logs / 'rafikid.log'}")
                if time.time() > deadline:
                    raise Fail(f"no control socket after 30s; see {self.logs / 'rafikid.log'}")
                time.sleep(0.25)
            print(f"started rafikid (pid {proc.pid}), socket {sock}")

        self.ensure_profile()
        if not state["user_created"]:
            print("bootstrapping the first user")
            self.rafiki("user", "create", PROFILE)
            state["user_created"] = True
            self.save_state(state)
        self.start_executor()
        print(self.rafiki("status").stdout.rstrip())

    def executor_row(self):
        doc = json.loads(self.rafiki("executor", "list", "-j").stdout or "{}")
        for row in doc.get("rows") or []:
            if EXECUTOR_NAME in json.dumps(row):
                return row
        return None

    def start_executor(self):
        pid = self.running_pid(self.exec_pid_file)
        if pid:
            print(f"executor already running (pid {pid})")
            return
        if EXECUTOR_NAME not in self.rafiki("executor", "name", check=False).stdout:
            self.rafiki("executor", "name", EXECUTOR_NAME)
        spill = self.dir / "spill"
        spill.mkdir(exist_ok=True)
        cmd = [self.bin / "rafiki", "-P", PROFILE, "executor", "serve",
               "--connect-socket", self.dir / "run" / "rafiki" / "executor.sock",
               "--root", Path.home() / "home", "--no-lsp", "--rtk", "off",
               "--spill-dir", spill, "--launch", "script"]
        if not (self.dir / "data" / "rafiki" / "executor.cred").exists():
            # First start: trade a one-time enrollment token for the durable
            # credential file serve writes. The token is spent on first use, so
            # its brief appearance in argv is not a standing exposure.
            out = self.rafiki("executor", "enroll", "--name", EXECUTOR_NAME).stdout
            token = [l for l in out.splitlines() if l.strip()][-1].strip()
            cmd += ["--enroll-token", token]
        proc = self.start_background(cmd, self.exec_pid_file, "executor.log")
        deadline = time.time() + 30
        while not (self.executor_row() or {}).get("connected"):
            if proc.poll() is not None:
                raise Fail(f"executor exited ({proc.returncode}) during startup; see {self.logs / 'executor.log'}")
            if time.time() > deadline:
                raise Fail(f"executor not connected after 30s; see {self.logs / 'executor.log'} and `rafiki executor list`")
            time.sleep(0.5)
        print(f"started executor {EXECUTOR_NAME} (pid {proc.pid}): {json.dumps(self.executor_row())[:300]}")

    def down(self):
        self.stop_background(self.exec_pid_file, "executor")
        self.stop_background(self.pid_file, "rafikid")
        print("database untouched")

    def status(self):
        state = self.load_state()
        pid = self.running_pid(self.pid_file)
        epid = self.running_pid(self.exec_pid_file)
        print(f"db:      {state['db']}")
        print(f"rafiki:  {state['rafiki_commit'][:12]}{' (dirty)' if state['rafiki_dirty'] else ''}")
        print(f"daemon:  {'running, pid ' + str(pid) if pid else 'stopped'}")
        print(f"executor:{' running, pid ' + str(epid) if epid else ' stopped'}")
        print(f"socket:  {self.socket_path()}")
        print(f"logs:    {self.logs / 'rafikid.log'}")
        if pid:
            print(self.rafiki("status").stdout.rstrip())

    def print_env(self):
        for k, v in self.xdg_dirs().items():
            print(f"export {k}={v}")
        print(f"export PATH={self.bin}:$PATH")
        print(f"# then: rafiki -P {PROFILE} list")


def psql(dsn, sql):
    return run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", dsn, "-c", sql])


def dsn_for(db):
    return f"{PG_ADMIN}/{db}?sslmode=disable"


class Target:
    """Which rafiki the operator CLI talks to. live: the operator's own
    profile (or --profile) and the rafiki on PATH. sandbox: the sandbox's
    binaries, isolated environment and review profile (or --profile)."""

    def __init__(self, kind, home, profile=None):
        self.kind = kind
        self.sandbox = Sandbox(home) if kind == "sandbox" else None
        self.profile = profile or (PROFILE if self.sandbox else None)
        self.env = self.sandbox.env() if self.sandbox else dict(os.environ)

    def cli(self, *args, check=True, input=None):
        cmd = ["rafiki"] + (["-P", self.profile] if self.profile else []) + list(args)
        return run(cmd, env=self.env, check=check, input=input)

    def client(self):
        if self.sandbox:
            # from_profile shells out to `rafiki profile show` with this
            # process's environment: point both at the sandbox.
            os.environ.clear()
            os.environ.update(self.env)
        return Rafiki.from_profile(self.profile)

    def describe(self):
        """What a run records about the rafiki it runs on."""
        out = self.cli("status").stdout
        version = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("version:")), "?")
        d = {"target": self.kind, "version": version}
        if self.sandbox:
            d["db"] = self.sandbox.load_state()["db"]
        return d
