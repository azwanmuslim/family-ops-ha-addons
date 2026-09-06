FROM debian:bookworm-slim

ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive \
    RUNNER_ALLOW_RUNASROOT=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl jq git bash tar gzip libicu72 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="arm64"; \
    latest="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')"; \
    mkdir -p /opt/actions-runner-template; \
    curl -fsSL "https://github.com/actions/runner/releases/download/v${latest}/actions-runner-linux-${arch}-${latest}.tar.gz" \
      | tar -xz -C /opt/actions-runner-template; \
    printf '%s' "$latest" > /opt/actions-runner-template/.family_ops_runner_version

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
