import json
import urllib.error
import urllib.parse
import urllib.request


class CliError(Exception):
    def __init__(self, code, message, exit_code, **details):
        super().__init__(message)
        self.payload = {"error": {"code": code, "message": message, **details}}
        self.exit_code = exit_code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_base(value):
    parts = urllib.parse.urlsplit(value)
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise CliError("INVALID_ARGUMENT", "API base must be HTTPS (HTTP is allowed only for loopback testing).", 2)
    return value.rstrip("/")


def safe_output(value, secrets=()):
    if isinstance(value, dict):
        return {
            key: safe_output(item, secrets)
            for key, item in value.items()
            if not any(term in key.lower() for term in ("token", "secret", "password"))
            and ("authorization" not in key.lower() or key == "authorization_url")
            and key.lower() not in {"code", "confirmation"}
        } | ({"code": safe_output(value["code"], secrets)} if "code" in value and "message" in value else {})
    if isinstance(value, list):
        return [safe_output(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    return value


class Client:
    def __init__(self, base, token=None):
        self.base = normalize_base(base)
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, query=None, authenticated=True):
        url = self.base + "/api/v1" + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        headers = {"Accept": "application/json"}
        if authenticated:
            if not self.token:
                raise CliError("AUTH_REQUIRED", "Run alethic auth login.", 3)
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise CliError("API_UNAVAILABLE", "API response exceeded the client limit.", 5)
                value = json.loads(raw)
                if not isinstance(value, (dict, list)):
                    raise ValueError()
                json.dumps(value, allow_nan=False)
                return value
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                payload = json.loads(exc.read(65536))
                error = payload.get("error", {})
                code = error.get("code")
            except (ValueError, AttributeError, TypeError):
                code = None
            finally:
                exc.close()
            exit_code = {400: 6, 401: 3, 403: 4, 404: 4, 409: 7, 422: 6, 428: 7}.get(status, 5)
            defaults = {3: "AUTH_REQUIRED", 4: "RESOURCE_FORBIDDEN", 5: "API_UNAVAILABLE", 6: "VALIDATION_FAILED", 7: "STATE_CONFLICT"}
            # Do not echo server error bodies, which may contain credentials or input fields.
            allowed = {"AUTH_REQUIRED", "MEMBERSHIP_INACTIVE", "INSUFFICIENT_SCOPE", "RESOURCE_FORBIDDEN", "RESOURCE_NOT_FOUND", "VALIDATION_FAILED", "CONFIRMATION_REQUIRED", "STATE_CONFLICT", "RATE_LIMITED", "UPSTREAM_UNAVAILABLE"}
            code = "AUTH_REQUIRED" if status == 401 else (code if isinstance(code, str) and code in allowed else defaults[exit_code])
            raise CliError(code, f"API request failed (HTTP {status}).", exit_code, http_status=status) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise CliError("API_UNAVAILABLE", "Cannot reach API or API returned invalid JSON.", 5) from None
