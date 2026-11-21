# Family Ops Hermes Personal AI (Codex)

Custom Home Assistant OS app, adapted from [diegaccio/ha-hermes-addon](https://github.com/diegaccio/ha-hermes-addon) for personal use with Codex OAuth. Hermes upstream image is pinned to `nousresearch/hermes-agent:v2026.5.7`.

## Purpose

- Keep `model.provider: openai-codex` and `model.default: gpt-6.1-sol` across app restarts. No OpenAI API key required.
- Store OAuth under this app's isolated persistent `/data`, not the existing GitHub runner.
- Never configure Hermes' Telegram gateway against the Home Assistant bot token: HA owns incoming updates. v37 provides an opt-in owner-only event bridge instead.
- The v37 app requests Home Assistant Core API access for its opt-in owner-only bridge; host-network access remains disabled. Treat the injected Supervisor token as an administrator credential.
- Home Assistant ingress protects the browser terminal; enable it only for device-code login and disable afterward.
- This is a personal fork; rebase/re-audit before any upstream upgrade.

## First login

1. Start the app from Home Assistant Settings > Add-ons.
2. Open the app ingress > Terminal, run `hermes auth add openai-codex`, follow device URL/code shown in your *own* terminal. Never paste the code/token into GitHub issues or chats.
3. Then run `hermes model`, select ChatGPT / Codex Subscription, choose an available Codex model. Verify `hermes auth list openai-codex` without posting credentials.
4. Restart the app; confirm gateway startup and model routing. Close terminal by setting `enable_terminal: false` afterward.
5. Do not enable Hermes Telegram listener. The existing HA integration stays sole consumer.

For source notes see official [Hermes providers documentation](https://hermes-agent.nousresearch.com/docs/integrations/providers).

## Telegram bridge (v37)

- Leave `ha_telegram_bridge: false` until HA Telegram routing is verified. The bridge uses HA WebSocket `telegram_command`/`telegram_text` events (and the event entity when available) and `telegram_bot.send_message`; it does not run a second Telegram poller.
- Restrict `ha_telegram_chat_id` and `ha_telegram_user_id` to your owner's private DM. Both must match; never use `ha_telegram_plain_text` until `/ai` is verified.
- `/ai your message` and `/hermes your message` are the explicit chat commands. With `ha_telegram_plain_text: true`, ordinary non-command owner text is also routed to Hermes; `/ping`, `/status`, `/help` are not intercepted.
- Inference reuses the pinned Hermes Agent `AIAgent` with all tools disabled during the initial rollout. No devices or automations can be controlled through this bridge yet; future skills need separate review.
- All local HA and OAuth credentials stay inside add-on storage. The add-on writes sanitized readiness counters to `/tmp/ha_telegram_bridge_status.json`, never messages, raw responses, or tokens to logs.
- Rollback: set `ha_telegram_bridge: false` and restart only this add-on. HA's Telegram integration and existing automations are not altered.

## HA tools and proactive monitor (v37)

- **Read-only**: ask "status rumah", "siapa di rumah", "RAM", "CPU", "CCTV", "gate" or "lampu"; /rumah, /ram, /cctv, /gate and /lampu also work.
- **Control**: only exact-match light/fan ON/OFF, and ALWAYS requires /confirm CODE within 90 seconds. /cancel aborts. No arbitrary Home Assistant services, scripts, covers, locks, alarms, or gate opening are exposed.
- **Proactive**: rate-limited owner Telegram alerts for HA RAM >=85% for 5 minutes, camera entity unavailable/unknown for 10 minutes and a garage gate entity staying open for 10 minutes. Maximum 3 new alerts per day. This never operates physical devices, never publishes messages or tokens in GitHub, and uses one HA state read every 120 seconds.
- **Graceful failure**: no guessed live statuses; when HA API is unavailable the assistant says it cannot read data. Telegram /ping, /status, /help and existing automations remain owned by HA.
- **Safety**: the model does not select service calls or run shell commands. The local policy router checks exact entity domain and name again at the final confirmation step.

## Full Home Assistant entity catalogue (v37)

The owner-only Telegram bridge reads **all currently exposed entity states** live with GET /api/states. No static catalogue: newly added or renamed entities are available automatically. /entities lists domain totals; /entities switch, /entities sensor 2 or /entity switch.doorlamp return current state. Natural BM/English requests such as "Lampu mana menyala?", "Boleh buka lampu pintu luar?" and "Status switch.doorlamp" use the read or confirmation router.

Home lights can be registered as switch.*, not just light.*. The router includes only household light/fan/AC switches with an exact match for control (always requires /confirm). **Never** grant unrestricted dynamic HA service calls to the LLM: gate switches, locks, alarms, CCTV configuration, group/all-switches, scripts, buttons, automation, system actions and unknown/unavailable devices remain read-only. A switch's OFF state is not a verified closed-gate position.

All live reads use the existing Home Assistant API credentials inside the add-on; no credentials/raw attributes are published to GitHub. The AI does not get an arbitrary service-execution tool. Commands remain owner-restricted.

## Simple language confirmation and bilingual room names (v37)

- Hermes normalizes "ruang tamu" = "living room", "lampu" = "lamp", "kipas" = "fan", "bilik utama" = "master bedroom", and other common BM/English synonyms for lookups only. Home Assistant entity IDs and friendly names are NOT renamed.
- "Hidupkan lampu ruang tamu" will list all matching lamps instead of arbitrarily selecting one; reply with a choice number (2-minute validity) to prepare the selected control, then approve with an affirmative.
- "Hidupkan Living Room Lamp 2" resolves the specific switch and asks for confirmation. "Living Room 2 Lamp" remains a separate device.
- Reply `ok`, `yes`, `ya`, `boleh`, `setuju`, `confirm`, `/confirm`, or `teruskan` within 3 minutes to confirm the one pending low-risk light/fan/safe-switch action. The old code is optional; bare /confirm no longer cancels the pending request.
- Wrong codes leave the pending request intact; expired requests, cancellations, other questions, and restart clear it. No affirmative can act when there is no outstanding pending request. Gate, lock, CCTV configuration, alarm and global-all switches remain blocked.

## Telegram numeric reply quality (v37)

- After an ambiguous device list, replies `2`, `No 2`, `no. 2`, `nombor 2`, `pilihan 2`, `#2` and `2.` select the stated number from the original list.
- Multiple numbers in one message do not select a device; Hermes asks for one number while preserving the pending menu.
- An additional number after selecting a device does not cancel the requested confirmation. The user is reminded to say `ok`/`confirm` or `batal`.
- A new explicit named-device command supersedes the old menu; no LLM interpretation or service call is used for menu selection.
- All physical actions still require the separate owner-only positive acknowledgement.

## Gate Vision on-demand snapshot (v43)

- Open the Home Assistant **Hermes Agent > Gate Sample Review** page (ingress-only).
- Press **Snapshot Now** while the gate is visibly OPEN or CLOSED. It uses the fixed local `camera.nvr_car_porch_cam_sub` camera, bypassing the 30-minute routine schedule.
- The new cropped picture appears immediately at the front of the review queue. Confirm **OPEN** or **CLOSED** only after inspecting that actual image. If the gate is moving, obstructed or too dark, use **SKIP**; an unverified photo never becomes training truth.
- Capture requires the authenticated HA ingress and an anti-CSRF token, limits repeats to once per three seconds, and never invokes `switch.bothgate`, motor or Home Assistant control services. Raw image data stays in the add-on; 14-day local retention remains unchanged. Routine 30-minute snapshots continue unaffected.
- After merging this version, refresh the HA add-on store and **update/restart Hermes** to load the new button and backend; GitHub source changes alone do not update a running add-on.

## GPS approach evidence and false-positive suppression (v44)

- A gate-closed suggestion now requires **three distinct, recent GPS fixes** from
  the same family member. Identical HA update timestamps are never re-counted.
- Reject missing accuracy or GPS uncertainty above **50 m**; fixes must be at
  most **5 minutes old**. Two successive decreases must each be at least
  **60 m**, with at least **150 m total reduction**, over 2-15 minutes.
  The final position must be approximately 120-800 m from Home.
- `NEAR_HOME`, `STATIONARY`, and `APPROACHING` are kept as local
  diagnostic classifications. Near home alone does **not** send an alert.
- The owner's Telegram suggestion includes estimated straight-line distances,
  elapsed time, and GPS accuracy. Raw coordinates are not exported or logged.
- This remains a suggestion-only POC: no gate service, pulse, or automation
  is permitted. After merging, update/restart the Hermes HA add-on to v44;
  a GitHub commit does not change an already running add-on.


## Gate Vision local validation and diagnostic (v45)

- The Learning percentage measures labelled coverage, **not model accuracy**.
- Use a class-balanced, newest-25%-held-out local evaluation from owner-confirmed OPEN and CLOSED images (at most 36/class); validation frames are never used as model references.
- Evaluate both original entrance crop and a focused upper-gate-band candidate reconstructed **locally from existing retained labelled crops**; choose a model only after at least 90% overall and 80% per-class validation at conservative similarity/margin thresholds.
- Model status is COLLECTING, EVALUATION_FAILED, or READY_POC. READY_POC is *not* field-tested safety certification and does not enable gate actuation. Poor frames remain UNKNOWN.
- Publish sanitized evaluation accuracy and UNKNOWN reasons to Home Assistant sensors, with model selection, without uploading raw images, vectors, coordinates or device secrets.
- When the labelled local dataset is enabled, do not bypass failed evaluation with the old two-reference template. Night remains UNKNOWN.
- The 14-day photo retention, review queue, snapshot-now and suggestions-only motor restriction are unchanged.


## Runtime fix (v46)

- Restore the staticmethod binding for `_presence`. A missing decorator in v45 caused a runtime TypeError in `check_once` after a valid camera read; this stopped virtual gate sensor refresh. Add an instance-level regression test. No change to gate motor restrictions.


## Gate Shape Structure Benchmark (v47, non-actuating)

- Add Pillow-only structural line-gradient candidates (`edge_gate`, `edge_center`, `edge_luma`) rebuilt locally from the already-labelled, retained gate crops. No raw CCTV data, vectors or locations leave the add-on.
- Compare OPEN and CLOSED recall independently with time-ordered held-out frames; prefer holding out a *whole 15-minute-separated gate event* when sufficient distinct events exist. Report event diversity, held-out accuracy, per-class recall, evaluation split and whether a candidate is eligible for later promotion.
- Publish only sanitized aggregate metrics in `sensor.hermes_gate_shape_accuracy`. All candidates remain **dry-run only**; original validated classifier, gate recommendations, Telegram and physical controls are not changed by this benchmark. `ready_to_promote` is only a code-development signal, never permission for motor control.
- If no distinct opening event has been captured for validation, status `EXPLORATORY_ONLY` is intentional even if the percentage looks high. Real field validation is still required.


## v48 data-driven gate-region benchmark (no model promotion)

- Fix validation leakage: hold out the final quarter of **15-minute-separated independent OPEN and CLOSED events**, instead of withholding a few consecutive frames from one event. CLOSED references are spread across time to cover different light conditions.
- Compare six dry-run classifiers. Three v47 line-gradient candidates and three v48 pixel-mask candidates. v48 learns the most discriminative brightness or edge positions from **training events only**, taking class variance into account, and evaluates on completely withheld events.
- Report **balanced accuracy** (average of OPEN and CLOSED recall) and per-class metrics, plus aggregate candidate scores to Home Assistant. Do not confuse this metric with prior 25%-sample or v47 50%-sample results; their validation splits differed.
- Still no new active classifier, no gate motor control, no image/GPS/vector export and no persisted new model. Keep `UNKNOWN` if validation is weak or either class fails.
