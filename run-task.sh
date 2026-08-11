#!/bin/bash

# MCPMark Task Runner
# Enable strict error handling
set -euo pipefail

# Default values
SERVICE="filesystem"
NETWORK_NAME="mcp-network"
POSTGRES_CONTAINER="mcp-postgres"

# Resource limits (can be overridden by environment variables)
DOCKER_MEMORY_LIMIT="${DOCKER_MEMORY_LIMIT:-4g}"
DOCKER_CPU_LIMIT="${DOCKER_CPU_LIMIT:-2}"

# Cleanup function
cleanup() {
    if [ "${SERVICE:-}" = "postgres" ]; then
        if podman ps --format '{{.Names}}' | grep -q "^${POSTGRES_CONTAINER}$"; then
            echo "Cleaning up PostgreSQL container..."
            podman stop "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
            podman rm "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
        fi
    fi
}

# Set up cleanup on exit
trap cleanup EXIT

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --mcp) SERVICE="$2"; shift 2 ;;
        --help)
            cat << EOF
Usage: $0 [--mcp SERVICE] [PIPELINE_ARGS]

Run MCPMark tasks in Docker containers.

Options:
    --mcp SERVICE    MCP service (notion|github|filesystem|playwright|postgres)
                        Default: filesystem

Environment Variables:
    DOCKER_MEMORY_LIMIT  Memory limit for container (default: 4g)
    DOCKER_CPU_LIMIT     CPU limit for container (default: 2)
    DOCKER_IMAGE_VERSION Docker image tag to use (default: latest)

All other arguments are passed directly to the pipeline. Notable
pass-through pipeline flags include:

    --ptc                Enable Programmatic Tool Calling: overlay a
                         \`programmatic_tool_call\` tool that runs Python
                         in a persistent sandbox and routes tools[...]
                         calls back through the underlying MCP server.
                         Results land under <svc>-ptc/.
    --ptc-only           PTC-only mode (implies --ptc): native tools stay
                         listed but can only be invoked via tools[...]
                         inside programmatic_tool_call. Results land under
                         <svc>-ptc-only/.
    --ptc-timeout SECS   Per-call timeout for programmatic_tool_call
                         (default: 60).

Examples:
    $0 --mcp notion --models o3 --exp-name test-1 --tasks all
    $0 --mcp postgres --models gpt-4 --exp-name pg-test --tasks basic_queries
    $0 --mcp filesystem --models qwen-3-coder-30b-a3b-instruct \\
        --exp-name ptc-test --tasks all --ptc --ptc-timeout 240
EOF
            exit 0
            ;;
        *) break ;;  # Stop parsing, rest goes to pipeline
    esac
done

# Docker image tag can be overridden by environment variable
DOCKER_IMAGE_REPO="evalsysorg/mcpmark"
DOCKER_IMAGE_VERSION="${DOCKER_IMAGE_VERSION:-latest}"
DOCKER_IMAGE="${DOCKER_IMAGE_REPO}:${DOCKER_IMAGE_VERSION}"

# Check if Docker image exists locally, pull only if not found
if ! podman image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
    echo "Docker image not found locally, pulling from Docker Hub..."
    podman pull "$DOCKER_IMAGE" || {
        echo "Error: Failed to pull Docker image from Docker Hub"
        echo "Please check your internet connection or Docker Hub access"
        exit 1
    }
else
    echo "Using local Docker image: $DOCKER_IMAGE"
fi

# Check if .mcp_env exists (warn but don't fail)
if [ ! -f .mcp_env ]; then
    echo "Warning: .mcp_env file not found. Some tasks may fail without API credentials."
fi

# Create network if doesn't exist
if ! podman network ls --format '{{.Name}}' | grep -q "^${NETWORK_NAME}$"; then
    echo "Creating Docker network: $NETWORK_NAME"
    podman network create "$NETWORK_NAME" || {
        echo "Error: Failed to create Docker network"
        exit 1
    }
fi

# Service-specific configurations
if [ "$SERVICE" = "postgres" ]; then
    # For postgres service, ensure PostgreSQL container is running
    if ! podman ps --format '{{.Names}}' | grep -q "^${POSTGRES_CONTAINER}$"; then
        echo "Starting PostgreSQL container..."
        podman run -d \
            --name "$POSTGRES_CONTAINER" \
            --network "$NETWORK_NAME" \
            -e POSTGRES_DATABASE=postgres \
            -e POSTGRES_USER=postgres \
            -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-password}" \
            pgvector/pgvector:0.8.0-pg17-bookworm

        echo "Waiting for PostgreSQL to be ready..."
        for i in {1..10}; do
            if podman exec "$POSTGRES_CONTAINER" pg_isready -U postgres >/dev/null 2>&1; then
                echo "PostgreSQL is ready!"
                break
            fi
            sleep 1
        done
    else
        echo "PostgreSQL container already running"
    fi

    # Run task with network connection to postgres
    podman run --rm \
        --http-proxy=false \
        --memory="$DOCKER_MEMORY_LIMIT" \
        --cpus="$DOCKER_CPU_LIMIT" \
        --network "$NETWORK_NAME" \
        --add-host=host.docker.internal:host-gateway \
        -e POSTGRES_HOST="$POSTGRES_CONTAINER" \
        -e POSTGRES_PORT=5432 \
        -e POSTGRES_USERNAME=postgres \
        -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-password}" \
        -e POSTGRES_DATABASE=postgres \
        -e QWEN3_CODER_30B_BASE_URL=http://host.docker.internal:28025/v1 \
        -e QWEN3_CODER_30B_BASE_URL_150=http://host.docker.internal:28026/v1 \
        -e QWEN3_8B_BASE_URL=http://host.docker.internal:28025/v1 \
        -e QWEN3_8B_SFT_BASE_URL=http://host.docker.internal:28025/v1 \
        -e QWEN3_5_9B_BASE_URL=http://host.docker.internal:28026/v1 \
        -e QWEN3_14B_BASE_URL=http://host.docker.internal:28025/v1 \
        -e NO_PROXY=host.docker.internal \
        -e no_proxy=host.docker.internal \
        -v "$(pwd)/results:/app/results" \
        -v "$(pwd)/postgres_state:/app/postgres_state" \
        $([ -f .mcp_env ] && echo "-v $(pwd)/.mcp_env:/app/.mcp_env:ro") \
        "$DOCKER_IMAGE" \
        python3 -m pipeline --mcp "$SERVICE" --k 1 "$@"
elif [ "$SERVICE" = "filesystem" ]; then
    # For filesystem service, mount test_environments
    podman run --rm \
        --http-proxy=false \
        --memory="$DOCKER_MEMORY_LIMIT" \
        --cpus="$DOCKER_CPU_LIMIT" \
        --network=host \
        -v "$(pwd)/results:/app/results" \
        -v "$(pwd)/test_environments:/app/test_environments" \
        $([ -f .mcp_env ] && echo "-v $(pwd)/.mcp_env:/app/.mcp_env:ro") \
        "$DOCKER_IMAGE" \
        python3 -m pipeline --mcp "$SERVICE" --k 1 "$@"
elif [ "$SERVICE" = "insforge" ]; then
    # For Insforge service, use host network to access Insforge backend on host
    podman run --rm \
        --http-proxy=false \
        --memory="$DOCKER_MEMORY_LIMIT" \
        --cpus="$DOCKER_CPU_LIMIT" \
        --add-host=host.docker.internal:host-gateway \
        -v "$(pwd)/results:/app/results" \
        $([ -f .mcp_env ] && echo "-v $(pwd)/.mcp_env:/app/.mcp_env:ro") \
        "$DOCKER_IMAGE" \
        python3 -m pipeline --mcp "$SERVICE" --k 1 "$@"
else
    # For other services (notion, github, playwright, etc.)
    podman run --rm \
        --http-proxy=false \
        --memory="$DOCKER_MEMORY_LIMIT" \
        --cpus="$DOCKER_CPU_LIMIT" \
        --network=host \
        -v "$(pwd)/results:/app/results" \
        -v "$(pwd)/test_environments:/app/test_environments" \
        $([ -f .mcp_env ] && echo "-v $(pwd)/.mcp_env:/app/.mcp_env:ro") \
        $([ -f notion_state.json ] && echo "-v $(pwd)/notion_state.json:/app/notion_state.json") \
        "$DOCKER_IMAGE" \
        python3 -m pipeline --mcp "$SERVICE" --k 1 "$@"
fi

echo "Task completed!"
