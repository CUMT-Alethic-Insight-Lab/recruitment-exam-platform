import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .client import CliError


def expiry_timestamp(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        if isinstance(value, (int, float)):
            result = float(value)
        else:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
            result = parsed.timestamp()
        if not math.isfinite(result):
            raise ValueError()
        return result
    except (TypeError, ValueError, AttributeError, OverflowError):
        raise CliError("AUTH_REQUIRED", "Credentials are missing, invalid or expired; run alethic auth login.", 3) from None


def private_path(path, directory=False):
    if os.name == "nt":
        user = subprocess.run(["whoami"], check=True, capture_output=True, text=True).stdout.strip()
        if not user:
            raise OSError("Cannot identify current Windows user")
        permission = "(OI)(CI)F" if directory else "F"
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:{permission}"],
            check=True, capture_output=True, text=True,
        )
    else:
        path.chmod(0o700 if directory else 0o600)


class CredentialStore:
    def __init__(self):
        override = os.environ.get("ALETHIC_CONFIG_DIR")
        if override:
            self.directory = Path(override)
        elif os.name == "nt":
            self.directory = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Alethic"
        else:
            self.directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "alethic"
        self.path = self.directory / "credentials.json"

    def load(self, base):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("base_url") != base:
                raise ValueError()
            if not isinstance(value.get("token"), str) or not value["token"] or any(c in value["token"] for c in "\r\n"):
                raise ValueError()
            if not isinstance(value.get("scopes"), list) or not all(isinstance(s, str) for s in value["scopes"]):
                raise ValueError()
            if expiry_timestamp(value.get("expires_at")) <= time.time():
                raise ValueError()
            return value
        except (OSError, ValueError, TypeError):
            raise CliError("AUTH_REQUIRED", "Credentials are missing, invalid or expired; run alethic auth login.", 3) from None

    def save(self, base, value):
        token = value.get("token")
        scopes = value.get("scopes")
        if not isinstance(token, str) or not token or any(c in token for c in "\r\n") or not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise CliError("API_UNAVAILABLE", "Token exchange returned invalid credentials.", 5)
        if expiry_timestamp(value.get("expires_at")) <= time.time():
            raise CliError("AUTH_REQUIRED", "Token exchange returned expired credentials.", 3)
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_path(self.directory, directory=True)
            fd, temporary = tempfile.mkstemp(prefix="credentials-", dir=self.directory)
            os.close(fd)
            temporary = Path(temporary)
            private_path(temporary)
            temporary.write_text(json.dumps({"base_url": base, "token": token, "expires_at": value["expires_at"], "scopes": scopes}), encoding="utf-8")
            os.replace(temporary, self.path)
        except (OSError, subprocess.SubprocessError):
            raise CliError("CREDENTIAL_STORAGE_FAILED", "Could not secure or save local credentials; no token was printed.", 5) from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def delete(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise CliError("CREDENTIAL_STORAGE_FAILED", "Could not delete local credentials.", 5) from None
