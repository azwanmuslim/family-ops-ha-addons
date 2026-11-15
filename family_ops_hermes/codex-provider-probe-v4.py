#!/usr/bin/env python3
"""One-off Codex response test independent of browser. Prints allowlisted fields only."""
import contextlib
import io
import json
import logging
import re
import signal

MODEL = "gpt-5.3-codex"
MARKER = "HERMES_CODEX_ALT_READY_8B9"
report = {
    "version": 4,
    "provider": "openai-codex",
    "model": MODEL,
    "runtime_resolved": False,
    "api_mode": "unknown",
    "response_present": False,
    "marker_seen": False,
    "response_chars": 0,
    "category": "unknown",
    "http_status": None,
    "exception_class": "none",
}
private = io.StringIO()
handler = logging.StreamHandler(private)
root = logging.getLogger()
old_level = root.level
old_disable = root.manager.disable
root.addHandler(handler)
root.setLevel(logging.WARNING)
logging.disable(logging.NOTSET)

def classify(value):
    s = str(value).lower()
    for name, regex in (
        ("oauth", r"invalid.grant|unauthorized|authentication.failed|invalid.token|no.credentials|not.authenticated|login.required|auth.*missing|refresh.token"),
        ("quota", r"rate.limit|quota|too.many.requests|resource.exhausted"),
        ("model", r"unsupported.model|model.not.found|model.*not.available|model.*not.supported|invalid.model"),
        ("network", r"connection.error|connection.failed|dns.fail|tls.error|ssl.error|timed.out|connection refused"),
        ("parse", r"parse.error|response.*invalid|incomplete.*response|no output|empty response"),
    ):
        if re.search(regex, s):
            return name
    return "unknown"

def on_alarm(_sig, _frame):
    raise TimeoutError("Codex probe timeout")

try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from hermes_cli.runtime_provider import resolve_runtime_provider
        rt = resolve_runtime_provider(requested="openai-codex", target_model=MODEL)
    report["api_mode"] = str(rt.get("api_mode") or "unknown")
    report["runtime_resolved"] = (
        rt.get("provider") == "openai-codex"
        and bool(rt.get("api_key") or rt.get("credential_pool"))
    )
    if report["runtime_resolved"]:
        signal.signal(signal.SIGALRM, on_alarm)
        signal.alarm(70)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                from run_agent import AIAgent
                agent = AIAgent(
                    api_key=rt.get("api_key"), base_url=rt.get("base_url"),
                    provider="openai-codex", api_mode=rt.get("api_mode"),
                    model=MODEL, credential_pool=rt.get("credential_pool"),
                    enabled_toolsets=[], disabled_toolsets=["all"],
                    max_iterations=1, quiet_mode=True, platform="cli",
                )
                response = agent.chat(
                    "Reply with exactly " + MARKER + " and nothing else. Do not use tools."
                )
            response = response if isinstance(response, str) else ""
            report["response_present"] = bool(response.strip())
            report["response_chars"] = min(len(response), 100000)
            report["marker_seen"] = MARKER in response
            report["category"] = (
                "none" if report["marker_seen"]
                else "response_other" if report["response_present"]
                else "empty"
            )
        finally:
            signal.alarm(0)
    else:
        report["category"] = "oauth"
except Exception as exc:
    cls = type(exc).__name__
    allow_classes = {
        "AuthenticationError", "PermissionDeniedError", "NotFoundError",
        "BadRequestError", "RateLimitError", "APIConnectionError",
        "APIStatusError", "TimeoutError", "RuntimeError", "ValueError",
    }
    report["exception_class"] = cls if cls in allow_classes else "other"
    report["category"] = "timeout" if isinstance(exc, TimeoutError) else classify(str(exc))
finally:
    root.removeHandler(handler)
    root.setLevel(old_level)
    logging.disable(old_disable)

error_text = private.getvalue()
if report["category"] in ("unknown", "empty", "response_other"):
    classified = classify(error_text)
    if classified != "unknown":
        report["category"] = classified
# Only expose numeric HTTP status, not response bodies or headers.
code_match = re.search(
    r"(?:status(?:_code)?|HTTP(?: error)?)[^0-9]{0,16}(4\d\d|5\d\d)",
    error_text, re.I
)
if code_match:
    report["http_status"] = int(code_match.group(1))
if report["category"] not in {
    "none", "unknown", "oauth", "quota", "model", "network", "parse",
    "timeout", "empty", "response_other",
}:
    report["category"] = "unknown"
if report["api_mode"] not in {"codex_responses", "chat_completions", "unknown"}:
    report["api_mode"] = "unknown"
print("HERMES_CODEX_PROVIDER_V4: " + json.dumps(report, sort_keys=True, separators=(",", ":")), flush=True)
