#!/usr/bin/env python3
"""Owner-only HA Telegram -> Hermes Codex bridge.

HA remains the ONLY Telegram bot update consumer. This process listens to
Home Assistant events and sends replies through telegram_bot.send_message.
No bot token, OAuth token, chat text, or model response is logged.
"""
import asyncio
import contextlib
import io
import json
import os
import time
from collections import deque
from pathlib import Path

import aiohttp
from ha_home_capabilities import HomeTools
from ha_gate_advisor import GateAdvisor

OWNER_CHAT = os.environ.get("HA_TELEGRAM_CHAT_ID", "").strip()
OWNER_USER = os.environ.get("HA_TELEGRAM_USER_ID", "").strip()
ALLOW_PLAIN = os.environ.get("HA_TELEGRAM_PLAIN_TEXT", "false").lower() == "true"
MODEL = os.environ.get("HA_TELEGRAM_MODEL", "gpt-6.1-sol").strip()
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
WS_URL = "ws://supervisor/core/websocket"
SEND_URL = "http://supervisor/core/api/services/telegram_bot/send_message"
STATE_PATH = Path("/tmp/ha_telegram_bridge_status.json")
HTTP_HEADERS = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
HISTORY = deque(maxlen=8)
SEEN = {}
QUEUE = asyncio.Queue(maxsize=6)
COUNTS = {"received": 0, "accepted": 0, "replied": 0, "errors": 0}


def status(state, error="none"):
    """Only sanitized counters and states; no identifiers or message content."""
    record = {
        "state": state, "error": error, "model": MODEL,
        "chat_configured": bool(OWNER_CHAT), "user_configured": bool(OWNER_USER),
        "plain_text_enabled": ALLOW_PLAIN, **COUNTS,
    }
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(STATE_PATH)


def eligible(event_name, data):
    if not isinstance(data, dict):
        return None
    try:
        if str(int(data.get("chat_id"))) != OWNER_CHAT:
            return None
        if str(int(data.get("user_id"))) != OWNER_USER:
            return None
    except (ValueError, TypeError):
        return None
    if event_name == "telegram_command":
        command = str(data.get("command") or "").split("@", 1)[0].lower()
        allowed = {
            "/ai", "/hermes", "/rumah", "/home", "/statusrumah",
            "/ram", "/cpu", "/prestasi", "/cctv", "/camera", "/gate", "/pagar",
            "/lampu", "/lights", "/peranti", "/hahelp", "/homehelp", "/rumahhelp",
            "/confirm", "/sahkan", "/cancel", "/batal",
            "/entities", "/devices", "/entity", "/entiti"
        }
        if command not in allowed:
            return None
        args = data.get("args") or ""
        text = " ".join(str(v) for v in args) if isinstance(args, list) else str(args)
        text = text.strip()[:1200]
        if command in ("/ai", "/hermes"):
            return text or "/hahelp"
        return command + (" " + text if text else "")
    if event_name == "telegram_text" and ALLOW_PLAIN:
        text = str(data.get("text") or "").strip()
        if text.startswith("/") or not text:
            return None
        return text[:1200]
    return None


def extract_event(item):
    if item.get("type") != "event":
        return None
    payload = item.get("event") or {}
    typ = payload.get("event_type")
    data = payload.get("data") or {}
    if typ == "state_changed":
        if not str(data.get("entity_id") or "").startswith("event."):
            return None
        newer = (data.get("new_state") or {}).get("attributes") or {}
        typ = newer.get("event_type")
        data = newer
    if typ not in ("telegram_command", "telegram_text"):
        return None
    return typ, data, str(payload.get("time_fired") or "")


def deduplicate(typ, data, fired):
    now = time.monotonic()
    for k, ts in list(SEEN.items()):
        if now - ts > 600:
            SEEN.pop(k, None)
    key = (str(data.get("chat_id")), str(data.get("id") or fired), typ)
    if key in SEEN:
        return False
    SEEN[key] = now
    return True


def infer(prompt):
    # Uses the same AIAgent constructor proven to work in Codex catalog v5.
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rt = resolve_runtime_provider(requested="openai-codex", target_model=MODEL)
        if rt.get("provider") != "openai-codex" or not rt.get("api_key"):
            raise RuntimeError("auth_unavailable")
        agent = AIAgent(
            api_key=rt.get("api_key"), base_url=rt.get("base_url"),
            provider="openai-codex", api_mode="codex_responses",
            model=MODEL, credential_pool=rt.get("credential_pool"),
            enabled_toolsets=[], disabled_toolsets=["all"],
            max_iterations=1, quiet_mode=True, platform="cli",
        )
        context = "\n".join(
            ("User: " if role == "user" else "Assistant: ") + msg
            for role, msg in list(HISTORY)
        )
        text = (
            "You are Hermes, a friendly personal assistant replying through an existing "
            "Home Assistant Telegram bot. Reply naturally in the user's language, "
            "usually casual Malay. Keep messages short, clear and useful. "
            "Live Home Assistant queries and confirmation-gated light/fan controls are "
            "performed by an independent local tool router BEFORE this model call. "
            "You have NO permission to call Home Assistant actions directly. Never "
            "invent live device statuses, actions, or claim a device was controlled. "
            "Do not reveal secrets or credentials.\n"
            + ("Previous conversation:\n" + context + "\n" if context else "")
            + "User: " + prompt + "\nAssistant:"
        )
        answer = agent.chat(text)
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("empty_model_reply")
    return answer.strip()


async def send(session, msg):
    # Existing HA bot sends the message; Hermes NEVER polls Telegram directly.
    body = {"chat_id": int(OWNER_CHAT), "parse_mode": "plain_text", "message": msg[:3900]}
    async with session.post(SEND_URL, headers=HTTP_HEADERS, json=body, timeout=aiohttp.ClientTimeout(total=25)) as resp:
        if resp.status >= 300:
            raise RuntimeError("ha_send_failed")
        await resp.read()


async def worker(session, home_tools):
    while True:
        prompt = await QUEUE.get()
        try:
            answer = await home_tools.handle(session, prompt)
            local_ha_response = answer is not None
            if answer is None:
                answer = await asyncio.wait_for(asyncio.to_thread(infer, prompt), timeout=125)
                HISTORY.append(("user", prompt))
                HISTORY.append(("assistant", answer[:2500]))
            await send(session, answer)
            COUNTS["replied"] += 1
            status("connected")
            print("HERMES_HA_TG_BRIDGE: model_reply_sent=true", flush=True)
            if local_ha_response:
                print("HERMES_HA_TG_BRIDGE: ha_tools_reply_sent=true", flush=True)
        except Exception:
            COUNTS["errors"] += 1
            status("connected", "request_failed")
            print("HERMES_HA_TG_BRIDGE: model_request_failed=true", flush=True)
            with contextlib.suppress(Exception):
                await send(session, "Maaf, Hermes belum berjaya jawab. Cuba /ai sekali lagi.")
        finally:
            QUEUE.task_done()


async def listen(session):
    delay = 3
    while True:
        try:
            async with session.ws_connect(WS_URL, heartbeat=30) as ws:
                challenge = await ws.receive_json()
                if challenge.get("type") != "auth_required":
                    raise RuntimeError("invalid_ws_handshake")
                await ws.send_json({"type": "auth", "access_token": TOKEN})
                auth = await ws.receive_json()
                if auth.get("type") != "auth_ok":
                    raise RuntimeError("ha_auth_failed")
                for idx, event_type in enumerate(("telegram_command", "telegram_text", "state_changed"), start=1):
                    await ws.send_json({"id": idx, "type": "subscribe_events", "event_type": event_type})
                delay = 3
                status("connected")
                print("HERMES_HA_TG_BRIDGE: connected", flush=True)
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        break
                    try:
                        data = json.loads(message.data)
                        event = extract_event(data)
                        if event is None:
                            continue
                        typ, attrs, fired = event
                        COUNTS["received"] += 1
                        prompt = eligible(typ, attrs)
                        if not prompt or not deduplicate(typ, attrs, fired):
                            continue
                        COUNTS["accepted"] += 1
                        if QUEUE.full():
                            continue
                        QUEUE.put_nowait(prompt)
                        status("connected")
                    except (ValueError, KeyError, TypeError):
                        continue
            status("reconnecting", "ws_closed")
        except Exception as exc:
            COUNTS["errors"] += 1
            status("reconnecting", "ha_ws_failure")
            name = type(exc).__name__
            if name not in {"TypeError", "ValueError", "TimeoutError", "ClientConnectorError", "ClientResponseError", "WSServerHandshakeError", "RuntimeError", "ClientError"}:
                name = "other"
            print("HERMES_HA_TG_BRIDGE: reconnect_reason=" + name, flush=True)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def main():
    if not TOKEN or not OWNER_CHAT.isdigit() or not OWNER_USER.isdigit():
        status("disabled", "missing_ha_auth_or_owner")
        print("HERMES_HA_TG_BRIDGE: missing_startup_prerequisite=true", flush=True)
        return
    status("starting")
    timeout = aiohttp.ClientTimeout(total=130)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        home_tools = HomeTools(send)
        advisor = GateAdvisor(send)
        home_tools.gate_advisor = advisor
        await asyncio.gather(worker(session, home_tools), listen(session),
                             home_tools.proactive_loop(session),
                             advisor.loop(session))


if __name__ == "__main__":
    asyncio.run(main())
