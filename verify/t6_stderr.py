"""D1 CLASSIFIER: collect REAL tmux stderr, then classify each with the shipped code."""
import os as _os
import sys as _sys


def _add_repo_root() -> None:
    """Put THIS worktree's repo root on sys.path. Inputs: none. Output: None."""
    _sys.path.insert(
        0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import os
import shutil
import subprocess
import sys
import tempfile

_add_repo_root()
from src.core.tmux_listing import classify_tmux_stderr, classify_listing_failure

TMUX = "/opt/homebrew/bin/tmux"
BASE = tempfile.mkdtemp(prefix="s4verify.")


def run(*args):
    r = subprocess.run([TMUX, *args], capture_output=True, text=True)
    return r.returncode, (r.stderr or "").strip()


def report(label, rc, err):
    v = classify_tmux_stderr(err)
    listing = classify_listing_failure(rc, err)
    flag = ""
    if v == "no_server" and "denied" in err.lower():
        flag = "   <<< MISCLASSIFIED: could-not-look reported as a real zero"
    print(f"  {label}")
    print(f"      stderr : {err!r}")
    print(f"      verdict: {v:16} ok={listing.ok} reason={listing.reason}{flag}")


print("=== REAL tmux stderr, classified by the shipped code ===")

# 1. socket path that never existed
rc, err = run("-S", os.path.join(BASE, "never-existed"), "list-sessions")
report("1. never-existed socket", rc, err)

# 2. stale socket: server SIGKILLed, socket file left behind
sock = os.path.join(BASE, "stale")
run("-S", sock, "new-session", "-d", "-s", "x", "sleep 300")
pid = subprocess.run([TMUX, "-S", sock, "display-message", "-p", "#{pid}"],
                     capture_output=True, text=True).stdout.strip()
if pid:
    subprocess.run(["kill", "-9", pid], capture_output=True)
subprocess.run(["sleep", "1"])
print(f"  (socket file still on disk: {os.path.exists(sock)})")
rc, err = run("-S", sock, "list-sessions")
report("2. stale socket after kill -9", rc, err)

# 3. permission denied on the containing directory
d = os.path.join(BASE, "noperm")
os.makedirs(d, exist_ok=True)
run("-S", os.path.join(d, "sock"), "new-session", "-d", "-s", "y", "sleep 300")
os.chmod(d, 0o000)
rc, err = run("-S", os.path.join(d, "sock"), "list-sessions")
report("3. permission denied", rc, err)
os.chmod(d, 0o755)
run("-S", os.path.join(d, "sock"), "kill-server")

# 4. a regular file where a socket should be
nf = os.path.join(BASE, "notasocket")
open(nf, "w").write("hi")
rc, err = run("-S", nf, "list-sessions")
report("4. non-socket file", rc, err)

# 5. THE BYPASS: socket path containing the literal marker text.
#    tmux_socket_name is user-configurable (config.json session.tmux_socket_name)
#    and is echoed verbatim into stderr.
d2 = os.path.join(BASE, "no server running")
os.makedirs(d2, exist_ok=True)
run("-S", os.path.join(d2, "sock"), "new-session", "-d", "-s", "z", "sleep 300")
os.chmod(d2, 0o000)
rc, err = run("-S", os.path.join(d2, "sock"), "list-sessions")
report("5. path contains 'no server running' + REAL permission denied", rc, err)
os.chmod(d2, 0o755)
run("-S", os.path.join(d2, "sock"), "kill-server")

# 6. path containing parentheses (anchoring check)
d3 = os.path.join(BASE, "dir(Permission denied)")
os.makedirs(d3, exist_ok=True)
rc, err = run("-S", os.path.join(d3, "nope"), "list-sessions")
report("6. path with parens, real errno ENOENT", rc, err)

print("\n=== SYNTHETIC: strings the shipped code has to survive ===")
for label, s in [
    ("trailing CR (windows/pty)", "error connecting to /tmp/x (No such file or directory)\r"),
    ("trailing newline", "error connecting to /tmp/x (No such file or directory)\n"),
    ("trailing spaces", "error connecting to /tmp/x (No such file or directory)   "),
    ("multi-line, error first", "error connecting to /tmp/x (No such file or directory)\nwarning: blah"),
    ("multi-line, error last", "warning: blah\nerror connecting to /tmp/x (No such file or directory)"),
    ("French strerror (glibc)", "error connecting to /tmp/x (Aucun fichier ou dossier de ce type)"),
    ("German strerror (glibc)", "error connecting to /tmp/x (Datei oder Verzeichnis nicht gefunden)"),
    ("capitalised differently", "error connecting to /tmp/x (NO SUCH FILE OR DIRECTORY)"),
    ("errno with trailing dot", "error connecting to /tmp/x (No such file or directory.)"),
    ("empty stderr, rc=1", ""),
]:
    v = classify_tmux_stderr(s)
    listing = classify_listing_failure(1, s)
    print(f"  {label:28} -> {v:16} ok={listing.ok}")

shutil.rmtree(BASE, ignore_errors=True)
