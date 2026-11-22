# Family Ops Deco Manager (read-only)

Home Assistant app for a TP-Link Deco X20 (main router at `192.168.0.1`), using a pinned `deco-local==1.0.0` library. The X20 V3 compatibility is **unverified** until a live authenticated read succeeds.

## Security
- No router or Home Assistant control port is exposed on the LAN or Internet. The HTTP server listens on port 18787 **inside the HA app network only**.
- All requests require a private `X-API-Key` header; API key and TP-Link credentials are stored **only in local Home Assistant app Options**, never in the repository.
- The API exposes `GET /health` and `GET /audit?section=qos|wifi|network|mesh|all`. **No writes** are implemented.
- The API redacts passwords, network names, token/session fields and device identifiers before returning results.
- Reads are paced, cached for 45 seconds and serialized to avoid stressing the router.

## One-time configuration
1. Install the Family Ops Deco Manager app from the Family Ops Home Assistant repository.
2. In its Configuration options fill `deco_email` with the TP-Link ID email used in the Deco app and `deco_password` with its password. Do not paste either into ChatGPT or GitHub.
3. The companion private `family-ops` workflow sets `api_key` securely from the existing `HA_BRIDGE_API_KEY` Actions secret.
4. Start the app. Run the private workflow command `get_deco_manager_health`, then `get_deco_manager_audit`.

## Limitations
The local library was verified on X55, not this X20 firmware. A control port failure or unsupported read is a compatibility issue, not permission to attempt guessed write opcodes. Network changes must have explicit user approval and a rollback plan.
