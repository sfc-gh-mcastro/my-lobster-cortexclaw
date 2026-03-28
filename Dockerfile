# Dockerfile for cortexclaw-agent
#
# This image provides an isolated environment for running the Cortex Code CLI.
# CortexClaw (the orchestrator) runs on the host and spawns agents inside
# containers built from this image.  The host mounts credentials (selective)
# and working directories at runtime via docker_runner.py.
#
# Build:  docker build -t cortexclaw-agent:latest .
# The image is NOT meant to be run standalone — CortexClaw generates a wrapper
# script that invokes `docker run ... cortexclaw-agent:latest cortex <args>`.

FROM debian:bookworm-slim

# Avoid interactive prompts during package install
ENV DEBIAN_FRONTEND=noninteractive

# Install minimal dependencies for the Cortex Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# Create the coco user home directory structure.
# The actual user UID/GID is overridden at runtime via --user flag,
# but we need the directory tree to exist.
RUN mkdir -p /home/coco/.snowflake /home/coco/.local/bin \
    && chmod 777 /home/coco /home/coco/.snowflake /home/coco/.local /home/coco/.local/bin

# Create workspace mount points
RUN mkdir -p /workspace/group /workspace/ipc /workspace/project \
    && chmod 777 /workspace/group /workspace/ipc /workspace/project

# Install Cortex Code CLI (beta channel — required by the Cortex Code Agent SDK)
# SKIP_PODMAN=1 skips the interactive Podman/sandbox prompt.
# NON_INTERACTIVE=1 skips all interactive prompts (path, etc.).
# CORTEX_CHANNEL=beta installs the beta build that the SDK expects.
ENV SKIP_PODMAN=1 NON_INTERACTIVE=1 CORTEX_CHANNEL=beta
RUN HOME=/home/coco curl -LsS https://ai.snowflake.com/static/cc-scripts/install.sh | HOME=/home/coco sh \
    && chmod +x /home/coco/.local/bin/cortex

# Put cortex on PATH
ENV PATH="/home/coco/.local/bin:${PATH}"
ENV HOME="/home/coco"

# Default working directory (overridden by -w in docker run)
WORKDIR /workspace/group

# Verify installation
RUN cortex --version

# No ENTRYPOINT — the wrapper script runs: docker run ... <image> cortex <args>
