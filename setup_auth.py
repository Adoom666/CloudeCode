#!/usr/bin/env python3
"""Setup script for Cloude Code authentication.

Creates the config file and ensures TOTP + JWT secrets exist.

This script is a CONVERGENCE operation, not a creation operation. Running
it a second time keeps every secret that is already set and mints only what
is genuinely missing. It used to overwrite both unconditionally, which
silently orphaned the user's paired authenticator and locked him out of his
own install with no recovery path.

Subcommands:
- (no args): full interactive setup. Existing secrets are preserved.
- ``--rotate-topic``: regenerate the ntfy push topic, write it to
  config.json, print the new ntfy URL, and exit. Used after a
  suspected topic leak or as a periodic hygiene rotation.
- ``--rotate-totp``: DESTRUCTIVE. Replace TOTP_SECRET, clear the
  ``.totp_paired`` marker, and print a fresh QR to re-pair with.
- ``--rotate-jwt``: DESTRUCTIVE. Replace JWT_SECRET, signing out every
  logged-in session.
Both rotation flags require a typed confirmation unless ``--yes`` is given.
"""
import argparse
import json
import secrets
import socket
import subprocess
import sys
from pathlib import Path


def check_and_setup_venv():
    """Ensure venv exists and has required packages."""
    # Get venv path
    project_root = Path(__file__).parent
    venv_path = project_root / "venv"
    venv_python = venv_path / "bin" / "python3"

    # Check if venv exists
    if not venv_python.exists():
        print("❌ venv not found at: venv/")
        print("Please create it first:")
        print("  python3 -m venv venv")
        sys.exit(1)

    # Check if dependencies are installed
    try:
        import pyotp
        import qrcode
        import jwt
        print("✅ All dependencies available\n")
        return
    except ImportError as e:
        missing_module = str(e).split("'")[1] if "'" in str(e) else "unknown"
        print(f"📦 Missing dependency: {missing_module}")
        print("Installing auth dependencies in venv...\n")

    # Install dependencies
    try:
        result = subprocess.run([
            str(venv_python), "-m", "pip", "install", "-q",
            "pyotp", "qrcode", "pillow", "pyjwt"
        ], check=True, capture_output=True, text=True)

        print("✅ Dependencies installed successfully\n")

        # Re-exec with venv python
        print("🔄 Re-running with venv python...\n")
        subprocess.run([str(venv_python), __file__] + sys.argv[1:])
        sys.exit(0)

    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install dependencies: {e}")
        print(f"Error output: {e.stderr}")
        sys.exit(1)


def generate_totp_secret():
    """Generate a random TOTP secret."""
    import pyotp
    return pyotp.random_base32()


def generate_jwt_secret():
    """Generate a random JWT secret."""
    return secrets.token_urlsafe(32)


#: The two .env keys that hold irreplaceable credential material. Losing
#: TOTP_SECRET orphans the user's authenticator; losing JWT_SECRET
#: invalidates every issued session token at once.
AUTH_SECRET_KEYS = ("TOTP_SECRET", "JWT_SECRET")

#: Marker written by src/api/auth.py once an authenticator has successfully
#: verified. Its presence makes GET /auth/qr return 403, so a sentinel left
#: standing over a REPLACED secret is an unrecoverable lockout: the old
#: authenticator no longer matches and the QR needed to pair a new one is
#: refused. Kept as a constant so the two writers agree on the name.
PAIRED_SENTINEL_NAME = ".totp_paired"


def read_env_value(env_path: Path, key: str):
    """Read one KEY=value out of a .env file without interpreting it.

    Args:
        env_path: Path to the .env file. May not exist.
        key: The variable name to look for.

    Returns:
        str | None: The stripped value when the key is present and
        non-blank, otherwise None. A present-but-empty line reads as None
        because an unset secret is not a secret worth preserving.

    Example:
        >>> read_env_value(Path(".env"), "TOTP_SECRET") is None
        True
    """
    if not env_path.exists():
        return None
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def capture_auth_secrets(env_path: Path) -> dict:
    """Snapshot the existing auth secrets BEFORE anything rewrites .env.

    ``setup_env_file`` rebuilds .env wholesale from .env.example, which
    blanks both secret lines. Any preservation guard that reads the file
    after that point sees an empty value and mints a replacement, so the
    snapshot has to be taken first.

    Args:
        env_path: Path to the .env file. May not exist.

    Returns:
        dict: Mapping of each key in ``AUTH_SECRET_KEYS`` to its existing
        value or None. Values are never logged or printed.
    """
    return {key: read_env_value(env_path, key) for key in AUTH_SECRET_KEYS}


def paired_sentinel_path(env_path: Path) -> Path:
    """Resolve where src/api/auth.py writes its TOTP-pairing sentinel.

    The server anchors the sentinel to the parent directory of
    ``AUTH_CONFIG_FILE``, so this reads the same setting rather than
    assuming the project root (hazard: an assumed value is the same defect
    as an unread setting).

    Args:
        env_path: Path to the .env file whose AUTH_CONFIG_FILE is read.

    Returns:
        Path: The sentinel path. Falls back to ``config.json`` beside the
        .env when AUTH_CONFIG_FILE is unset.
    """
    configured = read_env_value(env_path, "AUTH_CONFIG_FILE") or "./config.json"
    config_path = Path(configured).expanduser()
    if not config_path.is_absolute():
        config_path = (env_path.parent / config_path).resolve()
    return config_path.parent / PAIRED_SENTINEL_NAME


def apply_auth_secrets(
    env_path: Path,
    captured: dict,
    *,
    rotate_totp: bool = False,
    rotate_jwt: bool = False,
) -> dict:
    """Converge .env onto a correct set of auth secrets.

    Create-if-absent is the default. An existing secret is kept unless the
    caller explicitly asked to rotate it, because a setup script is a
    convergence operation, not a creation operation.

    When the TOTP secret is replaced (minted fresh or rotated) the
    ``.totp_paired`` sentinel is removed, because it asserts that an
    authenticator is paired to a secret that no longer exists. Leaving it
    would make /auth/qr refuse to serve the QR needed to recover.

    Args:
        env_path: Path to the .env file to write.
        captured: The snapshot from ``capture_auth_secrets``, taken before
            any template rewrite.
        rotate_totp: Replace an existing TOTP secret. Invalidates the
            currently paired authenticator.
        rotate_jwt: Replace an existing JWT secret. Signs out every session.

    Returns:
        dict: ``totp_action`` / ``jwt_action`` each one of "kept",
        "created" or "rotated"; ``totp_secret`` the effective TOTP secret;
        ``show_qr`` True whenever the TOTP secret changed; ``sentinel_cleared``
        True when a stale pairing marker was removed.
    """
    existing_totp = captured.get("TOTP_SECRET")
    existing_jwt = captured.get("JWT_SECRET")

    if existing_totp and not rotate_totp:
        totp_secret, totp_action = existing_totp, "kept"
    else:
        totp_secret = generate_totp_secret()
        totp_action = "rotated" if existing_totp else "created"

    if existing_jwt and not rotate_jwt:
        jwt_secret, jwt_action = existing_jwt, "kept"
    else:
        jwt_secret = generate_jwt_secret()
        jwt_action = "rotated" if existing_jwt else "created"

    update_env_file(
        env_path,
        totp_secret,
        jwt_secret,
        overwrite_totp=True,
        overwrite_jwt=True,
    )

    sentinel_cleared = False
    if totp_action != "kept":
        sentinel = paired_sentinel_path(env_path)
        try:
            if sentinel.exists():
                sentinel.unlink()
                sentinel_cleared = True
        except OSError as e:
            # Not fatal, but the user must know: a surviving sentinel over a
            # new secret is exactly the lockout this function exists to end.
            print(f"WARNING: could not clear {sentinel}: {e}")
            print("         Delete it by hand or /auth/qr will refuse to re-pair.")

    return {
        "totp_action": totp_action,
        "jwt_action": jwt_action,
        "totp_secret": totp_secret,
        "show_qr": totp_action != "kept",
        "sentinel_cleared": sentinel_cleared,
    }


def update_env_file(
    env_path: Path,
    totp_secret: str,
    jwt_secret: str,
    *,
    overwrite_totp: bool = False,
    overwrite_jwt: bool = False,
):
    """Write auth secrets into .env, preserving existing ones by default.

    Args:
        env_path: Path to the .env file. Created from .env.example if absent.
        totp_secret: The TOTP secret to write when none is already set.
        jwt_secret: The JWT secret to write when none is already set.
        overwrite_totp: Replace a non-empty existing TOTP_SECRET. Only the
            deliberate rotation path passes True.
        overwrite_jwt: Replace a non-empty existing JWT_SECRET.

    Returns:
        None. The file is written with mode 0600.
    """
    # Read existing .env or create from .env.example
    if env_path.exists():
        with open(env_path) as f:
            lines = f.readlines()
    else:
        # Start from .env.example
        example_path = env_path.parent / ".env.example"
        if example_path.exists():
            with open(example_path) as f:
                lines = f.readlines()
        else:
            lines = []

    # Update or add TOTP_SECRET and JWT_SECRET
    totp_found = False
    jwt_found = False

    for i, line in enumerate(lines):
        if line.startswith("TOTP_SECRET="):
            totp_found = True
            # An existing secret is paired to a real authenticator. Replacing
            # it without being asked is a silent, unrecoverable lockout.
            existing = line.split("=", 1)[1].strip()
            if not existing or overwrite_totp:
                lines[i] = f"TOTP_SECRET={totp_secret}\n"
        elif line.startswith("JWT_SECRET="):
            jwt_found = True
            # Replacing this invalidates every issued session token at once.
            existing = line.split("=", 1)[1].strip()
            if not existing or overwrite_jwt:
                lines[i] = f"JWT_SECRET={jwt_secret}\n"

    # Add if not found
    if not totp_found:
        # Find a good place to insert (after # Authentication Secrets comment if it exists)
        insert_index = len(lines)
        for i, line in enumerate(lines):
            if "# Authentication Secrets" in line:
                insert_index = i + 2  # After comment and blank TOTP_SECRET=
                break

        if insert_index >= len(lines):
            lines.append(f"\n# Authentication Secrets (generated by setup_auth.py)\n")
            lines.append(f"TOTP_SECRET={totp_secret}\n")
        else:
            lines.insert(insert_index, f"TOTP_SECRET={totp_secret}\n")

    if not jwt_found:
        # Add right after TOTP_SECRET
        for i, line in enumerate(lines):
            if line.startswith("TOTP_SECRET="):
                lines.insert(i + 1, f"JWT_SECRET={jwt_secret}\n")
                break
        else:
            lines.append(f"JWT_SECRET={jwt_secret}\n")

    # Write back
    with open(env_path, 'w') as f:
        f.writelines(lines)
    # Tighten perms — .env holds JWT_SECRET / TOTP_SECRET / PASSWORD_HASH.
    # Must not be world- or group-readable. Idempotent: safe on re-run.
    try:
        Path(env_path).chmod(0o600)
    except OSError:
        pass


def generate_qr_code(secret: str, account_name: str = "Cloude Code"):
    """Generate QR code for TOTP secret."""
    import pyotp
    import qrcode

    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(
        name=account_name,
        issuer_name="Cloude Code"
    )

    # Generate QR code
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(uri)
    qr.make(fit=True)

    # Print ASCII QR code to terminal
    qr.print_ascii()

    # Also save as image to project directory
    img = qr.make_image(fill_color="black", back_color="white")
    qr_path = Path(__file__).parent / "totp-qr.png"
    img.save(str(qr_path))

    return uri, qr_path

def prompt_with_default(prompt_text, default_value=""):
    """Prompt user for input with optional default value."""
    if default_value:
        user_input = input(f"{prompt_text} [{default_value}]: ").strip()
        return user_input if user_input else default_value
    else:
        return input(f"{prompt_text}: ").strip()


def setup_env_file(env_path):
    """Interactive setup for .env file configuration.

    Plan v3.2: the Cloudflare tunnel system was demolished. This wizard
    no longer asks for Cloudflare credentials, tunnel names, or zone
    IDs. It only collects local paths + the Claude CLI binary location;
    secrets are minted later by the auth-secret block in main().
    """
    # Get current values if .env exists
    current_values = {}
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    current_values[key] = value

    print("=" * 70)
    print("Local paths")
    print("=" * 70)
    print()

    # Try to auto-detect Claude CLI path
    import shutil
    auto_claude_path = shutil.which('claude')
    if not auto_claude_path:
        # Check common locations
        common_paths = [
            str(Path.home() / '.claude' / 'local' / 'claude'),
            '/usr/local/bin/claude',
            '/opt/homebrew/bin/claude'
        ]
        for path_str in common_paths:
            if Path(path_str).exists():
                auto_claude_path = path_str
                break

    default_claude_path = current_values.get('CLAUDE_CLI_PATH', auto_claude_path or '/path/to/claude')
    if auto_claude_path:
        print(f"📍 Detected Claude CLI at: {auto_claude_path}")

    claude_cli_path = prompt_with_default(
        "Claude CLI path (leave empty for auto-detect)",
        default_claude_path if not auto_claude_path else auto_claude_path
    )

    default_working_dir = current_values.get('DEFAULT_WORKING_DIR', '~/cloude-projects')
    working_dir = prompt_with_default(
        "Default working directory for projects",
        default_working_dir
    )

    default_log_dir = current_values.get('LOG_DIRECTORY', '/tmp/cloude-code-logs')
    log_dir = prompt_with_default(
        "Log directory",
        default_log_dir
    )

    print()

    # Update .env file with all values
    env_template_path = env_path.parent / ".env.example"
    if env_template_path.exists():
        with open(env_template_path) as f:
            content = f.read()
    else:
        # Minimal template if .env.example doesn't exist (rare - .env.example
        # is a tracked file, so this branch only fires if it was deleted).
        # Plan v3.2: no Cloudflare/tunnel keys — the tunnel system is gone.
        # HOST/PORT literals below must match src/config.py's Settings
        # field defaults (Settings.host = "0.0.0.0", Settings.port = 8000) -
        # that class is the single configuration root; this is a fallback
        # copy of its documented defaults, not an independent decision.
        content = (
            "# Server Configuration\n"
            "HOST=0.0.0.0\n"
            "PORT=8000\n"
            "\n"
            "# Session Configuration\n"
            "DEFAULT_WORKING_DIR=~/cloude-projects\n"
            "SESSION_TIMEOUT=3600\n"
            "\n"
            "# Claude CLI Configuration\n"
            "CLAUDE_CLI_PATH=/path/to/claude\n"
            "\n"
            "# Logging\n"
            "LOG_BUFFER_SIZE=1000\n"
            "LOG_FILE_RETENTION=7\n"
            "LOG_DIRECTORY=/tmp/cloude-code-logs\n"
            "\n"
            "# Security (Optional)\n"
            "API_KEY=\n"
            "# ALLOWED_ORIGINS defaults to [\"*\"] - only set if you need to restrict origins\n"
            "\n"
            "# Authentication Secrets\n"
            "TOTP_SECRET=\n"
            "JWT_SECRET=\n"
            "\n"
            "# Authentication Configuration\n"
            "AUTH_CONFIG_FILE=./config.json\n"
        )

    # Replace placeholders using regex for reliability
    import re

    # Replace optional settings
    # Handle CLAUDE_CLI_PATH
    if 'CLAUDE_CLI_PATH=' in content:
        content = re.sub(r'^CLAUDE_CLI_PATH=.*$', f'CLAUDE_CLI_PATH={claude_cli_path}', content, flags=re.MULTILINE)
    else:
        content += f"\nCLAUDE_CLI_PATH={claude_cli_path}\n"

    # Handle DEFAULT_WORKING_DIR
    if 'DEFAULT_WORKING_DIR=' in content:
        content = re.sub(r'^DEFAULT_WORKING_DIR=.*$', f'DEFAULT_WORKING_DIR={working_dir}', content, flags=re.MULTILINE)
    else:
        content += f"DEFAULT_WORKING_DIR={working_dir}\n"

    # Handle LOG_DIRECTORY
    if 'LOG_DIRECTORY=' in content:
        content = re.sub(r'^LOG_DIRECTORY=.*$', f'LOG_DIRECTORY={log_dir}', content, flags=re.MULTILINE)
    else:
        content += f"LOG_DIRECTORY={log_dir}\n"

    # Write .env
    with open(env_path, 'w') as f:
        f.write(content)
    # Tighten perms — .env will hold JWT_SECRET / TOTP_SECRET / PASSWORD_HASH
    # once update_env_file() runs. Chmod here too so the window between the
    # two writes is never world-readable. Idempotent.
    try:
        Path(env_path).chmod(0o600)
    except OSError:
        pass

    print()
    print(f"✅ Configuration written to {env_path}")
    print()

    # Validate Claude CLI path
    if claude_cli_path and claude_cli_path != '/path/to/claude':
        if not Path(claude_cli_path).exists():
            print(f"⚠️  Claude CLI not found at: {claude_cli_path}")
            print("   This may cause issues when creating projects.")
            print("   You can update CLAUDE_CLI_PATH in .env later.")
            print()

    return claude_cli_path, working_dir, log_dir


def _generate_ntfy_topic() -> str:
    """Generate a fresh 32-hex-char ntfy topic.

    Treat as a credential — anyone with this string can read your
    notifications. We use ``secrets.token_hex(16)`` for 128 bits of
    entropy in a URL-safe form (ntfy topics are ASCII-only paths).
    """
    return f"cloude-{secrets.token_hex(16)}"


def _default_public_base_url() -> str:
    """Best-effort guess at this machine's mDNS-resolvable URL."""
    try:
        hostname = socket.gethostname()
        # Strip any existing .local suffix to avoid double-suffixing.
        if hostname.endswith(".local"):
            hostname = hostname[: -len(".local")]
        return f"http://{hostname}.local:8000"
    except Exception:
        return "http://localhost:8000"


def _validate_url_reachable(url: str, timeout: float = 3.0) -> bool:
    """HEAD probe a URL. Returns True on any 2xx-4xx (server is alive).

    Connection refused / timeout / DNS failure → False. We warn-and-
    continue rather than block setup; the user may be configuring on
    a different network than they'll deploy on.

    TLS verification is disabled here on purpose: users on a LAN often
    run Cloude Code behind self-signed certs (mkcert, Caddy local CA),
    and this probe is a liveness check — not an authenticity check.
    The real request a user makes from their phone will use whatever
    trust store their OS provides. False negatives here are far more
    painful than the zero-risk of accepting a self-signed response
    from a host the user just typed.
    """
    try:
        import httpx
        with httpx.Client(timeout=timeout, follow_redirects=False, verify=False) as client:
            resp = client.head(url)
            # Any HTTP response means SOMETHING is listening.
            return 200 <= resp.status_code < 500
    except Exception:
        return False


def setup_notifications_block(config_path: Path) -> None:
    """Interactive ntfy push setup. Updates config.json in place.

    Asks the user if they want notifications. If yes:
    - Generates a fresh 32-hex topic.
    - Prompts for public_base_url with a sensible mDNS default.
    - HEAD-probes the URL; warns if unreachable but does not block.
    - Writes the notifications block to config.json.
    """
    print()
    print("=" * 70)
    print("Push Notifications (ntfy.sh)")
    print("=" * 70)
    print()
    print("Cloude Code can push permission prompts, task-complete,")
    print("and other signals to your phone via ntfy.sh.")
    print()
    print("Privacy note: titles/bodies contain NO project name. The")
    print("only identifying data is the deep-link URL (LAN-only).")
    print()
    answer = input("Enable push notifications? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        print("Skipping notifications setup.")
        return

    topic = _generate_ntfy_topic()
    base_url = "https://ntfy.sh"

    print()
    print("Subscribe in the ntfy app to this exact URL:")
    print()
    print(f"   {base_url}/{topic}")
    print()
    print("(iOS: ntfy app → + → Subscribe to topic)")
    print()
    input("Press Enter once you're subscribed...")

    print()
    default_pub = _default_public_base_url()
    public_base_url = prompt_with_default(
        "Public/LAN base URL for deep links",
        default_pub,
    )

    if public_base_url:
        print(f"Probing {public_base_url}/health ...")
        ok = _validate_url_reachable(f"{public_base_url}/health")
        if ok:
            print(f"OK — server reachable at {public_base_url}")
        else:
            print(f"WARN: {public_base_url} not reachable from here.")
            print("      Notifications will still work; deep-link clicks")
            print("      will only resolve from inside your LAN.")

    # Read existing config (created earlier in main()).
    if not config_path.exists():
        print(f"WARN: {config_path} not found — skipping notifications write.")
        return

    try:
        with open(config_path) as f:
            data = json.load(f)
    except Exception as e:
        print(f"WARN: could not read {config_path}: {e}")
        return

    data["notifications"] = {
        "enabled": True,
        "ntfy_base_url": base_url,
        "ntfy_topic": topic,
        "public_base_url": public_base_url,
    }

    try:
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)
        print()
        print(f"Notifications configured in {config_path}")
        print()
        # Item 9: tap-to-validate hint. After setup finishes, encourage
        # the user to open the deep-link base URL on their phone so they
        # confirm LAN reachability end-to-end before they rely on push
        # notifications. We print the base URL itself (not a specific
        # session deep link) because no session exists at setup time.
        if public_base_url:
            print("=" * 70)
            print("Confirm LAN reachability from your phone")
            print("=" * 70)
            print()
            print("Open this URL on the device where you'll receive ntfy pushes:")
            print()
            print(f"   {public_base_url}")
            print()
            print("If the Cloude Code login screen loads, push-notification")
            print("deep links will work from your phone's lock-screen.")
            print()
    except Exception as e:
        print(f"WARN: could not write {config_path}: {e}")


def rotate_topic_command() -> int:
    """Regenerate the ntfy topic and write to config.json.

    Reads the existing config to preserve ``ntfy_base_url`` and
    ``public_base_url``. If the notifications block is missing, we
    create one with ``enabled=false`` and ``public_base_url=""`` so
    the user can wire up the rest later.

    Returns process exit code.
    """
    project_root = Path(__file__).parent
    config_path = project_root / "config.json"

    if not config_path.exists():
        print(f"ERROR: {config_path} not found. Run setup_auth.py first.")
        return 1

    try:
        with open(config_path) as f:
            data = json.load(f)
    except Exception as e:
        print(f"ERROR: could not read {config_path}: {e}")
        return 1

    existing = data.get("notifications") or {}
    base_url = existing.get("ntfy_base_url") or "https://ntfy.sh"
    public_base_url = existing.get("public_base_url") or ""
    enabled = existing.get("enabled", True)

    new_topic = _generate_ntfy_topic()
    data["notifications"] = {
        "enabled": enabled,
        "ntfy_base_url": base_url,
        "ntfy_topic": new_topic,
        "public_base_url": public_base_url,
    }

    try:
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"ERROR: could not write {config_path}: {e}")
        return 1

    print()
    print("=" * 70)
    print("ntfy topic rotated")
    print("=" * 70)
    print()
    print("Re-subscribe in the ntfy app to this exact URL:")
    print()
    print(f"   {base_url}/{new_topic}")
    print()
    print("Old topic is now invalid — restart the Cloude Code server")
    print("to pick up the new topic.")
    print()
    return 0


def confirm_rotation(
    *, rotate_totp: bool, rotate_jwt: bool, assume_yes: bool = False
) -> bool:
    """Show the blast radius of a rotation and demand a typed confirmation.

    Args:
        rotate_totp: Whether TOTP_SECRET is about to be replaced.
        rotate_jwt: Whether JWT_SECRET is about to be replaced.
        assume_yes: Skip the prompt (for scripted use).

    Returns:
        bool: True when the rotation may proceed.
    """
    print()
    print("=" * 70)
    print("DESTRUCTIVE ROTATION REQUESTED")
    print("=" * 70)
    print()
    if rotate_totp:
        print("  --rotate-totp replaces your TOTP secret.")
        print("    Your authenticator app STOPS WORKING the moment this runs.")
        print("    You must scan the new QR code printed below to log in again.")
        print()
    if rotate_jwt:
        print("  --rotate-jwt replaces your JWT signing secret.")
        print("    EVERY logged-in session and device is signed out immediately.")
        print()
    if assume_yes:
        print("Proceeding (--yes given).")
        print()
        return True
    answer = input("Type 'rotate' to confirm, anything else to cancel: ").strip()
    print()
    return answer == "rotate"


def report_secret_outcome(outcome: dict) -> None:
    """Print what happened to each secret, without printing any value.

    Args:
        outcome: The dict returned by ``apply_auth_secrets``.

    Returns:
        None.
    """
    words = {
        "kept": "kept the existing value (unchanged)",
        "created": "generated (none was set)",
        "rotated": "REPLACED with a new value",
    }
    print(f"  TOTP_SECRET: {words[outcome['totp_action']]}")
    print(f"  JWT_SECRET:  {words[outcome['jwt_action']]}")
    if outcome["jwt_action"] == "rotated":
        print("  -> every logged-in session has been signed out.")
    if outcome["sentinel_cleared"]:
        print(f"  -> cleared the stale {PAIRED_SENTINEL_NAME} pairing marker,")
        print("     so the app will serve a fresh QR for re-pairing.")
    print()


def finish_setup(project_root: Path, env_path: Path, config_path: Path) -> None:
    """Print the closing summary, start the server, and restart the app.

    Shared by both exits from main(): the run that paired a new secret
    and the run that kept an existing one. Keeping them on one path is
    what stops a preserving run from skipping the server start.

    Args:
        project_root: Directory holding venv/ and src/.
        env_path: Path to the written .env, for the summary line.
        config_path: Path to config.json, for the summary line.

    Returns:
        None.
    """
    print()
    print("=" * 70)
    print("Setup Complete!")
    print("=" * 70)
    print()
    print(f"✅ Configuration: {env_path}")
    print(f"✅ App config: {config_path}")
    print()
    print("You can find and modify the app config here:")
    print(f"   {config_path}")
    print()
    print("Starting Cloude Code server...")
    print()

    # Start the server
    try:
        venv_python = project_root / "venv" / "bin" / "python3"
        if venv_python.exists():
            subprocess.Popen([str(venv_python), '-m', 'src.main'],
                           cwd=str(project_root),
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            print("✅ Server started in background")
        else:
            print("⚠️  Could not find venv, please start server manually with: ./start.sh")
    except Exception as e:
        print(f"⚠️  Could not start server: {e}")
        print("   Start manually with: ./start.sh")

    # Restart Cloude Code app if it's running
    print()
    print("Restarting Cloude Code app to apply changes...")
    try:
        # Check if the app is running
        result = subprocess.run(['pgrep', '-x', 'Cloude Code'], capture_output=True)
        if result.returncode == 0:
            # App is running, restart it
            subprocess.run(['killall', 'Cloude Code'], check=False)
            import time
            time.sleep(2)  # Wait for graceful shutdown
            subprocess.run(['open', '-a', 'Cloude Code'], check=False)
            print("✅ Cloude Code app restarted")
        else:
            print("ℹ️  Cloude Code app not running (launch it from Applications)")
    except Exception as e:
        print(f"⚠️  Could not restart app: {e}")
        print("   Please restart Cloude Code manually from Applications")

    print()
    print("This window will close in 3 seconds...")
    import time
    time.sleep(3)

    # Close terminal window
    subprocess.run(['osascript', '-e', 'tell application "Terminal" to close first window'],
                  check=False)



def main():
    """Main setup function."""
    # Handle --rotate-topic before requiring full venv / cloudflared.
    parser = argparse.ArgumentParser(
        description="Cloude Code authentication / notifications setup",
        add_help=True,
    )
    parser.add_argument(
        "--rotate-topic",
        action="store_true",
        help="Regenerate ntfy push topic, write to config.json, exit.",
    )
    parser.add_argument(
        "--rotate-totp",
        action="store_true",
        help=(
            "DESTRUCTIVE: replace TOTP_SECRET. This UNPAIRS your "
            "authenticator app; you must scan the new QR to get back in."
        ),
    )
    parser.add_argument(
        "--rotate-jwt",
        action="store_true",
        help=(
            "DESTRUCTIVE: replace JWT_SECRET. This SIGNS OUT every "
            "logged-in session and device immediately."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the typed confirmation for --rotate-totp / --rotate-jwt.",
    )
    args = parser.parse_args()

    if args.rotate_topic:
        sys.exit(rotate_topic_command())

    if (args.rotate_totp or args.rotate_jwt) and not confirm_rotation(
        rotate_totp=args.rotate_totp,
        rotate_jwt=args.rotate_jwt,
        assume_yes=args.yes,
    ):
        print("Rotation cancelled. Nothing was changed.")
        sys.exit(1)

    # Ensure venv has required dependencies
    check_and_setup_venv()

    print("=" * 70)
    print("Cloude Code Authentication Setup")
    print("=" * 70)
    print()

    # Show Python version
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"🐍 Python {python_version}")
    print()

    # Project root
    project_root = Path(__file__).parent
    env_path = project_root / ".env"

    # Snapshot the auth secrets BEFORE setup_env_file rebuilds .env from
    # .env.example, which blanks both secret lines. Reading them back after
    # that rewrite would see empty values and mint replacements - which is
    # exactly how a second setup run used to orphan a paired authenticator.
    captured_secrets = capture_auth_secrets(env_path)

    # Interactive .env setup (LAN-only — no Cloudflare prompts)
    claude_cli_path, working_dir, log_dir = setup_env_file(env_path)

    # Create directories
    import os
    print("Creating directories...")
    log_dir_expanded = os.path.expanduser(os.path.expandvars(log_dir))
    working_dir_expanded = os.path.expanduser(os.path.expandvars(working_dir))

    try:
        Path(log_dir_expanded).mkdir(parents=True, exist_ok=True)
        print(f"✅ Created log directory: {log_dir_expanded}")
    except Exception as e:
        print(f"⚠️  Could not create log directory: {e}")

    try:
        Path(working_dir_expanded).mkdir(parents=True, exist_ok=True)
        print(f"✅ Created projects directory: {working_dir_expanded}")
    except Exception as e:
        print(f"⚠️  Could not create projects directory: {e}")

    print()

    # Converge the auth secrets: keep what exists, mint only what is missing,
    # replace only what was explicitly asked for.
    print("Authentication secrets...")
    secret_outcome = apply_auth_secrets(
        env_path,
        captured_secrets,
        rotate_totp=args.rotate_totp,
        rotate_jwt=args.rotate_jwt,
    )
    totp_secret = secret_outcome["totp_secret"]
    report_secret_outcome(secret_outcome)

    # Config file in project directory
    config_path = project_root / "config.json"

    # Check if config already exists
    if config_path.exists():
        print(f"📄 Config file exists at: {config_path}")

        # Remove secrets from existing config if they're there
        try:
            with open(config_path) as f:
                data = json.load(f)

            if "totp_secret" in data or "jwt_secret" in data:
                data.pop("totp_secret", None)
                data.pop("jwt_secret", None)

                with open(config_path, 'w') as f:
                    json.dump(data, f, indent=2)

                print("✅ Removed secrets from config.json (now in .env)\n")
            else:
                print("✅ Config file already clean (no secrets)\n")

        except Exception as e:
            print(f"⚠️  Error cleaning config: {e}\n")

    else:
        # Create new config from example
        example_path = project_root / "config.example.json"
        with open(example_path) as f:
            config = json.load(f)

        # Remove secrets from config (they're in .env now)
        config.pop("totp_secret", None)
        config.pop("jwt_secret", None)

        # Write config WITHOUT secrets
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

        print(f"✅ Configuration file created at: {config_path}\n")

    # Item 6: optional ntfy push notification setup. Runs AFTER the
    # config file exists (we mutate it in place).
    setup_notifications_block(config_path)

    print("=" * 70)
    print("TOTP Setup")
    print("=" * 70)
    print()

    # Only ever show a QR when the secret actually changed. Printing one for
    # an unchanged secret is what taught the user that a second setup run
    # had re-paired him when it had not - and printing NO qr after a
    # rotation would leave him unable to pair at all. Both are the same
    # lockout wearing different clothes.
    if not secret_outcome["show_qr"]:
        print("Your existing TOTP secret was kept, so your authenticator app")
        print("still works. There is nothing to scan and no new QR code.")
        print()
        print("If you have LOST your authenticator, re-run with:")
        print("   python3 setup_auth.py --rotate-totp")
        print()
        finish_setup(project_root, env_path, config_path)
        return

    # Generate and display QR code
    uri, qr_path = generate_qr_code(totp_secret)

    # Open QR code with Preview
    print()
    print(f"📱 Opening QR code...")
    try:
        subprocess.run(['open', str(qr_path)], check=False)
        print(f"✅ QR code opened in Preview: {qr_path}")
    except Exception as e:
        print(f"⚠️  Could not open QR code automatically: {e}")
        print(f"   Please open manually: {qr_path}")

    print()
    print("=" * 70)
    print(f"📱 SCAN THE QR CODE that just opened in Preview!")
    print("=" * 70)
    print()
    print("Use Google Authenticator, Authy, or any TOTP app")
    print()
    print(f"Or manually enter this secret: {totp_secret}")
    print()

    input("Press Enter after scanning the QR code...")

    finish_setup(project_root, env_path, config_path)


if __name__ == "__main__":
    main()
