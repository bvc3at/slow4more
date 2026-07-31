# E2E test image for slow4more: isolated Claude Code + stub Anthropic API.
# Build from the repo root:  docker build -f tests/e2e/Dockerfile -t slow4more-test .
# Run (no network needed):   docker run --rm --network none slow4more-test
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

COPY . /repo

RUN mkdir -p /work && chown -R node:node /work /repo
USER node
WORKDIR /work

CMD ["bash", "/repo/tests/e2e/run_test.sh"]
