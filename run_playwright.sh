#!/bin/sh
# playwright: pipeline runs inside the mcpmark container, so --docker is fine.
DOCKER_MEMORY_LIMIT=16g ./run-benchmark.sh --models qwen-3-coder-30b-a3b-instruct --exp-name qwen-3-coder-0722-150 --mcps playwright --docker --ptc --ptc-timeout 60
DOCKER_MEMORY_LIMIT=16g ./run-benchmark.sh --models qwen-3-coder-30b-a3b-instruct --exp-name qwen-3-coder-0722-150 --mcps playwright --docker

# playwright_webarena: MUST run on the host (no --docker). The state manager starts
# the shopping/admin/forum containers via `docker`(->podman on host); the mcpmark
# image bundles no container runtime, so it cannot nest them. Host run picks up the
# docker->podman shim (~/bin/docker) and no_proxy=localhost from .mcp_env.
./run-benchmark.sh --models qwen-3-coder-30b-a3b-instruct --exp-name qwen-3-coder-0722-150 --mcps playwright_webarena --ptc --ptc-timeout 60
./run-benchmark.sh --models qwen-3-coder-30b-a3b-instruct --exp-name qwen-3-coder-0722-150 --mcps playwright_webarena
