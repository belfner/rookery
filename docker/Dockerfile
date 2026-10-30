# Ubuntu image that runs rookery's test suite and installs its catalog, built for either
# architecture: docker/test.sh x86_64|aarch64 builds and runs it (`make test-docker` does both).
# The test user has passwordless sudo, so the apt and sudo-created install root paths run too.
FROM ubuntu:24.04

# file reports binary architectures; python3 runs the Python script programs; build-essential
# compiles imcat; jq serves fastfetch's get-distro helper; sudo backs the system install paths.
# The package lists stay in the image, since apt resolves netron's dependencies from them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates file jq python3 sudo
COPY --from=ghcr.io/astral-sh/uv:0.9.4 /uv /uvx /usr/local/bin/

RUN useradd --create-home tester \
    && echo "tester ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/tester \
    && chmod 0440 /etc/sudoers.d/tester
USER tester
WORKDIR /home/tester/rookery
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/home/tester/rookery/.venv/bin:/home/tester/.local/bin:$PATH

# Dependencies first so source edits rebuild only the final layers
COPY --chown=tester pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY --chown=tester README.md LICENSE ./
COPY --chown=tester src ./src
COPY --chown=tester tests ./tests
RUN uv sync --frozen
COPY --chown=tester docker/run.sh docker/smoke.sh /home/tester/

CMD ["bash", "/home/tester/run.sh"]
