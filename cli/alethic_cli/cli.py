import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import urllib.parse
import uuid
import webbrowser

from .client import CliError, Client, normalize_base, safe_output
from .credentials import CredentialStore


DEFAULT_BASE = "https://alethicinsight.com/recruitment"
DEFAULT_SCOPES = ["profile:read:self", "projects:read", "knowledge:read", "applications:read:self"]


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError("INVALID_ARGUMENT", "Invalid arguments; use alethic --help for supported commands.", 2)


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--non-interactive", action="store_true", default=argparse.SUPPRESS)
    root = Parser(prog="alethic", parents=[common])
    groups = root.add_subparsers(dest="group", required=True)

    def add(parent, name):
        return parent.add_parser(name, parents=[common])

    def pages(command):
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--cursor")

    auth = add(groups, "auth").add_subparsers(dest="action", required=True)
    login = add(auth, "login")
    login.add_argument("--scope", action="append", dest="scopes")
    for action in ("status", "scopes", "logout"):
        add(auth, action)
    add(groups, "capabilities")
    projects = add(groups, "projects").add_subparsers(dest="action", required=True)
    project_list = add(projects, "list")
    pages(project_list)
    project_list.add_argument("--recruiting", action="store_true")
    add(projects, "get").add_argument("id")
    knowledge = add(groups, "knowledge").add_subparsers(dest="action", required=True)
    search = add(knowledge, "search")
    search.add_argument("query")
    pages(search)
    add(knowledge, "get").add_argument("id")
    forms = add(groups, "forms").add_subparsers(dest="action", required=True)
    add(forms, "schema").add_argument("kind", nargs="?", default="project-join", choices=["project-join"])
    applications = add(groups, "applications").add_subparsers(dest="action", required=True)
    application_list = add(applications, "list")
    pages(application_list)
    application_list.add_argument("--mine", action="store_true")
    application_list.add_argument("--project")
    add(applications, "get").add_argument("id")
    for action in ("draft", "submit"):
        command = add(applications, action)
        command.add_argument("--file", required=True)
        if action == "submit":
            command.add_argument("--dry-run", action="store_true")
    return root


def read_application(filename):
    try:
        raw = Path(filename).read_text(encoding="utf-8")
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (OSError, ValueError):
        raise CliError("VALIDATION_FAILED", "Application file must be a readable UTF-8 JSON object.", 6) from None
    if not isinstance(value, dict):
        raise CliError("VALIDATION_FAILED", "Application file must contain a JSON object.", 6)

    def contains_confirmation(item):
        if isinstance(item, dict):
            return any("confirmation" in key.lower() or "token" in key.lower() or contains_confirmation(v) for key, v in item.items())
        return isinstance(item, list) and any(contains_confirmation(v) for v in item)

    if contains_confirmation(value):
        raise CliError("CONFIRMATION_REQUIRED", "Do not supply confirmation credentials in CLI files. Confirm on the trusted website.", 7)
    return value


def page_query(args):
    if not 1 <= args.limit <= 50:
        raise CliError("INVALID_ARGUMENT", "--limit must be between 1 and 50.", 2)
    return {"limit": args.limit, "cursor": args.cursor}


def execute(args, client, store):
    if args.group == "auth" and args.action == "logout":
        store.delete()
        print("Local credentials deleted. The server grant was NOT revoked; revoke it on the trusted /org webpage (or wait for expiry).", file=sys.stderr)
        return {"logged_out": True, "server_revoked": False}
    if args.group == "auth" and args.action == "login":
        request_id = uuid.uuid4().hex
        scopes = args.scopes or DEFAULT_SCOPES
        browser_url = client.base + "/org?" + urllib.parse.urlencode({"cli_request": request_id, "scopes": ",".join(scopes)})
        if getattr(args, "non_interactive", False) or not sys.stdin.isatty():
            raise CliError("AUTH_REQUIRED", "Login requires an interactive terminal and trusted webpage authorization.", 3, authorization_url=browser_url)
        print("Authorize this CLI on the trusted website:\n" + browser_url, file=sys.stderr)
        try:
            webbrowser.open(browser_url)
        except (OSError, webbrowser.Error):
            print("Could not open a browser; open the URL above manually.", file=sys.stderr)
        code = getpass.getpass("One-time authorization code (hidden): ", stream=sys.stderr).strip()
        if not code:
            raise CliError("AUTH_REQUIRED", "An authorization code is required.", 3)
        value = client.request("POST", "/auth/cli/token", {"request_id": request_id, "code": code}, authenticated=False)
        if not isinstance(value, dict):
            raise CliError("API_UNAVAILABLE", "Token exchange returned an invalid response.", 5)
        store.save(client.base, value)
        client.token = value["token"]
        return {"authenticated": True, "expires_at": value["expires_at"], "scopes": value["scopes"]}
    credentials = store.load(client.base)
    client.token = credentials["token"]
    if args.group == "auth":
        if args.action == "scopes":
            return {"scopes": credentials["scopes"], "expires_at": credentials["expires_at"]}
        identity = client.request("GET", "/me")
        return {"authenticated": True, "identity": identity, "expires_at": credentials["expires_at"], "scopes": credentials["scopes"]}
    if args.group == "capabilities":
        return client.request("GET", "/capabilities")
    if args.group == "projects":
        if args.action == "get":
            return client.request("GET", "/projects/" + urllib.parse.quote(args.id, safe=""))
        query = page_query(args)
        if args.recruiting:
            query["recruiting"] = "true"
        return client.request("GET", "/projects", query=query)
    if args.group == "knowledge":
        if args.action == "get":
            return client.request("GET", "/knowledge/documents/" + urllib.parse.quote(args.id, safe=""))
        return client.request("GET", "/knowledge/search", query={**page_query(args), "q": args.query})
    if args.group == "forms":
        return client.request("GET", "/forms/project-join/schema")
    if args.action == "list":
        return client.request("GET", "/applications", query={**page_query(args), "mine": "true", "project": args.project})
    if args.action == "get":
        return client.request("GET", "/applications/" + urllib.parse.quote(args.id, safe=""))
    body = read_application(args.file)
    if args.action == "submit" and args.dry_run:
        result = client.request("POST", "/applications/validate", body)
        if isinstance(result, dict) and result.get("valid") is False:
            raise CliError("VALIDATION_FAILED", "Server validation failed; review the form schema and input fields.", 6)
        return result
    draft = client.request("POST", "/applications/drafts", body)
    draft_id = draft.get("id", draft.get("draft_id")) if isinstance(draft, dict) else None
    if not isinstance(draft_id, str) or not draft_id:
        raise CliError("API_UNAVAILABLE", "Draft response did not include an identifier.", 5)
    confirmation_url = client.base + "/org?" + urllib.parse.urlencode({"draft_id": draft_id})
    result = {"draft": draft, "confirmation_url": confirmation_url, "submitted": False}
    if args.action == "submit":
        raise CliError("CONFIRMATION_REQUIRED", "Draft prepared, not submitted. Review and submit on the trusted webpage.", 7, **result)
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    client = None
    exit_code = 0
    try:
        args = parser().parse_args(argv)
        base = normalize_base(os.environ.get("ALETHIC_API_BASE_URL", os.environ.get("ALETHIC_BASE_URL", DEFAULT_BASE)))
        client = Client(base)
        payload = execute(args, client, CredentialStore())
    except CliError as exc:
        payload, exit_code = exc.payload, exc.exit_code
    except (KeyboardInterrupt, EOFError):
        payload, exit_code = {"error": {"code": "AUTH_REQUIRED", "message": "Authorization cancelled."}}, 3
    except (OSError, ValueError):
        payload, exit_code = {"error": {"code": "API_UNAVAILABLE", "message": "Local I/O or API processing failed."}}, 5
    payload = safe_output(payload, (client.token,) if client else ())
    if not json_mode and "error" in payload:
        print(payload["error"]["message"], file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False, indent=None if json_mode else 2, allow_nan=False))
    return exit_code
