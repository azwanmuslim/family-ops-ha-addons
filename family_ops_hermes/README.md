# Family Ops Hermes Personal AI (Codex)

Custom Home Assistant OS app, adapted from [diegaccio/ha-hermes-addon](https://github.com/diegaccio/ha-hermes-addon) for personal use with Codex OAuth. Hermes upstream image is pinned to `nousresearch/hermes-agent:v2026.5.7`.

## Purpose

- Keep `model.provider: openai-codex` and `model.default: gpt-5.3-codex` across app restarts. No OpenAI API key required.
- Store OAuth under this app's isolated persistent `/data`, not the existing GitHub runner.
- Never configure Hermes' Telegram gateway against the Home Assistant bot token: HA owns the incoming updates. Future connection must be implemented via a deliberate HA event/agent bridge.
- HA access privileges and host-network access are disabled by default.
- Home Assistant ingress protects the browser terminal; enable it only for device-code login and disable afterward.
- This is a personal fork; rebase/re-audit before any upstream upgrade.

## First login

1. Start the app from Home Assistant Settings > Add-ons.
2. Open the app ingress > Terminal, run `hermes auth add openai-codex`, follow device URL/code shown in your *own* terminal. Never paste the code/token into GitHub issues or chats.
3. Then run `hermes model`, select ChatGPT / Codex Subscription, choose an available Codex model. Verify `hermes auth list openai-codex` without posting credentials.
4. Restart the app; confirm gateway startup and model routing. Close terminal by setting `enable_terminal: false` afterward.
5. Do not enable Hermes Telegram listener. The existing HA integration stays sole consumer.

For source notes see official [Hermes providers documentation](https://hermes-agent.nousresearch.com/docs/integrations/providers).
