# Family Ops GitHub Runner

Dedicated GitHub Actions self-hosted runner for `azwanmuslim/family-ops`, packaged as a Home Assistant add-on for Raspberry Pi 4 / aarch64.

## Purpose

- Runs GitHub Actions jobs inside the home network.
- Lets the private `family-ops` repository reach the Personal Ops Bridge locally.
- Keeps Home Assistant itself unexposed to the public internet.
- `hassio_api: true` allows workflows running inside this add-on to query Supervisor diagnostics when needed.

## Security

- Keep the target `family-ops` repository private.
- Do not commit registration tokens or Home Assistant API keys into this public add-on repository.
- Store `HA_BRIDGE_API_KEY` in the private repository's GitHub Actions secrets.
- Registration token is only entered in the Home Assistant add-on configuration.

## Updates

When this add-on's `version` is bumped in this repository, Home Assistant can detect the new version through the custom add-on repository and offer an Update button.
