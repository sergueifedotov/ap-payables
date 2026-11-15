#!/usr/bin/env python3
"""Accounts-payable agent.

The payment modes ask a local model (Ollama, qwen2.5:7b). The model chooses
the MCP tools. search_policy is vector retrieval. match_invoice is the ledger.
The identity modes stop before the model: the wrong group, the wrong program,
a raw user token, and the agent's own SPIFFE JWT at Vault.

    agent.py <pay|over|blocked|staff|engineering|notes|passthrough|vault-direct>
    agent.py --user NAME [--group GROUP ...] [--audience AUD] --question TEXT
    agent.py --user NAME [--group GROUP ...] --passthrough
    agent.py --vault

A mode is a saved call with a PASS line. The flags are the same call with the
person, the group, and the question written out. The flags print the answer
or the refusal and do not print PASS.

The uid of this process is how SPIRE names the program. call.sh passes that
uid. --audience changes only the audience on the workload JWT. The user
assertion is always minted for pay-gateway.

    --user alice --group accounts-payable --question "Can we pay invoice 4411?"
        Same call as pay. A question that contains "invoice" uses both tools.
    --question "What is staff compensation?"
        Same call as staff. No invoice, so the ledger is not read.
    --user bob --group engineering --question "Can we pay invoice 4411?"
        Same refusal as engineering. Exchange HTTP 400. The model is not called.
    --user alice --question "Can we pay invoice 4411?"
        No --group. The assertion carries no group. Exchange HTTP 400.
    uid 1002 with Alice's flags
        Same refusal as notes. SPIRE issues notes-bot.
    --audience mail-tool added to Alice's flags
        The workload token has the wrong audience. Exchange HTTP 400.
    --user alice --group accounts-payable --passthrough
        Same refusal as passthrough. No exchange. HTTP 403.
    --vault
        Same refusal as vault-direct. This process posts its SPIFFE JWT to Vault.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from urllib import error, request

from gateway.tokens import mint_user_assertion

SOCKET = os.environ.get("SPIRE_SOCKET", "/opt/spire/sockets/agent/api.sock")
VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://vault:8200").rstrip("/")
GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:9090")
IDP_SECRET = os.environ.get("IDP_JWT_SECRET", "ap-idp-hmac-key-not-for-production-use")
GATEWAY_AUDIENCE = os.environ.get("GATEWAY_AUDIENCE", "pay-gateway")
RESOURCE_AUDIENCE = os.environ.get("RESOURCE_AUDIENCE", "ap-payables")
VAULT_AUDIENCE = os.environ.get("VAULT_AUDIENCE", "vault.ap.example")
OLLAMA = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
CHAT_MODEL = os.environ.get("OLLAMA_CHAT_MODEL", "qwen2.5:7b")
QUESTION = {
    "pay": "Can we pay invoice 4411?",
    "over": "Can we pay invoice 4412?",
    "blocked": "Can we pay invoice 4413?",
    "staff": "What is staff compensation?",
}
STAFF_SYSTEM = (
    "You are an accounts payable clerk. This question is not about an invoice. "
    "Call search_policy with the user's question unchanged. Do not call match_invoice. "
    "If passages is empty, say the question is outside accounts payable and this corpus does not include payroll. "
    "Do not state a compensation figure."
)
MODEL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": "Retrieve accounts-payable passages about paying a vendor invoice. It has no staff-pay documents. A question outside that subject returns no passages. It does not return amounts.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "match_invoice",
            "description": "Read one invoice from the ledger and whether it is payable. Amounts come only from this tool.",
            "parameters": {
                "type": "object",
                "properties": {"invoice_id": {"type": "integer"}},
                "required": ["invoice_id"],
            },
        },
    },
]
SYSTEM = (
    "You are an accounts payable clerk. Decide whether the invoice in the question can be paid. "
    "Call search_policy before you state the rule. Call match_invoice with the invoice id before you state any amount. "
    "Then answer in two or three sentences. Quote the policy passage you were given. "
    "Use only amounts, the vendor, and the payable flag from match_invoice. "
    "If payable is false, start with 'Do not pay'. If payable is true, start with 'Pay invoice'."
)

INVOICE = {"pay": 4411, "over": 4412, "blocked": 4413, "engineering": 4411, "notes": 4411, "passthrough": 4411}
EXPECTED_UID = {name: (1002 if name == "notes" else 1001) for name in (*INVOICE, "staff", "vault-direct")}


def fetch_svid(audience: str) -> tuple[str, str]:
    proc = subprocess.run(
        ["spire-agent", "api", "fetch", "jwt", "-audience", audience, "-socketPath", SOCKET, "-output", "json"],
        check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "workload api failed").strip()
        raise SystemExit(f"SPIRE refused uid {os.getuid()}: {detail}")
    doc = json.loads(proc.stdout)
    parts = doc if isinstance(doc, list) else [doc]
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("svid"), str):
            return part.get("spiffe_id", ""), part["svid"]
        items = part.get("svids")
        if isinstance(items, list) and items:
            item = items[0]
            token = item.get("svid") or item.get("token")
            if token:
                return item.get("spiffe_id", ""), token
    raise RuntimeError(f"could not read an SVID from: {proc.stdout[:240]}")


def post_json(path: str, payload: dict, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    req = request.Request(
        f"{GATEWAY}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error_description": raw, "message": raw}


def exchange(subject_token: str, actor_token: str) -> tuple[int, dict]:
    return post_json("/oauth/token", {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "actor_token": actor_token,
        "actor_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "audience": RESOURCE_AUDIENCE,
    })


def mcp(method: str, token: str, svid: str, params: dict | None = None, msg_id: int = 1) -> tuple[int, dict]:
    body: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    return post_json("/mcp", body, {"Authorization": f"Bearer {token}", "X-Workload-SVID": svid})


def tool_payload(doc: dict) -> dict:
    if "error" in doc:
        return {"error": doc["error"].get("message", "")}
    result = doc.get("result") or {}
    if result.get("isError"):
        return {"error": result["content"][0]["text"]}
    content = result.get("content") or []
    if content and isinstance(content[0].get("text"), str) and content[0]["text"].startswith("{"):
        return json.loads(content[0]["text"])
    return result


def chat(messages: list[dict]) -> dict:
    body = json.dumps({
        "model": CHAT_MODEL,
        "messages": messages,
        "tools": MODEL_TOOLS,
        "stream": False,
        "options": {"temperature": 0},
    }).encode()
    req = request.Request(f"{OLLAMA}/api/chat", data=body, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except error.URLError as exc:
        raise SystemExit(f"Ollama chat failed at {OLLAMA}: {exc}") from exc


def arguments_of(call: dict) -> dict:
    function = call.get("function") or {}
    raw = function.get("arguments") or {}
    if isinstance(raw, str):
        raw = json.loads(raw or "{}")
    return raw if isinstance(raw, dict) else {}


def _contradicts(text: str, match: dict) -> bool:
    lowered = text.lower()
    if match.get("payable") is True:
        return lowered.startswith("do not pay")
    if match.get("payable") is False:
        return not lowered.startswith("do not pay")
    return False


def run_model(question: str, token: str, svid: str, system: str = SYSTEM, required: tuple[str, ...] = ("search_policy", "match_invoice")) -> tuple[str, dict, list[str], dict]:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
    called: list[str] = []
    match: dict = {}
    policy: dict = {}
    corrected = False
    for turn in range(1, 6):
        doc = chat(messages)
        message = doc.get("message") or {}
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            text = str(message.get("content") or "").strip()
            missing = [name for name in required if name not in called]
            if missing and turn < 5:
                messages.append({
                    "role": "user",
                    "content": (
                        "Call " + " and ".join(missing)
                        + " before you answer. Quote the policy passage and the ledger vendor and amount."
                    ),
                })
                continue
            if required == ("search_policy",) and policy.get("passages") and not corrected:
                corrected = True
                messages.append({
                    "role": "user",
                    "content": f"Those passages are accounts payable. Search once more with this exact query: {question}",
                })
                continue
            if match and not corrected and _contradicts(text, match):
                corrected = True
                flag = "true" if match.get("payable") else "false"
                messages.append({
                    "role": "user",
                    "content": (
                        f"match_invoice says payable is {flag}. "
                        "If it is false, start with 'Do not pay' and give the tool's reason. "
                        "If it is true, start with 'Pay invoice' and use the ledger amount."
                    ),
                })
                continue
            return text, match, called, policy
        for call in calls:
            name = (call.get("function") or {}).get("name") or ""
            args = arguments_of(call)
            called.append(name)
            print(f"model tool {name} {json.dumps(args)}")
            _, result = mcp("tools/call", token, svid, {"name": name, "arguments": args}, msg_id=turn)
            payload = tool_payload(result)
            if name == "search_policy":
                policy = payload
                sources = [item.get("source") for item in payload.get("passages") or []]
                print(f"retrieved {sources or 'nothing'}")
            if name == "match_invoice" and "error" not in payload:
                match = payload
            messages.append({"role": "tool", "tool_name": name, "content": json.dumps(payload)})
    raise SystemExit("the model did not finish after five turns")


def vault_direct(svid: str) -> tuple[int, str]:
    body = json.dumps({"role": "pay-gateway", "jwt": svid}).encode()
    req = request.Request(f"{VAULT_ADDR}/v1/auth/jwt/login", data=body, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=8) as resp:
            return resp.status, resp.read().decode()
    except error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def clerk_for(question: str) -> tuple[str, tuple[str, ...]]:
    if "invoice" in question.lower():
        return SYSTEM, ("search_policy", "match_invoice")
    return STAFF_SYSTEM, ("search_policy",)


def ask(token: str, svid: str, question: str) -> None:
    mcp("initialize", token, svid, {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ap-agent", "version": "0.1.0"}})
    _, listed = mcp("tools/list", token, svid, msg_id=2)
    names = [item["name"] for item in (listed.get("result") or {}).get("tools", [])]
    print(f"tools {', '.join(names)}")
    system, required = clerk_for(question)
    text, match, _called, policy = run_model(question, token, svid, system, required)
    if required == ("search_policy",):
        print(json.dumps(policy, indent=2))
    else:
        print(json.dumps(match, indent=2))
    print(text)


def run_flags(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="agent.py")
    parser.add_argument("--user")
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--audience", default=GATEWAY_AUDIENCE)
    parser.add_argument("--question")
    parser.add_argument("--passthrough", action="store_true")
    parser.add_argument("--vault", action="store_true")
    opts = parser.parse_args(argv)
    if opts.vault:
        audience = VAULT_AUDIENCE if opts.audience == GATEWAY_AUDIENCE else opts.audience
        spiffe_id, svid = fetch_svid(audience)
        print(f"uid {os.getuid()}: SPIRE issued {spiffe_id} aud={audience}")
        status, body = vault_direct(svid)
        print(body)
        print(f"vault status {status}")
        return
    if not opts.user or (not opts.passthrough and not opts.question):
        parser.error("--user and --question are required, unless --passthrough or --vault")
    groups = ", ".join(opts.group) if opts.group else "(none)"
    spiffe_id, svid = fetch_svid(opts.audience)
    print(f"uid {os.getuid()}: SPIRE issued {spiffe_id} aud={opts.audience}")
    print(f"user {opts.user} groups {groups}")
    subject = mint_user_assertion(opts.user, opts.group, GATEWAY_AUDIENCE, IDP_SECRET)
    if opts.passthrough:
        status, doc = mcp("tools/call", subject, svid, {"name": "match_invoice", "arguments": {"invoice_id": 4411}})
        print(json.dumps(doc, indent=2))
        print(f"http {status}")
        return
    status, exchanged = exchange(subject, svid)
    print(f"exchange http {status}")
    if status != 200 or "access_token" not in exchanged:
        print(json.dumps(exchanged, indent=2))
        return
    ask(exchanged["access_token"], svid, opts.question)


def finish(ok: bool, detail: str) -> None:
    print("PASS" if ok else "FAIL", detail)
    raise SystemExit(0 if ok else 1)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0].startswith("--"):
        run_flags(args)
        return
    mode = args[0] if len(args) == 1 else ""
    if mode not in EXPECTED_UID:
        names = "|".join(EXPECTED_UID)
        raise SystemExit(
            f"usage: agent.py <{names}>\n"
            "       agent.py --user NAME [--group GROUP ...] [--audience AUD] --question TEXT\n"
            "       agent.py --user NAME [--group GROUP ...] --passthrough\n"
            "       agent.py --vault"
        )
    uid = os.getuid()
    if uid != EXPECTED_UID[mode]:
        raise SystemExit(f"mode {mode} must run as uid {EXPECTED_UID[mode]}; this process is {uid}")

    if mode == "vault-direct":
        spiffe_id, svid = fetch_svid(VAULT_AUDIENCE)
        print(f"uid {uid}: SPIRE issued {spiffe_id} aud={VAULT_AUDIENCE}")
        status, body = vault_direct(svid)
        print(body)
        finish(status >= 400 and "sub" in body.lower(), f"vault status {status}")

    audience = GATEWAY_AUDIENCE
    spiffe_id, svid = fetch_svid(audience)
    print(f"uid {uid}: SPIRE issued {spiffe_id} aud={audience}")
    if mode == "engineering":
        subject = mint_user_assertion("bob", ["engineering"], GATEWAY_AUDIENCE, IDP_SECRET)
        print("user bob groups engineering")
    else:
        subject = mint_user_assertion("alice", ["accounts-payable"], GATEWAY_AUDIENCE, IDP_SECRET)
        print("user alice groups accounts-payable")

    if mode == "passthrough":
        status, doc = mcp("tools/call", subject, svid, {"name": "match_invoice", "arguments": {"invoice_id": 4411}})
        print(json.dumps(doc, indent=2))
        message = (doc.get("error") or {}).get("message", "")
        finish(status == 403 and "on-behalf-of" in message, f"http {status}")

    status, exchanged = exchange(subject, svid)
    print(f"exchange http {status}")
    if mode in {"engineering", "notes"}:
        print(json.dumps(exchanged, indent=2))
        description = exchanged.get("error_description", "")
        if mode == "engineering":
            finish(status == 400 and "accounts-payable" in description, f"http {status}")
        finish(status == 400 and "allowlist" in description, f"http {status}")
    if status != 200 or "access_token" not in exchanged:
        print(json.dumps(exchanged, indent=2))
        finish(False, "exchange did not issue a token")

    token = exchanged["access_token"]
    mcp("initialize", token, svid, {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ap-agent", "version": "0.1.0"}})
    _, listed = mcp("tools/list", token, svid, msg_id=2)
    names = [item["name"] for item in (listed.get("result") or {}).get("tools", [])]
    print(f"tools {', '.join(names)}")
    if mode == "staff":
        text, _match, called, policy = run_model(QUESTION[mode], token, svid, STAFF_SYSTEM, ("search_policy",))
        print(json.dumps(policy, indent=2))
        print(text)
        passages = policy.get("passages") or []
        lowered = text.lower()
        ok = (
            "search_policy" in called and not passages
            and "compensation figures" not in lowered
            and ("outside" in lowered or "payroll" in lowered)
            and "password" not in lowered
        )
        finish(ok, "staff compensation")

    text, match, called, _policy = run_model(QUESTION[mode], token, svid)
    print(json.dumps(match, indent=2))
    print(text)
    leaked = "password" in text.lower() or "lease_id" in json.dumps(match)
    used_tools = "search_policy" in called and "match_invoice" in called
    lowered = text.lower()
    if mode == "pay":
        ok = (
            used_tools and match.get("payable") is True and match.get("vendor") == "northwind"
            and str(match.get("amount", "")).split(".")[0] == "9800"
            and match.get("on_behalf_of") == "alice" and match.get("db_application_name") == "alice"
            and "4411" in text and "9800" in lowered.replace(",", "").replace("$", "")
            and ("northwind" in lowered or "vendor" in lowered)
            and "do not pay" not in lowered and not leaked
        )
    elif mode == "over":
        ok = (
            used_tools and match.get("payable") is False and lowered.startswith("do not pay")
            and "4412" in text and "exceed" in lowered and not leaked
        )
    else:
        ok = (
            used_tools and match.get("payable") is False and lowered.startswith("do not pay")
            and "4413" in text and ("approv" in lowered or "block" in lowered) and not leaked
        )
    finish(ok, f"invoice {INVOICE[mode]}")


if __name__ == "__main__":
    main()
