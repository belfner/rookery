# Ubuntu arm64 image that installs rookery's catalog on aarch64 and runs the installed programs.
# Build from the repository root; `make test-aarch64` builds and runs it. An x86_64 host needs
# qemu registered first:  docker run --privileged --rm tonistiigi/binfmt --install arm64
FROM --platform=linux/arm64 ubuntu:24.04

# file reports binary architectures; python3 runs the Python script programs; build-essential
# compiles imcat; jq serves fastfetch's get-distro helper
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates file jq python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.4 /uv /uvx /usr/local/bin/

RUN useradd --create-home tester
USER tester
WORKDIR /home/tester/rookery
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/home/tester/rookery/.venv/bin:/home/tester/.local/bin:$PATH

# Dependencies first so source edits rebuild only the final layers
COPY --chown=tester pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --chown=tester README.md LICENSE ./
COPY --chown=tester src ./src
RUN uv sync --frozen --no-dev
COPY --chown=tester docker/aarch64/smoke.sh /home/tester/smoke.sh

CMD ["bash", "/home/tester/smoke.sh"]
