#!/bin/bash
# Start a podman container that relays host:LISTEN_PORT -> 127.0.0.1:UPSTREAM_PORT
# so containers on `mcp-network` (postgres branch) can reach a vLLM endpoint
# that the SSH reverse tunnel only binds on host's loopback.
#
# Usage:
#   ./start-vllm-relay.sh                # use defaults
#   LISTEN_PORT=28025 UPSTREAM_PORT=18025 ./start-vllm-relay.sh
#   ./start-vllm-relay.sh stop           # tear down

set -euo pipefail

NAME="${RELAY_NAME:-vllm-relay}"
LISTEN_PORT="${LISTEN_PORT:-28025}"
UPSTREAM_PORT="${UPSTREAM_PORT:-18025}"
IMAGE="${RELAY_IMAGE:-evalsysorg/mcpmark:latest}"

if [ "${1:-}" = "stop" ]; then
    podman rm -f "$NAME" 2>/dev/null || true
    echo "stopped $NAME"
    exit 0
fi

# Recreate cleanly so config changes (port, image) actually take effect.
podman rm -f "$NAME" >/dev/null 2>&1 || true

podman run -d \
    --name "$NAME" \
    --restart=always \
    --network=host \
    -e LISTEN_PORT="$LISTEN_PORT" \
    -e UPSTREAM_PORT="$UPSTREAM_PORT" \
    "$IMAGE" \
    python3 -u -c '
import os, socket, threading
UP=("127.0.0.1", int(os.environ["UPSTREAM_PORT"]))
LISTEN=("0.0.0.0", int(os.environ["LISTEN_PORT"]))
def pump(s, d):
    try:
        while True:
            b = s.recv(65536)
            if not b: break
            d.sendall(b)
    except Exception:
        pass
    finally:
        try: d.shutdown(socket.SHUT_WR)
        except Exception: pass
def handle(c):
    try:
        u = socket.create_connection(UP)
    except Exception as e:
        print("upstream fail:", e, flush=True); c.close(); return
    threading.Thread(target=pump, args=(c, u), daemon=True).start()
    pump(u, c)
    c.close(); u.close()
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(LISTEN); srv.listen(128)
print("relay up", LISTEN, "->", UP, flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
' >/dev/null

sleep 1
podman ps --filter "name=^${NAME}$" --format '{{.Names}} {{.Status}}'
podman logs "$NAME" 2>&1 | tail -3
