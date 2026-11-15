# Family Ops Hermes Personal AI (Codex)

Custom Home Assistant OS app, adapted from [diegaccio/ha-hermes-addon](https://github.com/diegaccio/ha-hermes-addon) for personal use with Codex OAuth. Hermes upstream image is pinned to `nousresearch/hermes-agent:v2026.5.7`.

## Purpose

- Keep `model.provider: openai-codex` and `model.default: gpt-6.1-sol` across app restarts. No OpenAI API key required.
- Store OAuth under this app's isolated persistent `/data`, not the existing GitHub runner.
- Never configure Hermes' Telegram gateway against the Home Assistant bot token: HA owns incoming updates. v34 provides an opt-in owner-only event bridge instead.
- The v34 app requests Home Assistant Core API access for its opt-in owner-only bridge; host-network access remains disabled. Treat the injected Supervisor token as an administrator credential.
- Home Assistant ingress protects the browser terminal; enable it only for device-code login and disable afterward.
- This is a personal fork; rebase/re-audit before any upstream upgrade.

## First login

1. Start the app from Home Assistant Settings > Add-ons.
2. Open the app ingress > Terminal, run `hermes auth add openai-codex`, follow device URL/code shown in your *own* terminal. Never paste the code/token into GitHub issues or chats.
3. Then run `hermes model`, select ChatGPT / Codex Subscription, choose an available Codex model. Verify `hermes auth list openai-codex` without posting credentials.
4. Restart the app; confirm gateway startup and model routing. Close terminal by setting `enable_terminal: false` afterward.
5. Do not enable Hermes Telegram listener. The existing HA integration stays sole consumer.

For source notes see official [Hermes providers documentation](https://hermes-agent.nousresearch.com/docs/integrations/providers).

## Telegram bridge (v34)

- Leave `ha_telegram_bridge: false` until HA Telegram routing is verified. The bridge uses HA WebSocket `telegram_command`/`telegram_text` events (and the event entity when available) and `telegram_bot.send_message`; it does not run a second Telegram poller.
- Restrict `ha_telegram_chat_id` and `ha_telegram_user_id` to your owner's private DM. Both must match; never use `ha_telegram_plain_text` until `/ai` is verified.
- `/ai your message` and `/hermes your message` are the explicit chat commands. With `ha_telegram_plain_text: true`, ordinary non-command owner text is also routed to Hermes; `/ping`, `/status`, `/help` are not intercepted.
- Inference reuses the pinned Hermes Agent `AIAgent` with all tools disabled during the initial rollout. No devices or automations can be controlled through this bridge yet; future skills need separate review.
- All local HA and OAuth credentials stay inside add-on storage. The add-on writes sanitized readiness counters to `/tmp/ha_telegram_bridge_status.json`, never messages, raw responses, or tokens to logs.
- Rollback: set `ha_telegram_bridge: false` and restart only this add-on. HA's Telegram integration and existing automations are not altered.

## HA tools and proactive monitor (v34)

- **Read-only**: ask "status rumah", "siapa di rumah", "RAM", "CPU", "CCTV", "gate" or "lampu"; /rumah, /ram, /cctv, /gate and /lampu also work.
- **Control**: only exact-match light/fan ON/OFF, and ALWAYS requires /confirm CODE within 90 seconds. /cancel aborts. No arbitrary Home Assistant services, scripts, covers, locks, alarms, or gate opening are exposed.
- **Proactive**: rate-limited owner Telegram alerts for HA RAM >=85% for 5 minutes, camera entity unavailable/unknown for 10 minutes and a garage gate entity staying open for 10 minutes. Maximum 3 new alerts per day. This never operates physical devices, never publishes messages or tokens in GitHub, and uses one HA state read every 120 seconds.
- **Graceful failure**: no guessed live statuses; when HA API is unavailable the assistant says it cannot read data. Telegram /ping, /status, /help and existing automations remain owned by HA.
- **Safety**: the model does not select service calls or run shell commands. The local policy router checks exact entity domain and name again at the final confirmation step.

## Full Home Assistant entity catalogue (v34)

The owner-only Telegram bridge reads **all currently exposed entity states** live with GET /api/states. No static catalogue: newly added or renamed entities are available automatically. /entities lists domain totals; /entities switch, /entities sensor 2 or /entity switch.doorlamp return current state. Natural BM/English requests such as "Lampu mana menyala?", "Boleh buka lampu pintu luar?" and "Status switch.doorlamp" use the read or confirmation router.

Home lights can be registered as switch.*, not just light.*. The router includes only household light/fan/AC switches with an exact match for control (always requires /confirm). **Never** grant unrestricted dynamic HA service calls to the LLM: gate switches, locks, alarms, CCTV configuration, group/all-switches, scripts, buttons, automation, system actions and unknown/unavailable devices remain read-only. A switch's OFF state is not a verified closed-gate position.

All live reads use the existing Home Assistant API credentials inside the add-on; no credentials/raw attributes are published to GitHub. The AI does not get an arbitrary service-execution tool. Commands remain owner-restricted.
