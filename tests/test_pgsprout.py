import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pgsprout  # noqa: E402


def write_config(text: str) -> Path:
    path = Path(tempfile.mkdtemp()) / pgsprout.CONFIG_NAME
    path.write_text(text)
    return path


MINIMAL = '[[databases]]\nname = "app"\nsource = "app_prod"\n'


class SanitizeTest(unittest.TestCase):
    def test_git_branch_becomes_postgres_suffix(self):
        self.assertEqual(pgsprout.sanitize("feature/ABC-12 Test", 40), "feature_abc_12_test")

    def test_truncates_to_postgres_limit(self):
        self.assertEqual(pgsprout.sanitize("x" * 80, 40), "x" * 40)


class ConfigTest(unittest.TestCase):
    def test_remote_local_host_is_rejected(self):
        with self.assertRaises(SystemExit):
            pgsprout.load_config(write_config('[local]\nhost = "db.example.com"\n' + MINIMAL))

    def test_database_needs_source(self):
        with self.assertRaises(SystemExit):
            pgsprout.load_config(write_config('[[databases]]\nname = "app"\n'))

    def test_paths_are_relative_to_config(self):
        path = write_config(MINIMAL.replace('source = "app_prod"', 'source = "p"\nverify = "v.sql"'))
        cfg = pgsprout.load_config(path)
        self.assertEqual(cfg["databases"][0]["verify"], path.parent / "v.sql")
        self.assertEqual(cfg["storage"]["url"], str(path.parent / ".pgsprout/dumps"))

    def test_init_template_is_a_valid_config(self):
        pgsprout.load_config(write_config(pgsprout.CONFIG_TEMPLATE))


class EnvTest(unittest.TestCase):
    def test_render_env_fills_placeholders(self):
        cfg = {"host": "localhost", "port": 5433, "user": "postgres",
               "env": {"DB": "core_{branch}", "PORT": "{port}"}}
        self.assertEqual(pgsprout.render_env(cfg, "try1"), {"DB": "core_try1", "PORT": "5433"})

    def test_local_env_drops_source_connection(self):
        os.environ["PGHOST"] = "prod.example.com"
        try:
            self.assertNotIn("PGHOST", pgsprout.local_env())
        finally:
            del os.environ["PGHOST"]

    def test_env_is_injected_after_wrapper(self):
        cfg = {"root": Path.cwd(), "host": "localhost", "port": 5432, "user": "u",
               "wrapper": ["env", "X=from_wrapper"], "env": {"X": "from_config"}}
        self.assertEqual(pgsprout.run_in_branch(cfg, "b", ["sh", "-c", 'test "$X" = from_config']), 0)


class StorageTest(unittest.TestCase):
    def cfg(self, url, **storage):
        parsed = pgsprout.parse_storage_url(url, Path("/"))
        return {"storage": {"region": None, "endpoint": None, **parsed, **storage}}

    def test_local_dir_gets_one_subdir_per_database(self):
        root = tempfile.mkdtemp()
        env = pgsprout.storage_env(self.cfg(url=root), "db1")
        self.assertEqual(env, {"STORAGE_TYPE": "directory", "STORAGE_DIRECTORY_PATH": f"{root}/db1"})
        self.assertTrue(Path(root, "db1").is_dir())

    def test_s3_url_splits_into_bucket_and_per_database_prefix(self):
        env = pgsprout.storage_env(self.cfg(url="s3://goldens/team/app/", region="eu-central-1"), "db1")
        self.assertEqual(env, {"STORAGE_TYPE": "s3", "STORAGE_S3_BUCKET": "goldens",
                               "STORAGE_S3_PREFIX": "team/app/db1", "STORAGE_S3_REGION": "eu-central-1"})

    def test_s3_bucket_root_and_custom_endpoint(self):
        env = pgsprout.storage_env(self.cfg(url="s3://goldens", endpoint="http://localhost:9000"), "db1")
        self.assertEqual(env["STORAGE_S3_PREFIX"], "db1")
        self.assertEqual(env["STORAGE_S3_ENDPOINT"], "http://localhost:9000")
        self.assertEqual(env["STORAGE_S3_FORCE_PATH_STYLE"], "true")


class StorageUrlTest(unittest.TestCase):
    root = Path("/repo")

    def kind(self, url):
        return pgsprout.parse_storage_url(url, self.root)

    def test_schemes(self):
        self.assertEqual(self.kind("dumps"), {"kind": "dir", "url": "/repo/dumps"})
        self.assertEqual(self.kind("file:///data/dumps"), {"kind": "dir", "url": "/data/dumps"})
        self.assertEqual(self.kind("s3://b/p")["kind"], "s3")
        self.assertEqual(self.kind("https://cdn.example.com/g/"), {"kind": "https", "url": "https://cdn.example.com/g"})
        self.assertEqual(self.kind("http://localhost:8000/g")["kind"], "https")

    def test_plain_http_to_a_remote_host_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.kind("http://cdn.example.com/g")

    def test_unknown_scheme_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.kind("ftp://example.com/g")


class PackFetchTest(unittest.TestCase):
    def fake_dump(self, storage: Path, db: str, dump_id: str, done: bool = True) -> None:
        d = storage / db / dump_id
        d.mkdir(parents=True)
        (d / "toc.dat").write_text("toc")
        if done:
            (d / "metadata.json").write_text("{}")

    def test_latest_dump_skips_incomplete(self):
        storage = Path(tempfile.mkdtemp())
        self.fake_dump(storage, "db1", "100")
        self.fake_dump(storage, "db1", "200", done=False)
        self.assertEqual(pgsprout.latest_dump(storage / "db1").name, "100")

    def test_pack_then_fetch_over_http(self):
        import functools
        import threading
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        storage, out, dest = (Path(tempfile.mkdtemp()) for _ in range(3))
        self.fake_dump(storage, "db1", "100")
        cfg = {"storage": {"kind": "dir", "url": str(storage)}, "databases": [{"name": "db1"}]}
        pgsprout.cmd_pack(cfg, type("Args", (), {"outdir": str(out)}))

        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(out))
        server = ThreadingHTTPServer(("localhost", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            pgsprout.fetch_dump(f"http://localhost:{server.server_port}", "db1", dest)
        finally:
            server.shutdown()
        self.assertEqual((dest / "db1" / "100" / "toc.dat").read_text(), "toc")

    def test_fetch_rejects_archive_escaping_its_directory(self):
        import io
        src, dest = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        with tarfile.open(src / "db1.tar.gz", "w:gz") as tar:
            info = tarfile.TarInfo("../evil")
            tar.addfile(info, io.BytesIO(b""))
        with self.assertRaises(tarfile.TarError):
            pgsprout.fetch_dump(src.as_uri(), "db1", dest)


class DiffTest(unittest.TestCase):
    def test_normalize_drops_noise_between_identical_schemas(self):
        dump = "--\n-- PostgreSQL dump\n\\restrict abc123\nSET lock_timeout = 0;\n" \
               "SELECT pg_catalog.set_config('search_path', '', false);\n\nCREATE TABLE t (id int);\n\\unrestrict abc123\n"
        self.assertEqual(pgsprout.normalize_schema(dump), ["CREATE TABLE t (id int);"])


class VerifyTest(unittest.TestCase):
    def test_non_zero_counts_fail(self):
        out = "real email|0\npush token|3\n"
        self.assertEqual(pgsprout.failed_checks(out), ["push token|3"])

    def test_all_zero_passes(self):
        self.assertEqual(pgsprout.failed_checks("a|0\nb|0\n"), [])


if __name__ == "__main__":
    unittest.main()
