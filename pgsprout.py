"""pgsprout: masked golden copies of Postgres, sprouted into local branches in seconds.

Goldens (<db>_golden) are rebuilt from a source (staging, a read replica) with `refresh`:
dumped (optionally masked by greenmask), restored locally, checked by a verify query and
locked as templates. Branches (<db>_<branch>) are file-level copies of the goldens.
Everything project-specific lives in pgsprout.toml, found by walking up from the cwd.
"""

import argparse
import difflib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

__version__ = "0.2.0"

CONFIG_NAME = "pgsprout.toml"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
NAME_RE = re.compile(r"^[a-z0-9_]+$")
TAG = "pgsprout"  # COMMENT ON DATABASE marker, so `list` only shows what we created
PG_MAX_NAME = 63

CONFIG_TEMPLATE = """\
# pgsprout: masked golden copies of Postgres, sprouted into local branches.
# Paths are relative to this file. [run.env] values may use {host} {port} {user} {branch}.

[local]                        # goldens and branches live here; only localhost is accepted
host = "localhost"
port = 5432
user = "postgres"

[storage]                      # where dumps live; every database gets its own sub-path
url = ".pgsprout/dumps"        # dir or file:///abs/dir (gitignore it), s3://bucket/prefix,
                               # or https://host/path (restore only: fetches <url>/<db>.tar.gz)
# region = "eu-central-1"      # s3 only; credentials come from the usual AWS env/profile
# endpoint = "http://localhost:9000"  # S3-compatible stores (MinIO, R2)
keep = 1                       # dumps kept per database after each run

[[databases]]
name = "app"                   # -> app_golden, app_<branch>
source = "app_production"      # database name on the source (PGHOST/PGUSER/PGPASSWORD)
# greenmask = ".pgsprout/greenmask-app.yml"   # full dump with masking rules
# verify = ".pgsprout/verify-app.sql"         # rows of (label, count); every count must be 0
# exclude_table_data = ["public.audit_*"]     # schema only for these tables

[run]
# wrapper = ["mise", "exec", "--"]  # [run.env] is injected after it, so the wrapper can't override it
migrate = [
  ["python", "manage.py", "migrate"],
]

[run.env]
DATABASE_URL = "postgres://{user}@{host}:{port}/app_{branch}"
"""


def die(msg: str):
    sys.exit(f"pgsprout: {msg}")


# -- config ------------------------------------------------------------------


def find_config(explicit: str | None) -> Path:
    given = explicit or os.environ.get("PGSPROUT_CONFIG")
    if given:
        path = Path(given).resolve()
        return path if path.is_file() else die(f"config not found: {path}")
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / CONFIG_NAME).is_file():
            return d / CONFIG_NAME
    die(f"no {CONFIG_NAME} in {Path.cwd()} or any parent (run `pgsprout init` or pass --config)")


def load_config(path: Path, storage_url: str | None = None) -> dict:
    """storage_url (--storage) beats $PGSPROUT_STORAGE_URL, which beats [storage] url."""
    raw = tomllib.loads(path.read_text())
    root = path.parent
    local = raw.get("local", {})
    if local.get("host", "localhost") not in LOCAL_HOSTS:
        die(f"[local] host must be one of {sorted(LOCAL_HOSTS)}: branches are never created remotely")
    dbs = raw.get("databases") or die("config needs at least one [[databases]] entry")
    for db in dbs:
        if not NAME_RE.match(db.get("name", "")) or not db.get("source"):
            die(f"each [[databases]] needs a [a-z0-9_] name and a source: {db}")
        for key in ("greenmask", "verify"):
            if key in db:
                db[key] = root / db[key]
    run = raw.get("run", {})
    storage = raw.get("storage", {})
    return {
        "root": root,
        "host": local.get("host", "localhost"),
        "port": int(local.get("port", 5432)),
        "user": local.get("user", "postgres"),
        "storage": {
            **parse_storage_url(
                storage_url or os.environ.get("PGSPROUT_STORAGE_URL") or storage.get("url", ".pgsprout/dumps"), root
            ),
            "region": storage.get("region"),
            "endpoint": storage.get("endpoint"),
        },
        "keep": int(storage.get("keep", 1)),
        "databases": dbs,
        "wrapper": run.get("wrapper", []),
        "migrate": run.get("migrate", []),
        "env": run.get("env", {}),
    }


def parse_storage_url(url: str, root: Path) -> dict:
    """-> {"kind": "dir" | "s3" | "https", "url": normalized}; plain paths are relative to root."""
    parsed = urlparse(url)
    if parsed.scheme == "s3":
        return {"kind": "s3", "url": url}
    if parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS):
        return {"kind": "https", "url": url.rstrip("/")}
    if parsed.scheme == "file":
        return {"kind": "dir", "url": parsed.path}
    if parsed.scheme:
        die(f"[storage] url must be a path, file://, s3:// or https:// (http only for localhost): {url}")
    return {"kind": "dir", "url": str(root / url)}


# -- helpers -----------------------------------------------------------------


def sanitize(name: str, max_len: int) -> str:
    """git branch -> Postgres-safe suffix: 'feature/ABC-12 x' -> 'feature_abc_12_x'."""
    clean = re.sub(r"[^a-z0-9]", "_", name.lower())[:max_len]
    if not NAME_RE.match(clean):
        die(f"invalid branch name: {name!r}")
    return clean


def branch_name(cfg: dict, arg: str | None) -> str:
    raw = arg or os.environ.get("PGSPROUT_BRANCH")
    if not raw:
        raw = subprocess.run(
            ["git", "branch", "--show-current"], cwd=cfg["root"], capture_output=True, text=True
        ).stdout.strip()
    longest = max(len(db["name"]) for db in cfg["databases"])
    return sanitize(raw or die("no branch name given and not on a git branch"), PG_MAX_NAME - longest - 1)


def render_env(cfg: dict, branch: str) -> dict:
    values = {"host": cfg["host"], "port": cfg["port"], "user": cfg["user"], "branch": branch}
    return {key: str(val).format(**values) for key, val in cfg["env"].items()}


def local_env() -> dict:
    """Environment for local Postgres calls: drop PG* so a source left in the shell can't leak in."""
    return {k: v for k, v in os.environ.items() if not k.startswith("PG")}


def local_conn(cfg: dict) -> list[str]:
    return ["-h", cfg["host"], "-p", str(cfg["port"]), "-U", cfg["user"]]


def storage_env(cfg: dict, db: str) -> dict:
    """greenmask reads STORAGE_* over its config file, so one [storage] serves every database."""
    storage = cfg["storage"]
    if storage["kind"] == "dir":
        path = Path(storage["url"]) / db
        path.mkdir(parents=True, exist_ok=True)
        return {"STORAGE_TYPE": "directory", "STORAGE_DIRECTORY_PATH": str(path)}
    bucket, _, prefix = storage["url"].removeprefix("s3://").partition("/")
    env = {"STORAGE_TYPE": "s3", "STORAGE_S3_BUCKET": bucket,
           "STORAGE_S3_PREFIX": f"{prefix.strip('/')}/{db}".lstrip("/")}
    if storage["region"]:
        env["STORAGE_S3_REGION"] = storage["region"]
    if storage["endpoint"]:
        env["STORAGE_S3_ENDPOINT"] = storage["endpoint"]
        env["STORAGE_S3_FORCE_PATH_STYLE"] = "true"
    return env


def fetch_dump(base_url: str, db: str, dest: Path) -> None:
    """Download <base_url>/<db>.tar.gz (made by `pgsprout pack`) and unpack it under dest."""
    url = f"{base_url}/{db}.tar.gz"
    archive = dest / f"{db}.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (urllib.error.URLError, TimeoutError) as err:
        die(f"cannot fetch {url}: {err}")
    with tarfile.open(archive) as tar:
        tar.extractall(dest / db, filter="data")  # refuses absolute paths and ../ escapes
    archive.unlink()
    if not any((dest / db).glob("*/toc.dat")):
        die(f"{url} is not a pgsprout pack (no <dump-id>/toc.dat inside)")


def latest_dump(path: Path) -> Path:
    """Newest completed greenmask dump in a directory storage (metadata.json is written last)."""
    done = [d for d in path.iterdir() if d.is_dir() and (d / "metadata.json").is_file()]
    return max(done, key=lambda d: int(d.name)) if done else die(f"no completed dump in {path}")


def greenmask(db: dict, *args) -> list:
    config = ["--config", db["greenmask"]] if "greenmask" in db else []
    return ["greenmask", *config, *args]


def sh(cmd: list, *, env: dict | None = None, cwd: Path | None = None) -> str:
    result = subprocess.run([str(c) for c in cmd], env=env, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"{shlex.join(map(str, cmd))} failed:\n{result.stderr.strip()}")
    return result.stdout


def psql(cfg: dict, sql: str, db: str = "postgres") -> str:
    return sh(["psql", *local_conn(cfg), "-d", db, "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql], env=local_env())


def failed_checks(verify_output: str) -> list[str]:
    """verify SQL prints `label|count` rows; any non-zero count fails the golden."""
    return [line for line in verify_output.splitlines() if line and int(line.rsplit("|", 1)[1]) > 0]


# -- commands ----------------------------------------------------------------


def cmd_init(_cfg, args) -> None:
    path = Path(args.config or CONFIG_NAME)
    if path.exists():
        die(f"{path} already exists")
    path.write_text(CONFIG_TEMPLATE)
    print(f"wrote {path}; add its dump dir to .gitignore")


def cmd_dump(cfg: dict, _args) -> None:
    """Source connection comes from PGHOST/PGUSER/PGPASSWORD in the shell."""
    if cfg["storage"]["kind"] == "https":
        die("https storage is read-only: dump to a directory or s3://, then `pgsprout pack` and upload")
    for db in cfg["databases"]:
        print(f"dump {db['source']} -> {db['name']}", flush=True)
        env = {**os.environ, **storage_env(cfg, db["name"])}
        excludes = [f"--exclude-table-data={t}" for t in db.get("exclude_table_data", [])]
        # conninfo form: a bare name would let greenmask's default --host override PGHOST
        sh(greenmask(db, "dump", "--dbname", f"dbname={db['source']}", *excludes), env=env)
        sh(greenmask(db, "delete", "--retain-recent", cfg["keep"], "--prune-failed"), env=env)
    print(f"dumps in {cfg['storage']['url']}")


def cmd_restore(cfg: dict, _args) -> None:
    with tempfile.TemporaryDirectory(prefix="pgsprout-") as tmp:
        restore_goldens(cfg, Path(tmp))
    verify_and_lock(cfg)


def restore_goldens(cfg: dict, tmp: Path) -> None:
    env = local_env()
    if cfg["storage"]["kind"] == "https":
        # fetch everything before touching any golden, so a failed download changes nothing
        for db in cfg["databases"]:
            print(f"fetch {db['name']}", flush=True)
            fetch_dump(cfg["storage"]["url"], db["name"], tmp)
    for db in cfg["databases"]:
        golden = f"{db['name']}_golden"
        print(f"restore {golden}", flush=True)
        if cfg["storage"]["kind"] == "https":
            storage = {**env, "STORAGE_TYPE": "directory", "STORAGE_DIRECTORY_PATH": str(tmp / db["name"])}
        else:
            storage = {**env, **storage_env(cfg, db["name"])}
        if psql(cfg, f"SELECT 1 FROM pg_database WHERE datname = '{golden}'"):
            psql(cfg, f'ALTER DATABASE "{golden}" IS_TEMPLATE false')
        psql(cfg, f'DROP DATABASE IF EXISTS "{golden}" WITH (FORCE)')
        psql(cfg, f'CREATE DATABASE "{golden}"')
        sh(greenmask(db, "restore", "latest", *local_conn(cfg), "--dbname", golden), env=storage)


def verify_and_lock(cfg: dict) -> None:
    env = local_env()
    # gate every golden before locking any: a failed check leaves them unlocked for inspection
    for db in cfg["databases"]:
        if "verify" in db:
            golden = f"{db['name']}_golden"
            out = sh(["psql", *local_conn(cfg), "-d", golden, "-v", "ON_ERROR_STOP=1",
                      "-At", "-F", "|", "-f", db["verify"]], env=env)
            if failed := failed_checks(out):
                die(f"{db['verify'].name} failed on {golden}:\n" + "\n".join(failed))

    for db in cfg["databases"]:
        golden = f"{db['name']}_golden"
        psql(cfg, f'ALTER DATABASE "{golden}" IS_TEMPLATE true ALLOW_CONNECTIONS false')
        psql(cfg, f"COMMENT ON DATABASE \"{golden}\" IS '{TAG}'")
    print("goldens ready")


def cmd_pack(cfg: dict, args) -> None:
    """Latest dump of each database -> <outdir>/<db>.tar.gz, ready to serve over https."""
    if cfg["storage"]["kind"] != "dir":
        die("pack reads a directory storage; point [storage] url at the dump directory")
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    for db in cfg["databases"]:
        dump = latest_dump(Path(cfg["storage"]["url"]) / db["name"])
        with tarfile.open(out / f"{db['name']}.tar.gz", "w:gz") as tar:
            tar.add(dump, arcname=dump.name)
        print(f"{db['name']}: {out / (db['name'] + '.tar.gz')} (dump {dump.name})")


def cmd_refresh(cfg: dict, args) -> None:
    cmd_dump(cfg, args)
    cmd_restore(cfg, args)


def branch_dbs(cfg: dict, args, *, drop: bool, create: bool) -> None:
    branch = branch_name(cfg, args.name)
    for db in cfg["databases"]:
        target = f"{db['name']}_{branch}"
        if drop:
            psql(cfg, f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
        if create:
            psql(cfg, f'CREATE DATABASE "{target}" TEMPLATE "{db["name"]}_golden" STRATEGY FILE_COPY')
            psql(cfg, f"COMMENT ON DATABASE \"{target}\" IS '{TAG}'")
    print(f"{args.command}: {', '.join(db['name'] + '_' + branch for db in cfg['databases'])}")


def run_in_branch(cfg: dict, branch: str, command: list[str]) -> int:
    assignments = [f"{k}={v}" for k, v in render_env(cfg, branch).items()]
    # env is injected after the wrapper (e.g. `mise exec --`), which may itself load an env file
    return subprocess.run([*cfg["wrapper"], "env", *assignments, *command], cwd=cfg["root"]).returncode


def cmd_migrate(cfg: dict, args) -> None:
    branch = branch_name(cfg, args.name)
    for command in cfg["migrate"] or die("no [run] migrate commands in config"):
        if code := run_in_branch(cfg, branch, command):
            sys.exit(code)


def cmd_exec(cfg: dict, args) -> None:
    command = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not command:
        die("usage: pgsprout exec [-b name] -- cmd...")
    sys.exit(run_in_branch(cfg, branch_name(cfg, args.branch), command))


def cmd_env(cfg: dict, args) -> None:
    for key, val in render_env(cfg, branch_name(cfg, args.name)).items():
        print(f"export {key}={shlex.quote(val)}")


def normalize_schema(dump: str) -> list[str]:
    """pg_dump --schema-only minus what changes between identical schemas (comments, SETs, restrict keys)."""
    noise = ("--", "SET ", "SELECT pg_catalog.set_config", "\\restrict", "\\unrestrict")
    return [line for line in dump.splitlines() if line.strip() and not line.startswith(noise)]


def schema_of(cfg: dict, database: str) -> list[str]:
    dump = ["pg_dump", *local_conn(cfg), "-d", database, "--schema-only", "--no-owner", "--no-privileges"]
    if not database.endswith("_golden"):
        return normalize_schema(sh(dump, env=local_env()))
    # goldens refuse connections so nothing can hold them open; lift that just for the dump
    psql(cfg, f'ALTER DATABASE "{database}" ALLOW_CONNECTIONS true')
    try:
        return normalize_schema(sh(dump, env=local_env()))
    finally:
        psql(cfg, f'ALTER DATABASE "{database}" ALLOW_CONNECTIONS false')


def cmd_diff(cfg: dict, args) -> None:
    """Schema diff between two branches ("golden" = the goldens); exits 1 when they differ."""
    old = "golden" if args.base == "golden" else branch_name(cfg, args.base)
    new = branch_name(cfg, args.target)
    changed = False
    for db in cfg["databases"]:
        a, b = f"{db['name']}_{old}", f"{db['name']}_{new}"
        lines = list(difflib.unified_diff(schema_of(cfg, a), schema_of(cfg, b), a, b, lineterm="", n=2))
        changed |= bool(lines)
        print("\n".join(lines) if lines else f"{a} -> {b}: no schema changes")
    sys.exit(1 if changed else 0)


def cmd_list(cfg: dict, _args) -> None:
    rows = psql(cfg, "SELECT rpad(datname, 50) || pg_size_pretty(pg_database_size(datname)) FROM pg_database "
                     f"WHERE shobj_description(oid, 'pg_database') = '{TAG}' ORDER BY datname")
    print(rows or f"no goldens or branches on {cfg['host']}:{cfg['port']}; run `pgsprout refresh` first", end="\n" if not rows else "")


COMMANDS = {
    "init": cmd_init,
    "refresh": cmd_refresh,
    "dump": cmd_dump,
    "restore": cmd_restore,
    "pack": cmd_pack,
    "create": lambda cfg, a: branch_dbs(cfg, a, drop=False, create=True),
    "drop": lambda cfg, a: branch_dbs(cfg, a, drop=True, create=False),
    "reset": lambda cfg, a: branch_dbs(cfg, a, drop=True, create=True),
    "migrate": cmd_migrate,
    "env": cmd_env,
    "exec": cmd_exec,
    "diff": cmd_diff,
    "list": cmd_list,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="pgsprout", description=__doc__.splitlines()[0])
    parser.add_argument("--config", help=f"path to {CONFIG_NAME} (default: search upwards from cwd)")
    parser.add_argument("--storage", help="override [storage] url, e.g. s3://bucket/prefix (or $PGSPROUT_STORAGE_URL)")
    parser.add_argument("--version", action="version", version=f"pgsprout {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help=f"write a commented {CONFIG_NAME} template")
    sub.add_parser("refresh", help="dump + restore")
    sub.add_parser("dump", help="source DBs (PGHOST/PGUSER/PGPASSWORD) -> [storage], masked")
    sub.add_parser("restore", help="[storage] -> <db>_golden, verified, then locked as templates")
    sub.add_parser("pack", help="latest dumps -> <outdir>/<db>.tar.gz for an https [storage]").add_argument("outdir")
    for name, text in [("create", "clone goldens into a branch"), ("drop", "drop a branch"),
                       ("reset", "drop + create"), ("migrate", "run [run] migrate commands on a branch"),
                       ("env", "print the branch's [run.env] as shell exports")]:
        sub.add_parser(name, help=f"{text} (name defaults to the git branch)").add_argument("name", nargs="?")
    ex = sub.add_parser("exec", help="run a command with the branch's [run.env]")
    ex.add_argument("-b", "--branch")
    ex.add_argument("cmd", nargs=argparse.REMAINDER)
    diff = sub.add_parser("diff", help="schema diff: golden (or a branch) -> a branch; exit 1 if different")
    diff.add_argument("target", nargs="?", help="branch to inspect (default: the git branch)")
    diff.add_argument("--base", default="golden", help="branch to compare against (default: golden)")
    sub.add_parser("list", help="goldens and branches with sizes")

    args = parser.parse_args()
    cfg = None if args.command == "init" else load_config(find_config(args.config), args.storage)
    COMMANDS[args.command](cfg, args)


if __name__ == "__main__":
    main()
