#!/usr/bin/env python3
"""Safe one-time live Codex model-catalog verification and single response test.

Never prints OAuth tokens, account IDs, raw error messages or model responses.
"""
import contextlib
import io
import json
import logging
import re
import signal

MARKER = "HERMES_CODEX_CATALOG_READY_26B"
CATALOG = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
report = {
    "version": 5, "provider": "openai-codex",
    "runtime_resolved": False, "catalog_http_status": None,
    "catalog_model_count": 0, "configured_gpt54_listed": False,
    "fallback_gpt53_listed": False, "selected_model": "none",
    "inference": "not_tested", "response_nonempty": False,
    "marker_seen": False, "response_chars": 0,
    "category": "unknown", "inference_http_status": None,
}
private = io.StringIO()
root = logging.getLogger()
handler = logging.StreamHandler(private)
prev_level, prev_disable = root.level, root.manager.disable
root.addHandler(handler)
root.setLevel(logging.WARNING)
logging.disable(logging.NOTSET)

def classify(value):
    s = str(value).lower()
    for name, pattern in (
        ("oauth", r"invalid.grant|unauthorized|auth.*fail|invalid.token|no.credentials|not.authenticated|login.required|auth.*missing"),
        ("quota", r"rate.limit|quota|too.many.requests|resource.exhausted"),
        ("model", r"unsupported.model|model.not.found|model.*not.available|model.*not.supported|invalid.model"),
        ("network", r"connection.error|connection.failed|dns.fail|tls.error|ssl.error|timed.out|connection refused"),
        ("response", r"parse.error|response.*invalid|incomplete.*response|no output|empty.response"),
    ):
        if re.search(pattern, s):
            return name
    return "unknown"

def on_alarm(_sig, _frame):
    raise TimeoutError("Probe execution timed out")

try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from hermes_cli.runtime_provider import resolve_runtime_provider
        rt = resolve_runtime_provider(requested="openai-codex", target_model="gpt-5.4")
    token = rt.get("api_key") or ""
    report["runtime_resolved"] = rt.get("provider") == "openai-codex" and bool(token)
    if not report["runtime_resolved"]:
        report["category"] = "oauth"
    else:
        import httpx
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.get(CATALOG, headers={"Authorization": "Bearer " + token})
        report["catalog_http_status"] = int(response.status_code)
        if response.status_code != 200:
            report["category"] = (
                "oauth" if response.status_code == 401
                else "access_denied" if response.status_code == 403
                else "quota" if response.status_code == 429
                else "endpoint" if response.status_code == 404
                else "http_failure"
            )
        else:
            payload = response.json()
            items = payload.get("models") if isinstance(payload, dict) else None
            items = items if isinstance(items, list) else []
            allowed = []
            for item in items:
                if not isinstance(item, dict) or item.get("supported_in_api") is False:
                    continue
                if str(item.get("visibility") or "").lower() in ("hide", "hidden"):
                    continue
                slug = item.get("slug")
                if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", slug):
                    continue
                priority = item.get("priority")
                priority = priority if isinstance(priority, (int, float)) else 10000
                allowed.append((priority, slug))
            allowed.sort(key=lambda item: (item[0], item[1]))
            model_ids = [name for _rank, name in allowed]
            report["catalog_model_count"] = len(model_ids)
            report["configured_gpt54_listed"] = "gpt-5.4" in model_ids
            report["fallback_gpt53_listed"] = "gpt-5.3-codex" in model_ids
            if not model_ids:
                report["category"] = "empty_catalog"
            else:
                # Take the API's highest-priority available model; never infer
                # access from local built-in defaults or synthetic aliases.
                selected = model_ids[0]
                report["selected_model"] = selected
                signal.signal(signal.SIGALRM, on_alarm)
                signal.alarm(65)
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        from run_agent import AIAgent
                        agent = AIAgent(
                            api_key=rt.get("api_key"), base_url=rt.get("base_url"),
                            provider="openai-codex", api_mode="codex_responses",
                            model=selected, credential_pool=rt.get("credential_pool"),
                            enabled_toolsets=[], disabled_toolsets=["all"],
                            max_iterations=1, quiet_mode=True, platform="cli",
                        )
                        model_response = agent.chat(
                            "Reply with exactly " + MARKER + ". Do not use tools."
                        )
                    model_response = model_response if isinstance(model_response, str) else ""
                    report["response_nonempty"] = bool(model_response.strip())
                    report["response_chars"] = min(len(model_response), 100000)
                    report["marker_seen"] = MARKER in model_response
                    report["inference"] = (
                        "pass" if report["marker_seen"]
                        else "nonempty_unverified" if report["response_nonempty"]
                        else "empty"
                    )
                    report["category"] = (
                        "none" if report["marker_seen"] else "response_other"
                    )
                finally:
                    signal.alarm(0)
except Exception as exc:
    report["category"] = "timeout" if isinstance(exc, TimeoutError) else classify(str(exc))
    report["inference"] = "exception"
finally:
    root.removeHandler(handler)
    root.setLevel(prev_level)
    logging.disable(prev_disable)

private_errors = private.getvalue()
if report["inference"] in ("empty", "nonempty_unverified", "exception"):
    hint = classify(private_errors)
    if hint != "unknown":
        report["category"] = hint
match = re.search(
    r"(?:status(?:_code)?|HTTP(?: error)?)[^0-9]{0,16}(4\d\d|5\d\d)",
    private_errors, re.I
)
if match:
    report["inference_http_status"] = int(match.group(1))
if report["category"] not in {
    "unknown","none","oauth","quota","model","network","response",
    "access_denied","endpoint","http_failure","empty_catalog",
    "response_other","timeout",
}:
    report["category"] = "unknown"
print("HERMES_CODEX_DISCOVERY_V5: " + json.dumps(report, sort_keys=True, separators=(",", ":")), flush=True)
