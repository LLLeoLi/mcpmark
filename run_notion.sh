#!/bin/bash
# ---------------------------------------------------------------------------
# Notion benchmark runner — fill in the two values below, then just run it.
#
#   ./run_notion.sh
#   ./run_notion.sh -m 14b -e qwen-3-14b-noptc           # override on the CLI
#   ./run_notion.sh -m 14b -e qwen-3-14b -p both         # no-PTC run, then PTC run
#   PTC_MODE=both ./run_notion.sh                        # override via env
#
# Model shorthands:  8b -> qwen-3-8b
#                   14b -> qwen-3-14b
#                 coder -> qwen-3-coder-30b-a3b-instruct
#
# PTC modes (-p / --ptc-mode), results land in separate service dirs so one
# --exp-name is enough:
#   none       no PTC                                          -> notion/
#   ptc        --ptc                                           -> notion-ptc/
#   ptc-only   --ptc-only                                      -> notion-ptc-only/
#   both       none, then ptc         (sequential, same exp)
#   both-only  none, then ptc-only    (sequential, same exp)
# ---------------------------------------------------------------------------

# ======== FILL THESE IN ====================================================
# MODEL accepts the shorthands 8b / 14b / coder (expanded below), a full model
# name from src/model_config.py, or a comma-separated mix of both.
MODEL="${MODEL:-8b}"
EXP_NAME="${EXP_NAME:-qwen-3-8b-noptc}"
# ===========================================================================

# ---- Optional knobs (env-overridable) -------------------------------------
PTC_MODE="${PTC_MODE:-none}"                   # none | ptc | ptc-only | both | both-only
K="${K:-4}"                                    # runs per task, for pass@k
PTC_TIMEOUT="${PTC_TIMEOUT:-60}"
export DOCKER_MEMORY_LIMIT="${DOCKER_MEMORY_LIMIT:-16g}"

set -euo pipefail
cd "$(dirname "$0")"

while [[ $# -gt 0 ]]; do
    case $1 in
        -m|--model|--models) MODEL="$2"; shift 2 ;;
        -e|--exp-name)       EXP_NAME="$2"; shift 2 ;;
        -k|--k)              K="$2"; shift 2 ;;
        -p|--ptc-mode)       PTC_MODE="$2"; shift 2 ;;
        --ptc)               PTC_MODE=ptc; shift ;;
        --ptc-only)          PTC_MODE=ptc-only; shift ;;
        --both)              PTC_MODE=both; shift ;;
        --ptc-timeout)       PTC_TIMEOUT="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 1 ;;
    esac
done

# Each pass is one full benchmark invocation; "both" just queues two of them.
case "$PTC_MODE" in
    none)      PASSES=(none) ;;
    ptc)       PASSES=(ptc) ;;
    ptc-only)  PASSES=(ptc-only) ;;
    both)      PASSES=(none ptc) ;;
    both-only) PASSES=(none ptc-only) ;;
    *) echo "Invalid --ptc-mode '$PTC_MODE' (expected: none|ptc|ptc-only|both|both-only)" >&2; exit 1 ;;
esac

# Expand the shorthands. Anything unrecognised passes through untouched, so a
# full model name from src/model_config.py still works.
expand_models() {
    local out=() m
    IFS=',' read -ra _in <<< "$1"
    for m in "${_in[@]}"; do
        case "$m" in
            8b)    out+=(qwen-3-8b) ;;
            14b)   out+=(qwen-3-14b) ;;
            coder) out+=(qwen-3-coder-30b-a3b-instruct) ;;
            *)     out+=("$m") ;;
        esac
    done
    (IFS=','; echo "${out[*]}")
}
MODEL="$(expand_models "$MODEL")"

# Notion state setup drives a real browser through the Notion UI, so this needs a
# valid notion_state.json at the repo root. Re-run the login helper if setup
# starts failing with a login/redirect error (as opposed to a code error).
# k>1 requires the image to contain evaluator.close(); without it every run after
# run-1 dies with "Playwright Sync API inside the asyncio loop". Rebuild with
# ./build-docker.sh if you see that.
FAILED=()
for pass in "${PASSES[@]}"; do
    EXTRA=()
    case "$pass" in
        ptc)      EXTRA=(--ptc --ptc-timeout "$PTC_TIMEOUT") ;;
        ptc-only) EXTRA=(--ptc-only --ptc-timeout "$PTC_TIMEOUT") ;;
    esac

    echo
    echo "==> notion [$pass] | model=$MODEL exp=$EXP_NAME k=$K ${EXTRA[*]:-}"
    # Keep going on failure so a "both" run still attempts the second pass.
    if ! ./run-benchmark.sh \
            --models "$MODEL" \
            --exp-name "$EXP_NAME" \
            --mcps notion \
            --k "$K" \
            --docker \
            "${EXTRA[@]}"; then
        echo "✗ notion [$pass] failed" >&2
        FAILED+=("$pass")
    fi
done

if [ ${#FAILED[@]} -gt 0 ]; then
    echo "✗ failed passes: ${FAILED[*]}" >&2
    exit 1
fi
echo "✓ all passes done: ${PASSES[*]}"
