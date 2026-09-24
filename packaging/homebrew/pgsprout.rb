# Copy to <owner>/homebrew-tap as Formula/pgsprout.rb after tagging a release.
# sha256: curl -sL <url> | shasum -a 256
class Pgsprout < Formula
  desc "Masked golden copies of Postgres, sprouted into local branches in seconds"
  homepage "https://github.com/ubxt/pgsprout"
  url "https://github.com/ubxt/pgsprout/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_SHA256"
  license "MIT"

  depends_on "greenmask"
  depends_on "libpq"
  depends_on "python@3.13"

  def install
    libexec.install "pgsprout.py"
    # libpq is keg-only; append it so a user's own psql/pg_dump (e.g. Postgres.app) still wins
    (bin/"pgsprout").write <<~SH
      #!/bin/bash
      export PATH="$PATH:#{Formula["libpq"].opt_bin}"
      exec "#{Formula["python@3.13"].opt_bin}/python3.13" "#{libexec}/pgsprout.py" "$@"
    SH
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/pgsprout --version")
    system bin/"pgsprout", "init"
    assert_path_exists testpath/"pgsprout.toml"
  end
end
