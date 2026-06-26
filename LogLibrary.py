from loguru import logger
import json
import sys
import os
import base64

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.exceptions import InvalidTag
    _HAS_CRYPTO = True
except ImportError:  # cryptography is optional; secrets stay plaintext without it.
    _HAS_CRYPTO = False

# --------------------------- Secret handling ---------------------------
# Any config key whose name contains "pass" (case-insensitive) is treated as a
# secret: stored encrypted on disk and decrypted in memory for the application.
#
# Two layers of encryption are used, on purpose with DIFFERENT algorithms:
#   1. Secret VALUES in the JSON config are encrypted with Fernet (AES-CBC+HMAC).
#   2. The Fernet KEY itself is stored next to the program in "<name>.key",
#      wrapped with ChaCha20-Poly1305 under a key derived (scrypt) from a stable
#      per-machine identifier. So the key file is never plaintext and is bound to
#      the machine it was created on.
_ENC_PREFIX = 'ENC:'          # marks an already-encrypted value in the JSON file.
_KEY_ENV = 'LOGLIB_KEY'       # optional env var overriding the on-disk key file.
_KEY_FILE_VERSION = 1         # format version stored inside the wrapped key file.

'''
To use default config minimum, copy the following code snippet into your main program.
----------------------------------------------------------------------------------------------------------------------------------------

from LogLibrary import Load_Config, Loguru_Logging
# ----------------------- Configuration Values -----------------------
Program_Name = ""        # Program name for identification and logging.
Program_Version = ""            # Program version used for file naming and logging.
# ---------------------------------------------------------------------

default_config = {
            "log_Level": "DEBUG",
            "Log_Console": 1,  # 1/true to enable console logging, 0/false to disable.
            "log_Backup": 90,         # Log retention duration in days (older logs are removed).
            "Log_Size": "10 MB"       # Maximum log file size before rotation.
            # Any key containing "pass" (e.g. "DB_Password") is encrypted on disk
            # automatically after the first run. Put the plaintext value here the
            # first time only.
        }

config = Load_Config(default_config, Program_Name)
logger = Loguru_Logging(config, Program_Name, Program_Version)

# Secret/password handling:
# - Any config key whose name contains "pass" (case-insensitive) is treated as a
#   secret. On the FIRST run you set it as plaintext; the library then encrypts it
#   in the JSON file (value becomes "ENC:..."). At runtime `config[...]` always
#   holds the decrypted plaintext for your program to use.
# - Encryption uses the `cryptography` package (pip install cryptography). The
#   Fernet key is stored next to the program as "<Program_Name>.key" and is
#   itself encrypted (ChaCha20-Poly1305) with a key derived from this machine's
#   identifier, so the key file is bound to the machine and never plaintext.
#   The file is created automatically on first run; copying it to another machine
#   will NOT work (different machine id). To move secrets between machines, set
#   the LOGLIB_KEY environment variable, which overrides the key file.
----------------------------------------------------------------------------------------------------------------------------------------
'''
global script_dir

if getattr(sys, 'frozen', False):
    # When packaged into a single executable (e.g., PyInstaller), place files
    # next to the executable to keep configuration and logs with the binary.
    script_dir = os.path.dirname(sys.executable)
else:
    # When running as a normal script, co-locate config/logs with this module.
    script_dir = os.path.dirname(os.path.abspath(__file__))

def _is_truthy(value):
    """Interpret common truthy representations from JSON/config.

    Accepts native bools/ints as well as strings like "1", "true", "yes",
    and "on" (case-insensitive) so the console toggle is forgiving of how
    operators edit the JSON file by hand.
    """
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _write_config(config_path, config):
    """Atomically write `config` to `config_path` as pretty-printed JSON.

    Writing to a temporary file and replacing the target avoids leaving a
    truncated/corrupt config behind if the process is interrupted mid-write.
    """
    tmp_path = f'{config_path}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as tmp_file:
        json.dump(config, tmp_file, indent=4)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.replace(tmp_path, config_path)


def _deep_merge(defaults, overrides):
    """Return `defaults` with `overrides` layered on top, recursing into dicts.

    Where both sides hold a dict for the same key, the dicts are merged
    recursively so a partial nested override (e.g. only `db.host` in the file)
    keeps sibling defaults (e.g. `db.port`). Any non-dict override replaces the
    default outright. Neither input is mutated.
    """
    merged = dict(defaults)
    for key, value in overrides.items():
        base = merged.get(key)
        if isinstance(base, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(base, value)
        else:
            merged[key] = value
    return merged


def _is_secret_key(key_name):
    """Return True if `key_name` looks like a secret (contains "pass")."""
    return 'pass' in str(key_name).lower()


def _config_has_secret(obj):
    """Recursively check whether any key in `obj` is a secret with a value."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _is_secret_key(key) and isinstance(value, str) and value != '':
                return True
            if _config_has_secret(value):
                return True
    elif isinstance(obj, list):
        return any(_config_has_secret(item) for item in obj)
    return False


def _key_path(safe_name):
    """Return the key-file path ("<safe_name>.key") next to the program."""
    return os.path.join(script_dir, f'{safe_name}.key')


def _machine_id():
    """Return a stable per-machine identifier (bytes), best effort.

    Tries OS-provided machine GUIDs first (systemd machine-id, Windows
    MachineGuid, macOS IOPlatformUUID), then falls back to hostname + MAC.
    Never raises; the fallback is always available.
    """
    # Linux / systemd.
    for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            with open(path, 'r', encoding='utf-8') as id_file:
                value = id_file.read().strip()
            if value:
                return value.encode()
        except OSError:
            pass

    # Windows registry MachineGuid. Force the 64-bit view so a 32-bit Python on
    # 64-bit Windows is not WOW64-redirected to WOW6432Node (where MachineGuid
    # may be absent), which would otherwise drop us to the less-stable fallback.
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SOFTWARE\Microsoft\Cryptography',
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as reg_key:
                value, _ = winreg.QueryValueEx(reg_key, 'MachineGuid')
            if value:
                return str(value).encode()
        except OSError:
            pass

    # macOS IOPlatformUUID.
    if sys.platform == 'darwin':
        try:
            import subprocess
            out = subprocess.check_output(
                ['ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                stderr=subprocess.DEVNULL, text=True,
            )
            for line in out.splitlines():
                if 'IOPlatformUUID' in line:
                    return line.split('"')[-2].encode()
        except (OSError, subprocess.SubprocessError, IndexError):
            pass

    # Fallback: hostname + MAC (less stable, but always obtainable).
    import platform
    import uuid
    return f'{platform.node()}-{uuid.getnode()}'.encode()


def _derive_wrap_key(salt):
    """Derive a 32-byte ChaCha20-Poly1305 key from the machine id via scrypt."""
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    return kdf.derive(_machine_id())


def _wrap_key(raw_key):
    """Encrypt `raw_key` (the Fernet key) for storage; return JSON bytes.

    Uses ChaCha20-Poly1305 (distinct from the Fernet/AES-CBC scheme used for
    the secret values) under a machine-derived key. Salt and nonce are random
    and stored alongside the ciphertext (they are not secret).
    """
    salt = os.urandom(16)
    nonce = os.urandom(12)
    ciphertext = ChaCha20Poly1305(_derive_wrap_key(salt)).encrypt(nonce, raw_key, None)
    blob = {
        'v': _KEY_FILE_VERSION,
        'salt': base64.b64encode(salt).decode(),
        'nonce': base64.b64encode(nonce).decode(),
        'key': base64.b64encode(ciphertext).decode(),
    }
    return json.dumps(blob).encode('utf-8')


def _unwrap_key(blob_bytes):
    """Decrypt a wrapped key file produced by `_wrap_key`; return the raw key.

    Raises on tampering, a wrong machine id, or a malformed file so the caller
    can fall back to regenerating the key.
    """
    blob = json.loads(blob_bytes.decode('utf-8'))
    salt = base64.b64decode(blob['salt'])
    nonce = base64.b64decode(blob['nonce'])
    ciphertext = base64.b64decode(blob['key'])
    return ChaCha20Poly1305(_derive_wrap_key(salt)).decrypt(nonce, ciphertext, None)


def _write_key_file(key_path, raw_key):
    """Wrap `raw_key` and persist it to `key_path` with owner-only permissions.

    Returns True on success. chmod is best effort (unsupported on some
    Windows filesystems).
    """
    try:
        blob = _wrap_key(raw_key)
        with open(key_path, 'wb') as key_file:
            key_file.write(blob)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        return True
    except OSError as exc:
        sys.stderr.write(
            f'[LogLibrary] Could not write key file "{key_path}" ({exc}).\n'
        )
        return False


def _resolve_fernet(safe_name):
    """Return (Fernet, generated_flag, saved_flag).

    Key resolution order:
    1. The `LOGLIB_KEY` env var, when set and valid (overrides the key file).
    2. An existing wrapped key file "<safe_name>.key" next to the program,
       unwrapped with this machine's id.
    3. A freshly generated key, wrapped and written to that key file so it
       persists for the next run. `generated_flag` is True in this case and
       `saved_flag` reports whether the new key was successfully written.
    """
    key = os.environ.get(_KEY_ENV)
    if key:
        try:
            return Fernet(key.encode()), False, False
        except (ValueError, TypeError) as exc:
            sys.stderr.write(
                f'[LogLibrary] Invalid {_KEY_ENV} value ({exc}); '
                f'falling back to the key file.\n'
            )

    key_path = _key_path(safe_name)
    stale_key_exists = False

    # Reuse the stored key when the key file exists and unwraps on this machine.
    if os.path.exists(key_path):
        try:
            with open(key_path, 'rb') as key_file:
                raw_key = _unwrap_key(key_file.read())
            return Fernet(raw_key), False, False
        except (ValueError, KeyError, OSError, InvalidToken, InvalidTag) as exc:
            stale_key_exists = True
            sys.stderr.write(
                f'[LogLibrary] Could not read/unwrap key file "{key_path}" '
                f'({exc}); generating a new key.\n'
            )

    # No usable key yet: generate one and persist it for subsequent runs.
    new_key = Fernet.generate_key()
    if stale_key_exists:
        # Preserve the unreadable key instead of overwriting it, so recovery
        # (e.g. restoring the original machine) remains possible. If the backup
        # itself fails, do NOT overwrite the original — report it as unsaved so
        # the old key is never destroyed.
        try:
            os.replace(key_path, f'{key_path}.bak')
        except OSError as exc:
            sys.stderr.write(
                f'[LogLibrary] Could not back up unreadable key "{key_path}" '
                f'({exc}); leaving it untouched and not persisting the new key.\n'
            )
            return Fernet(new_key), True, False
    saved = _write_key_file(key_path, new_key)
    return Fernet(new_key), True, saved


def _process_secrets(obj, fernet):
    """Walk `obj`, returning (runtime_obj, disk_obj, changed).

    - Plaintext secrets: kept as-is for runtime, encrypted for disk.
    - Encrypted (ENC:) secrets: decrypted for runtime, left encrypted on disk.
    `changed` is True when at least one plaintext secret was encrypted.
    """
    if isinstance(obj, dict):
        runtime, disk, changed = {}, {}, False
        for key, value in obj.items():
            if _is_secret_key(key) and isinstance(value, str) and value != '':
                if value.startswith(_ENC_PREFIX):
                    token = value[len(_ENC_PREFIX):].encode()
                    try:
                        runtime[key] = fernet.decrypt(token).decode()
                    except (InvalidToken, UnicodeDecodeError):
                        # Wrong/missing key (InvalidToken) or corrupt non-UTF-8
                        # plaintext after decryption — degrade gracefully rather
                        # than crash the host application.
                        sys.stderr.write(
                            f'[LogLibrary] Could not decrypt "{key}" — wrong key '
                            f'or corrupt value. Leaving value encrypted.\n'
                        )
                        runtime[key] = value
                    disk[key] = value
                else:
                    runtime[key] = value  # plaintext for the application to use
                    disk[key] = _ENC_PREFIX + fernet.encrypt(value.encode()).decode()
                    changed = True
            else:
                child_runtime, child_disk, child_changed = _process_secrets(value, fernet)
                runtime[key], disk[key] = child_runtime, child_disk
                changed = changed or child_changed
        return runtime, disk, changed

    if isinstance(obj, list):
        runtime, disk, changed = [], [], False
        for item in obj:
            child_runtime, child_disk, child_changed = _process_secrets(item, fernet)
            runtime.append(child_runtime)
            disk.append(child_disk)
            changed = changed or child_changed
        return runtime, disk, changed

    return obj, obj, False


def _apply_secret_encryption(config, config_path, safe_name, force_write):
    """Encrypt plaintext secrets / decrypt stored secrets.

    Returns the runtime config (with plaintext secret values). Rewrites the
    config file with encrypted values when something changed or `force_write`
    is set and the file is missing.

    Args:
        config: The merged configuration dict.
        config_path: Path to the JSON config file.
        safe_name: Program name used to locate the per-program key file.
        force_write: When True, (re)write the file even if no plaintext secret
            needed encrypting (used when creating a brand-new config file).
    """
    if not _config_has_secret(config):
        return config

    if not _HAS_CRYPTO:
        sys.stderr.write(
            '[LogLibrary] Secret keys found but "cryptography" is not installed; '
            'storing values as plaintext. Run: pip install cryptography\n'
        )
        return config

    fernet, generated, saved = _resolve_fernet(safe_name)
    runtime_config, disk_config, changed = _process_secrets(config, fernet)

    if changed or force_write:
        try:
            _write_config(config_path, disk_config)
        except OSError as exc:
            sys.stderr.write(
                f'[LogLibrary] Failed to persist encrypted config ({exc}).\n'
            )

    if generated:
        key_path = _key_path(safe_name)
        if saved:
            sys.stderr.write(
                f'[LogLibrary] A new encryption key was generated, wrapped, and '
                f'saved to "{key_path}" (bound to this machine). Keep this file '
                f'with the program — it is required to decrypt your secrets.\n'
            )
        else:
            # The key could not be written, so it only lives in memory this run.
            # Without persistence the next run makes a different key and stored
            # secrets become unreadable; offer the env-var fallback.
            sys.stderr.write(
                '[LogLibrary] A new encryption key was generated but could NOT be '
                f'saved to "{key_path}". Secrets will be unreadable next run unless '
                f'you set {_KEY_ENV} in your environment:\n'
                f'{_generated_key_hint(fernet)}'
            )

    return runtime_config


def _generated_key_hint(fernet):
    """Build a copy-paste export line for a freshly generated Fernet key."""
    # Reconstruct the original urlsafe-base64 key from Fernet's split halves.
    # These are private attributes; guard against cryptography internals changing
    # so a failed hint never crashes the warning path.
    try:
        raw = fernet._signing_key + fernet._encryption_key
        key_str = base64.urlsafe_b64encode(raw).decode()
        return f'    export {_KEY_ENV}={key_str}\n'
    except AttributeError:
        return f'    (set {_KEY_ENV} to a saved key)\n'


def Load_Config(default_config, Program_Name):
    """Load or create a JSON config for the application.

    Behavior:
    - If `<Program_Name>_config.json` does not exist, create it with
      `default_config` and write to disk (pretty-printed).
    - Read the config and merge it onto `default_config` so newly introduced
      default keys are always present even for older config files.
    - If the file is missing or corrupt, fall back to `default_config`
      instead of crashing the host application.

    Args:
        default_config: A dict with default settings to seed the file.
        Program_Name: The app name used to derive the config filename.

    Returns:
        dict: Parsed configuration content (defaults merged with file values).
    """
    # Use a stable name even when Program_Name is empty.
    safe_name = Program_Name if Program_Name else 'app'
    config_file_name = f'{safe_name}_config.json'
    config_path = os.path.join(script_dir, config_file_name)

    # Create config file with default values if it does not exist.
    if not os.path.exists(config_path):
        # Persist defaults so operators can edit them later. Encrypt any secret
        # values up front so the new file never lands plaintext on disk.
        _write_config(config_path, default_config)
        return _apply_secret_encryption(dict(default_config), config_path, safe_name, force_write=True)

    # Load configuration, tolerating a missing/corrupt file.
    try:
        with open(config_path, 'r', encoding='utf-8') as config_file:
            file_config = json.load(config_file)
        if not isinstance(file_config, dict):
            raise ValueError('Config root must be a JSON object.')
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        # Don't take the host application down because of a bad config file;
        # back up the broken file and continue with defaults.
        sys.stderr.write(
            f'[LogLibrary] Failed to read config "{config_path}" ({exc}); '
            f'using default configuration.\n'
        )
        try:
            if os.path.exists(config_path):
                os.replace(config_path, f'{config_path}.bak')
        except OSError:
            pass
        return _apply_secret_encryption(dict(default_config), config_path, safe_name, force_write=True)

    # Merge file values over the defaults so missing keys are backfilled,
    # recursing into nested dicts so a partial nested override does not drop
    # sibling default keys.
    config = _deep_merge(default_config, file_config)

    # Encrypt any plaintext secrets (first run) / decrypt stored secrets so the
    # application always receives usable plaintext values.
    return _apply_secret_encryption(config, config_path, safe_name, force_write=False)

# ----------------------- Loguru Logging Setup -----------------------
def Loguru_Logging(config, Program_Name, Program_Version):
    """Initialize Loguru sinks per configuration.

    Sinks:
    - Console (optional): enabled when `Log_Console` is truthy.
    - File: `<script_dir>/logs/<Program_Name>_<Program_Version>.log` with
      size-based rotation and day-based retention.

    Args:
        config: The configuration dict returned by `Load_Config`.
        Program_Name: Application name (used in file naming and banner).
        Program_Version: Application version (used in file naming and banner).

    Returns:
        loguru.Logger: Configured logger instance ready for use.
    """
    logger.remove()

    log_Backup = int(config.get('log_Backup', 90))
    Log_Size = str(config.get('Log_Size', '10 MB')).upper()
    log_Level = str(config.get('log_Level', 'DEBUG')).upper()

    log_dir = os.path.join(script_dir, 'logs')
    os.makedirs(log_dir, exist_ok=True)

    safe_name = Program_Name if Program_Name else 'app'
    log_file_name = f'{safe_name}_{Program_Version}.log'
    log_file = os.path.join(log_dir, log_file_name)

    # Accept 1, "1", true, "true", "yes" etc. as enabling the console sink.
    if _is_truthy(config.get('Log_Console', 0)):
        logger.add(
            sys.stdout,
            level=log_Level,
            format="<green>{time}</green> | <blue>{level}</blue> | <cyan>{thread.id}</cyan> | <magenta>{function}</magenta> | {message}",
            enqueue=True,
        )

    logger.add(
        log_file,
        format="{time} | {level} | {thread.id} | {function} | {message}",
        level=log_Level,
        rotation=Log_Size,
        retention=f"{log_Backup} days",
        compression="zip",
        enqueue=True,       # non-blocking, process-safe writes (better throughput)
        backtrace=False,    # avoid leaking full stack frames into log files
        diagnose=False,     # avoid logging local variable values (perf + security)
    )

    logger.info('-' * 117)
    logger.info(f"Start {Program_Name} Version {Program_Version}")
    logger.info('-' * 117)

    return logger