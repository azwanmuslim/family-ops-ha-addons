#!/usr/bin/env python3
"""One-time Codex inference diagnostic on the *local* Hermes container.

Never prints credentials, OAuth labels, upstream errors, or model responses.
Only safe JSON fields with booleans/enums/counts are written to add-on logs.
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys

MODEL = "gpt-5.4"
PROVIDER = "openai-codex"
SENTINEL = "HERMES_DIRECT_CODEX_READY_7A2"
result = {
    "version": 3, "provider": PROVIDER, "model": MODEL,
    "auth_logged_in": False, "auth_check": "unknown",
    "runtime_resolved": False, "runtime_api_mode": "unknown",
    "runtime_has_credentials": False, "inference": "skipped",
    "response_nonempty": False, "response_bytes": 0,
    "exit_code": None, "category": "unknown",
}


def classify(msg):
    s = str(msg).lower()
    checks = (
        ("oauth", r"invalid.grant|unauthorized|no.credentials|not.authenticated|login.required|expired.token|authentication.failed|auth.*missing"),
        ("quota", r"rate.limit|quota|too.many.requests|resource.exhausted"),
        ("model", r"unsupported.model|model.not.found|model.*not.available|model.*not.supported|invalid.model"),
        ("network", r"connection.error|connection.failed|dns.fail|tls.error|ssl.error|timed.out|connection refused"),
        ("cli", r"unrecognized.arguments|invalid.choice|no.such.option|unknown.option|unknown.command"),
    )
    for label, regex in checks:
        if re.search(regex, s):
            return label
    return "unknown"


try:
    # The v2026.5.7 auth API may include an api_key in its result.
    # Never log or serialize it; only use the presence of a usable login.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from hermes_cli.auth import get_codex_auth_status
        auth = get_codex_auth_status()
    result["auth_logged_in"] = bool(auth.get("logged_in")) if isinstance(auth, dict) else False
    result["auth_check"] = "ok"
    if not result["auth_logged_in"]:
        result["category"] = "oauth"
except Exception as exc:
    result["auth_check"] = "error"
    result["category"] = classify(exc)

if result["auth_logged_in"]:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from hermes_cli.runtime_provider import resolve_runtime_provider
            runtime = resolve_runtime_provider(requested=PROVIDER, target_model=MODEL)
        result["runtime_api_mode"] = str(runtime.get("api_mode") or "unknown")
        result["runtime_resolved"] = str(runtime.get("provider") or "").lower() == PROVIDER
        result["runtime_has_credentials"] = bool(runtime.get("api_key") or runtime.get("credential_pool"))
        if not result["runtime_resolved"] or not result["runtime_has_credentials"]:
            result["category"] = "oauth"
    except Exception as exc:
        result["category"] = classify(exc)
        if result["category"] == "unknown":
            result["category"] = "provider"

if result["runtime_resolved"] and result["runtime_has_credentials"]:
    try:
        # Deliberately a single prompt; no shell, filesystem or HA actions requested.
        # This is a one-off protected diagnostic; -z has no interactive input.
        completed = subprocess.run(
            ["/opt/hermes/.venv/bin/hermes", "--provider", PROVIDER,
             "--model", MODEL, "-z",
             "Reply with the exact text " + SENTINEL + ". No tools or other actions."],
            text=True, capture_output=True, timeout=75, check=False,
            env={**os.environ, "HERMES_YOLO_MODE": "0"},
        )
        output = completed.stdout or ""
        stderr = completed.stderr or ""
        result["exit_code"] = int(completed.returncode)
        result["response_bytes"] = min(len(output.encode("utf-8")), 1000000)
        result["response_nonempty"] = bool(output.strip())
        if SENTINEL in output and completed.returncode == 0:
            result["inference"] = "pass"
            result["category"] = "none"
        elif output.strip():
            result["inference"] = "nonempty_unverified"
            result["category"] = classify(output + "\n" + stderr)
        else:
            result["inference"] = "empty"
            result["category"] = classify(stderr)
    except subprocess.TimeoutExpired:
        result["inference"] = "timeout"
        result["category"] = "timeout"
    except Exception as exc:
        result["inference"] = "exception"
        result["category"] = classify(exc)

# Fail closed if an internal unexpected enum appears.
for key, allowed in (
    ("category", {"none","unknown","oauth","provider","model","network","cli","quota","timeout"}),
    ("inference", {"skipped","pass","nonempty_unverified","empty","timeout","exception"}),
    ("auth_check", {"ok","error","unknown"}),
):
    if result[key] not in allowed:
        result[key] = "unknown"

print("HERMES_CODEX_DIRECT_V3: " + json.dumps(result, sort_keys=True, separators=(",",":")), flush=True)
