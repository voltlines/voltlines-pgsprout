# pgsprout

Masked golden copies of Postgres, sprouted into local branches in seconds.

Test a migration against realistic data, break it, reset, try again — without waiting
for a restore and without real customer data on your laptop.

```bash
pgsprout refresh          # staging/replica -> masked, verified, locked local goldens
git switch feature/x
pgsprout create           # db1_feature_x, db2_feature_x, ... (file-level copies)
pgsprout migrate          # your migrate commands, pointed at the branch
pgsprout reset            # broke it? back to golden in seconds
pgsprout drop
```

## How it works

1. **dump** — each source database is dumped with
   [greenmask](https://github.com/GreenmaskIO/greenmask), with masking rules where configured,
   into `[storage]`: a local directory or an S3 bucket. The source connection comes from
   `PGHOST`/`PGUSER`/`PGPASSWORD`.
2. **restore** — the latest dump of each database is pulled from `[storage]` and restored
   locally as `<db>_golden`. No source access needed.
3. **verify** — your verify query runs on each golden; any non-zero check leaves the
   goldens unlocked and stops. This is the gate that keeps unmasked data out.
4. **lock** — goldens become templates with connections disabled.
5. **create** — a branch is `CREATE DATABASE <db>_<branch> TEMPLATE <db>_golden STRATEGY FILE_COPY`
   for every database in the set: a file copy, not a restore.

## What it takes care of

- **Several databases as one branch**, created, reset and dropped together.
- **Production-shaped data, safely**: masking via greenmask, and a verify gate that must pass
  before a golden can be used.
- **Branches side by side**: `db1_feature_a` and `db1_feature_b` coexist, so two worktrees can
  test at the same time; `migrate`/`exec` point each command at the right one.
- **Guard rails**: branches are only ever created on `localhost`; source `PG*` variables are
  stripped from every local call, so a staging `PGHOST` left in your shell can't redirect a
  `DROP DATABASE`.
- **Env injection** *after* wrappers like `mise exec` that would otherwise reload an env file
  over your per-branch variables.

## Install

```bash
brew install ubxt/tap/pgsprout      # pulls greenmask and libpq too
# or
uv tool install pgsprout             # needs greenmask and psql/pg_dump on PATH
```

Requires Python 3.12+ and nothing else from PyPI.

## Storage

`[storage] url` decides where dumps are written and read from. Every database gets its own
sub-path.

| url | dump | restore |
|---|---|---|
| `path` or `file:///abs/path` | yes | yes |
| `s3://bucket/prefix` (+ `region`, optional `endpoint` for MinIO/R2) | yes | yes |
| `https://host/path` | no, read-only | fetches `<url>/<db>.tar.gz` |

For https, dump to a directory, then `pgsprout pack <outdir>` writes the latest dump of each
database as `<db>.tar.gz`; upload those anywhere static (a web server, a release, an artifact
store). All archives are downloaded before any golden is touched, and archives that try to
escape their directory are refused. Plain `http://` is accepted only for `localhost`.

## Shared goldens

Point `[storage]` at a bucket and the dump can run once, somewhere with access to the source
(a CronJob next to a read replica), while everyone else only restores:

```toml
[storage]
url = "s3://my-goldens/app"      # each database lands under app/<name>/
region = "eu-central-1"          # credentials: the usual AWS env vars / profile
# endpoint = "http://localhost:9000"   # MinIO, R2 and other S3-compatible stores
keep = 1                         # older dumps are pruned after every dump
```

```bash
pgsprout dump        # in the job: source -> masked dumps in S3
pgsprout restore     # on a laptop: S3 -> verified, locked goldens
```

Only masked dumps ever reach the bucket, but treat it as sensitive anyway: private, encrypted,
and readable only by the people who restore from it.

## Configure

```bash
pgsprout init        # writes a commented pgsprout.toml
```

`pgsprout.toml` is found by walking up from the current directory. Paths in it are relative
to the file. A full two-database example lives in [`examples/multi-db`](examples/multi-db).

```toml
[local]                        # only localhost is accepted
port = 5432

[storage]
url = ".pgsprout/dumps"        # or s3://bucket/prefix

[[databases]]
name = "db1"                   # -> db1_golden, db1_<branch>
source = "app"
greenmask = ".pgsprout/greenmask-db1.yml"
verify = ".pgsprout/verify-db1.sql"

[[databases]]
name = "db2"
source = "app_events"
exclude_table_data = ["public.event_log_*"]    # schema only

[run]
wrapper = ["mise", "exec", "--"]               # optional
migrate = [["python", "manage.py", "migrate"]]

[run.env]                                      # {host} {port} {user} {branch}
DB1_URL = "postgres://{user}@{host}:{port}/db1_{branch}"
```

A verify query returns one `label | count` row per check; every count must be 0:

```sql
SELECT 'real-looking email', count(*) FROM users WHERE email NOT LIKE '%@example.invalid'
UNION ALL
SELECT 'live tokens', count(*) FROM api_tokens;
```

## Commands

| Command | |
|---|---|
| `init` | write a commented `pgsprout.toml` |
| `refresh` | `dump` + `restore` |
| `dump` | source databases -> `[storage]` (masked where configured) |
| `restore` | `[storage]` -> goldens, verified, then locked |
| `pack <outdir>` | latest dumps -> `<db>.tar.gz`, for an https `[storage]` |
| `create` / `drop` / `reset` `[name]` | manage a branch; name defaults to the current git branch |
| `migrate [name]` | run `[run] migrate` commands against the branch |
| `exec [-b name] -- cmd...` | run any command with the branch's `[run.env]` |
| `env [name]` | print `[run.env]` as shell exports |
| `diff [branch] [--base b]` | schema diff from the goldens (or `b`) to a branch; exit 1 if different |
| `list` | goldens and branches with their sizes |

## Masking tips

- Rebuild contact data from the row id on **reserved** values (`user42@example.invalid`,
  a number range no carrier assigns) instead of random ones: a random email or phone can
  belong to a real person.
- Generated columns (`GENERATED ALWAYS AS`) can't be written; mask their inputs and Postgres
  recomputes them on restore.
- With multi-table inheritance, the columns live in the parent table — mask them there.
- Exclude the data of token, session and message-log tables entirely.

## Disk

On Postgres 17 and earlier each branch is a full copy of its golden. Drop branches you're
done with; `pgsprout list` shows what is taking space.

## Related projects

- [pgbranch](https://github.com/le-vlad/pgbranch) — git-style branching of a local Postgres
  database, with schema diff/merge, git hooks and remotes.
- [greenmask](https://github.com/GreenmaskIO/greenmask) — dump, masking and restore; pgsprout
  drives it for every dump.
- [Database Lab Engine](https://github.com/postgres-ai/database-lab-engine) — thin clones of
  large databases on ZFS, for teams sharing a server.
- [Neon](https://github.com/neondatabase/neon) — serverless Postgres with copy-on-write branches.

## License

MIT
