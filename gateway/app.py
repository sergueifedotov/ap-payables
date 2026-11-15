#!/usr/bin/env python3
"""Accounts-payable MCP server.

POST /oauth/token exchanges Alice's assertion and the caller's JWT-SVID for an
ap-payables token. POST /mcp is the tool door: initialize, tools/list, and
tools/call. search_policy reads the policy corpus. match_invoice reads the
ledger through a Vault-minted role and revokes it before the tool result returns.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request

import jwt
import psycopg

from gateway.pay import decide
from gateway.retrieve import search
from gateway.tokens import (
    TOKEN_EXCHANGE,
    actor_matches,
    authorize_exchange,
    downscoped_groups,
    issue_obo,
    read_subject_token,
    verify_obo,
)

SOCKET = os.environ.get("SPIRE_SOCKET", "/opt/spire/sockets/agent/api.sock")
VAULT_ADDR = os.environ["VAULT_ADDR"].rstrip("/")
PGHOST = os.environ.get("PGHOST", "postgres")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PGDATABASE = os.environ.get("PGDATABASE", "payables")
IDP_SECRET = os.environ["IDP_JWT_SECRET"]
OBO_SECRET = os.environ["OBO_JWT_SECRET"]
AUDIT_PATH = Path(os.environ.get("AUDIT_PATH", "/opt/app/state/audit.jsonl"))
ROLE_PATH = Path(os.environ.get("ROLE_PATH", "/opt/app/state/last_role.txt"))
JWKS_PATH = Path(os.environ.get("JWKS_PATH", "/opt/app/state/jwks.json"))
CORPUS = Path(os.environ.get("CORPUS_PATH", "/opt/app/corpus/ap"))
TRUST_DOMAIN = os.environ.get("TRUST_DOMAIN", "ap.example")
GATEWAY_AUDIENCE = os.environ.get("GATEWAY_AUDIENCE", "pay-gateway")
RESOURCE_AUDIENCE = os.environ.get("RESOURCE_AUDIENCE", "ap-payables")
VAULT_AUDIENCE = os.environ.get("VAULT_AUDIENCE", "vault.ap.example")
GATEWAY_ID = os.environ.get("GATEWAY_SPIFFE_ID", f"spiffe://{TRUST_DOMAIN}/workload/pay-gateway")
GROUP = "accounts-payable"
ALLOWED_GROUPS = {GROUP}
ALLOWED = {f"spiffe://{TRUST_DOMAIN}/workload/ap-agent"}

MATCH_SQL = """
SELECT i.id AS invoice_id,
       v.name AS vendor,
       v.status AS vendor_status,
       i.po_id AS po_id,
       i.amount AS amount,
       p.remaining AS po_remaining
FROM invoices i
JOIN vendors v ON v.id = i.vendor_id
JOIN purchase_orders p ON p.id = i.po_id
WHERE i.id = %s
"""

TOOLS = [
    {
        "name": "search_policy",
        "description": (
            "Retrieve accounts-payable policy clauses. Use this for the payment rule. "
            "It does not return invoice amounts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "match_invoice",
        "description": (
            "Read one invoice, its vendor, and its purchase order from the ledger, "
            "and say whether it is payable. The ledger is the source of the amounts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "integer"}},
            "required": ["invoice_id"],
        },
    },
]

_keys: dict[str, jwt.PyJWK] = {}


def load_keys() -> None:
    doc = json.loads(JWKS_PATH.read_text())
    _keys.clear()
    for entry in doc.get("keys", []):
        if entry.get("use") == "jwt-svid":
            _keys[entry["kid"]] = jwt.PyJWK(entry)


def audit(record: dict) -> None:
    record["ts"] = time.time()
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_PATH.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def fetch_svid(audience: str) -> str:
    proc = subprocess.run(
        ["spire-agent", "api", "fetch", "jwt", "-audience", audience, "-socketPath", SOCKET, "-output", "json"],
        check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "workload api failed").strip()
        raise RuntimeError(detail)
    doc = json.loads(proc.stdout)
    parts = doc if isinstance(doc, list) else [doc]
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("svid"), str):
            return part["svid"]
        items = part.get("svids")
        if isinstance(items, list) and items:
            token = items[0].get("svid") or items[0].get("token")
            if token:
                return token
    raise RuntimeError(f"could not read an SVID from: {proc.stdout[:240]}")


def verify_workload(token: str) -> tuple[str | None, str | None]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        return None, f"malformed workload token: {exc}"
    kid = header.get("kid")
    if kid not in _keys:
        load_keys()
    if kid not in _keys:
        return None, f"unknown signing key {kid!r}"
    try:
        claims = jwt.decode(
            token, key=_keys[kid], algorithms=["ES256", "ES384", "RS256", "PS256"],
            audience=GATEWAY_AUDIENCE, options={"require": ["sub", "aud", "exp"]},
        )
    except jwt.InvalidAudienceError:
        unverified = jwt.decode(token, options={"verify_signature": False})
        return unverified.get("sub"), f"audience {unverified.get('aud')} is not {GATEWAY_AUDIENCE!r}"
    except jwt.ExpiredSignatureError:
        return None, "workload token expired"
    except jwt.PyJWTError as exc:
        return None, f"invalid workload token: {exc}"
    sub = str(claims["sub"])
    if not sub.startswith(f"spiffe://{TRUST_DOMAIN}/"):
        return sub, f"{sub} is outside trust domain {TRUST_DOMAIN}"
    if sub not in ALLOWED:
        return sub, f"{sub} is not on the tool allowlist"
    return sub, None


def vault_login(svid: str) -> str:
    body = json.dumps({"role": "pay-gateway", "jwt": svid}).encode()
    req = request.Request(f"{VAULT_ADDR}/v1/auth/jwt/login", data=body, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=5) as resp:
            doc = json.loads(resp.read().decode())
    except error.HTTPError as exc:
        raise RuntimeError(f"vault login failed: {exc.read().decode(errors='replace')}") from exc
    return doc["auth"]["client_token"]


def vault_creds(token: str) -> tuple[str, str, str]:
    req = request.Request(f"{VAULT_ADDR}/v1/database/creds/ap-invoice-match", headers={"X-Vault-Token": token})
    try:
        with request.urlopen(req, timeout=8) as resp:
            doc = json.loads(resp.read().decode())
    except error.HTTPError as exc:
        raise RuntimeError(f"vault creds failed: {exc.read().decode(errors='replace')}") from exc
    return doc["data"]["username"], doc["data"]["password"], doc["lease_id"]


def vault_revoke(token: str, lease_id: str) -> None:
    body = json.dumps({"lease_id": lease_id}).encode()
    req = request.Request(
        f"{VAULT_ADDR}/v1/sys/leases/revoke", data=body,
        headers={"X-Vault-Token": token, "Content-Type": "application/json"}, method="PUT",
    )
    try:
        with request.urlopen(req, timeout=8) as resp:
            resp.read()
    except error.HTTPError as exc:
        raise RuntimeError(f"revoke failed: {exc.read().decode(errors='replace')}") from exc


def ledger_row(username: str, password: str, invoice_id: int, person: str) -> tuple[dict | None, str]:
    with psycopg.connect(
        host=PGHOST, port=PGPORT, dbname=PGDATABASE, user=username, password=password,
        connect_timeout=5, options="-c statement_timeout=3000",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('application_name', %s, false)", (person,))
            seen = cur.fetchone()
            application_name = "" if seen is None else str(seen[0])
            cur.execute(MATCH_SQL, (invoice_id,))
            row = cur.fetchone()
            if row is None:
                return None, application_name
            cols = [desc.name for desc in cur.description]
            item = {}
            for key, value in zip(cols, row):
                item[key] = str(value) if isinstance(value, Decimal) else value
            return item, application_name


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/oauth/token":
            self._exchange()
            return
        if self.path == "/mcp":
            self._mcp()
            return
        self._send(404, {"error": "not found"})

    def _exchange(self) -> None:
        payload, err = self._body()
        if err or payload is None:
            self._send(400, {"error": "invalid_request", "error_description": err or "body is not json"})
            return
        if payload.get("grant_type") != TOKEN_EXCHANGE:
            self._send(400, {"error": "unsupported_grant_type", "error_description": "grant_type must be token exchange"})
            return
        subject_token = str(payload.get("subject_token") or "")
        actor_token = str(payload.get("actor_token") or "")
        requested = str(payload.get("audience") or "")
        if not subject_token or not actor_token or not requested:
            self._send(400, {"error": "invalid_request", "error_description": "subject_token, actor_token, and audience are required"})
            return
        claims, subject_err = read_subject_token(subject_token, IDP_SECRET, OBO_SECRET, GATEWAY_AUDIENCE)
        if subject_err or claims is None:
            self._send(400, {"error": "invalid_grant", "error_description": subject_err or "invalid subject token"})
            return
        actor, work_err = verify_workload(actor_token)
        if work_err or not actor:
            self._send(400, {"error": "invalid_grant", "error_description": work_err or "invalid actor token"})
            return
        reason = authorize_exchange(
            claims, actor, requested,
            allowed_groups=ALLOWED_GROUPS, allowed_actors=ALLOWED,
            resource_audience=RESOURCE_AUDIENCE, group_name=GROUP,
        )
        if reason:
            print(f"exchange refused: {reason}", flush=True)
            self._send(400, {"error": "invalid_grant", "error_description": reason})
            return
        token = issue_obo(str(claims["sub"]), actor, downscoped_groups(claims, ALLOWED_GROUPS), RESOURCE_AUDIENCE, OBO_SECRET)
        print(f"exchanged sub={claims['sub']} act={actor} aud={RESOURCE_AUDIENCE}", flush=True)
        self._send(200, {
            "access_token": token,
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "token_type": "Bearer",
            "expires_in": 120,
        })

    def _mcp(self) -> None:
        payload, err = self._body()
        if err or payload is None:
            self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": err or "body is not json"}})
            return
        method = str(payload.get("method") or "")
        msg_id = payload.get("id")
        if method == "initialize":
            self._send(200, {"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ap-payables", "version": "0.1.0"},
            }})
            return
        if method not in {"tools/list", "tools/call"}:
            self._send(200, {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method: {method}"}})
            return
        obo, actor, auth_err = self._tool_auth()
        if auth_err or obo is None or actor is None:
            self._finish_block(None if obo is None else str(obo.get("sub")), actor, auth_err or "not an on-behalf-of token", False)
            self._send(403, {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32003, "message": auth_err or "not an on-behalf-of token"}})
            return
        subject = str(obo["sub"])
        if method == "tools/list":
            self._send(200, {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
            return
        params = payload.get("params") or {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if name == "search_policy":
            self._search(msg_id, subject, actor, arguments)
            return
        if name == "match_invoice":
            self._match(msg_id, subject, actor, arguments)
            return
        self._finish_block(subject, actor, f"unknown tool: {name}", False)
        self._send(200, {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True,
        }})

    def _tool_auth(self) -> tuple[dict | None, str | None, str | None]:
        header = self.headers.get("Authorization", "")
        workload = self.headers.get("X-Workload-SVID", "")
        if not header.startswith("Bearer ") or not workload:
            return None, None, "on-behalf-of token and workload SVID are both required"
        obo, obo_err = verify_obo(header.removeprefix("Bearer ").strip(), OBO_SECRET, RESOURCE_AUDIENCE, GROUP)
        if obo_err or obo is None:
            return None, None, obo_err or "not an on-behalf-of token"
        actor, work_err = verify_workload(workload.strip())
        if work_err or not actor:
            return obo, actor, work_err or "invalid workload token"
        mismatch = actor_matches(obo, actor)
        if mismatch:
            return obo, actor, mismatch
        return obo, actor, None

    def _search(self, msg_id, subject: str, actor: str, arguments: dict) -> None:
        query = str(arguments.get("query") or "").strip()
        if not query or len(query) > 500:
            self._finish_block(subject, actor, "query is required", False)
            self._tool_error(msg_id, "query is required")
            return
        passages = search(CORPUS, query)
        body: dict = {"passages": passages}
        if not passages:
            body["reason"] = "no accounts-payable passage is close enough. This corpus does not include payroll."
        audit({"subject": subject, "actor": actor, "gateway": GATEWAY_ID, "tool": "search_policy", "decision": "allow", "vault_login": False, "lease_id": "", "row_count": len(passages)})
        print(f"search_policy passages={len(passages)} on behalf of {subject}", flush=True)
        self._tool_ok(msg_id, body)

    def _match(self, msg_id, subject: str, actor: str, arguments: dict) -> None:
        invoice_id = arguments.get("invoice_id")
        if not isinstance(invoice_id, int) or isinstance(invoice_id, bool) or invoice_id < 1:
            self._finish_block(subject, actor, "invoice_id must be an integer", False)
            self._tool_error(msg_id, "invoice_id must be an integer")
            return
        username = ""
        lease_id = ""
        try:
            svid = fetch_svid(VAULT_AUDIENCE)
            unverified = jwt.decode(svid, options={"verify_signature": False})
            if unverified.get("sub") != GATEWAY_ID:
                self._finish_block(subject, actor, f"gateway SVID is {unverified.get('sub')}", False)
                self._tool_error(msg_id, f"gateway SVID is {unverified.get('sub')}")
                return
            token = vault_login(svid)
            username, password, lease_id = vault_creds(token)
            row, application_name = ledger_row(username, password, invoice_id, subject)
            vault_revoke(token, lease_id)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)
            if username:
                ROLE_PATH.write_text(username + "\n")
            audit({"subject": subject, "actor": actor, "gateway": GATEWAY_ID, "tool": "match_invoice", "decision": "error", "reason": reason, "vault_login": True, "lease_id": lease_id, "row_count": 0})
            print(f"error: {reason}", flush=True)
            self._tool_error(msg_id, reason, status=500)
            return
        if application_name != subject:
            self._tool_error(msg_id, "database session was not stamped with the person", status=500)
            return
        if row is None:
            ROLE_PATH.write_text(username + "\n")
            audit({"subject": subject, "actor": actor, "gateway": GATEWAY_ID, "tool": "match_invoice", "decision": "allow", "vault_login": True, "lease_id": lease_id, "row_count": 0})
            print(f"match_invoice {invoice_id} not found; minted role {username}; lease revoked", flush=True)
            self._tool_ok(msg_id, {"invoice_id": invoice_id, "found": False, "on_behalf_of": subject, "db_application_name": application_name})
            return
        decision = decide(row)
        decision["found"] = True
        decision["on_behalf_of"] = subject
        decision["db_application_name"] = application_name
        encoded = json.dumps(decision)
        if any(word in encoded for word in (username, lease_id, "password")):
            self._tool_error(msg_id, "response included a secret", status=500)
            return
        ROLE_PATH.write_text(username + "\n")
        audit({"subject": subject, "actor": actor, "gateway": GATEWAY_ID, "tool": "match_invoice", "decision": "allow", "payable": decision["payable"], "vault_login": True, "lease_id": lease_id, "row_count": 1})
        print(f"match_invoice {invoice_id} payable={decision['payable']} on behalf of {subject}; minted role {username}; lease revoked", flush=True)
        self._tool_ok(msg_id, decision)

    def _finish_block(self, subject: str | None, actor: str | None, reason: str, vault_login: bool) -> None:
        audit({"subject": subject, "actor": actor, "gateway": GATEWAY_ID, "tool": "", "decision": "block", "reason": reason, "vault_login": vault_login, "lease_id": "", "row_count": 0})
        print(f"block: {reason}", flush=True)

    def _tool_ok(self, msg_id, payload: dict) -> None:
        text = json.dumps(payload)
        self._send(200, {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}})

    def _tool_error(self, msg_id, message: str, status: int = 200) -> None:
        self._send(status, {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": message}], "isError": True}})

    def _body(self) -> tuple[dict | None, str | None]:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode() or "{}")
        except json.JSONDecodeError:
            return None, "body is not json"
        if not isinstance(payload, dict):
            return None, "body is not a json object"
        return payload, None

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    load_keys()
    print(f"ap payables on :9090  corpus={CORPUS}  audience={RESOURCE_AUDIENCE}  jwt keys={sorted(_keys)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9090), Handler).serve_forever()


if __name__ == "__main__":
    main()
