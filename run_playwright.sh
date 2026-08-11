#!/bin/bash
# ---------------------------------------------------------------------------
# Playwright benchmark runner — fill in the two values below, then just run it.
#
#   ./run_playwright.sh                        # both suites (default)
#   ./run_playwright.sh -m 14b -e exp-1        # override on the CLI
#   ./run_playwright.sh -s playwright          # eval_web + web_search only
#   ./run_playwright.sh -s webarena            # reddit/shopping/shopping_admin only
#   ./run_playwright.sh -m 14b -e exp-1 -p both   # no-PTC run, then PTC run
#
# Model shorthands:  8b -> qwen-3-8b
#                   14b -> qwen-3-14b
#                 coder -> qwen-3-coder-30b-a3b-instruct
#
# PTC modes (-p / --ptc-mode), results land in separate service dirs so one
# --exp-name is enough:
#   none       no PTC                                          -> <svc>/
#   ptc        --ptc                                           -> <svc>-ptc/
#   ptc-only   --ptc-only                                      -> <svc>-ptc-only/
#   both       none, then ptc         (sequential, same exp)
#   both-only  none, then ptc-only    (sequential, same exp)
#
# Two suites, two execution modes — this is not a style choice:
#   playwright          -> runs INSIDE the mcpmark container (--docker).
#   playwright_webarena -> MUST run on the HOST (no --docker). Its state manager
#                          starts the shopping/shopping_admin/forum containers via
#                          `docker` (-> podman shim at ~/bin/docker); the mcpmark
#                          image ships no container runtime, so under --docker every
#                          task fails instantly with
#                          "No such file or directory: 'docker'".
#                          Host runs also need no_proxy=localhost in .mcp_env.
#
# One task set, not two: src/evaluator.py maps playwright_webarena -> the
# `playwright` results dir, so both suites land in the SAME
# results/<exp>/<model>__playwright/run-N/ and add up to 25 standard tasks
# (eval_web 2 + web_search 2 + reddit 7 + shopping 7 + shopping_admin 7).
# With SUITE=both this script aggregates them into one 25-task pass@k report at
# the end (disable with --no-aggregate). A PTC pass writes to
# <model>__playwright-ptc/ and the aggregator reports it as a separate model row
# ("<model>-ptc"), so PTC and non-PTC numbers are never mixed.
# ---------------------------------------------------------------------------

# ======== FILL THESE IN ====================================================
# MODEL accepts the shorthands 8b / 14b / coder (expanded below), a full model
# name from src/model_config.py, or a comma-separated mix of both.
MODEL="${MODEL:-8b}"
EXP_NAME="${EXP_NAME:-qwen-3-8b-noptc}"
# ===========================================================================

# ---- Optional knobs (env-overridable) -------------------------------------
SUITE="${SUITE:-both}"                         # both | playwright | webarena
PTC_MODE="${PTC_MODE:-none}"                   # none | ptc | ptc-only | both | both-only
K="${K:-4}"                                    # runs per task, for pass@k
PTC_TIMEOUT="${PTC_TIMEOUT:-60}"
AGGREGATE="${AGGREGATE:-1}"                    # 1 = summarise the 25 tasks at the end
export DOCKER_MEMORY_LIMIT="${DOCKER_MEMORY_LIMIT:-16g}"

set -euo pipefail
cd "$(dirname "$0")"

while [[ $# -gt 0 ]]; do
    case $1 in
        -m|--model|--models) MODEL="$2"; shift 2 ;;
        -e|--exp-name)       EXP_NAME="$2"; shift 2 ;;
        -s|--suite)          SUITE="$2"; shift 2 ;;
        -k|--k)              K="$2"; shift 2 ;;
        -p|--ptc-mode)       PTC_MODE="$2"; shift 2 ;;
        --ptc)               PTC_MODE=ptc; shift ;;
        --ptc-only)          PTC_MODE=ptc-only; shift ;;
        --both)              PTC_MODE=both; shift ;;
        --ptc-timeout)       PTC_TIMEOUT="$2"; shift 2 ;;
        --no-aggregate)      AGGREGATE=0; shift ;;
        -h|--help)
            sed -n '2,41p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 1 ;;
    esac
done

case "$SUITE" in
    both|playwright|webarena) ;;
    *) echo "Invalid --suite '$SUITE' (expected: both|playwright|webarena)" >&2; exit 1 ;;
esac

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

if [ "$SUITE" != playwright ] && ! command -v docker >/dev/null 2>&1; then
    echo "✗ playwright_webarena needs a working \`docker\` on PATH (the ~/bin/docker" >&2
    echo "  -> podman shim). Without it every task fails setup instantly." >&2
    exit 1
fi

FAILED=()
run_pass() {  # run_pass <mcp> <pass> [extra run-benchmark args...]
    local mcp="$1" pass="$2"; shift 2
    local extra=()
    case "$pass" in
        ptc)      extra=(--ptc --ptc-timeout "$PTC_TIMEOUT") ;;
        ptc-only) extra=(--ptc-only --ptc-timeout "$PTC_TIMEOUT") ;;
    esac

    echo
    echo "==> $mcp [$pass] | model=$MODEL exp=$EXP_NAME k=$K ${extra[*]:-}"
    # Keep going on failure so later passes / the other suite still run.
    if ! ./run-benchmark.sh \
            --models "$MODEL" \
            --exp-name "$EXP_NAME" \
            --mcps "$mcp" \
            --k "$K" \
            "$@" \
            "${extra[@]}"; then
        echo "✗ $mcp [$pass] failed" >&2
        FAILED+=("$mcp/$pass")
    fi
}

for pass in "${PASSES[@]}"; do
    if [ "$SUITE" = both ] || [ "$SUITE" = playwright ]; then
        run_pass playwright "$pass" --docker
    fi
    if [ "$SUITE" = both ] || [ "$SUITE" = webarena ]; then
        # NOTE: deliberately no --docker here. See header.
        run_pass playwright_webarena "$pass"
    fi
done

# Both suites share one results dir, so a single aggregation covers all 25 tasks.
# Non-PTC and PTC passes come out as separate model rows (<model> / <model>-ptc).
# Non-fatal: a failed pass should still leave whatever numbers we do have.
if [ "$AGGREGATE" = 1 ]; then
    if [ "$SUITE" = both ]; then
        echo
        echo "==> aggregating (25 tasks: playwright + playwright_webarena)"
        python3 -m src.aggregators.aggregate_results \
            --exp-name "$EXP_NAME" --k "$K" --mcps playwright || \
            echo "⚠ aggregation reported incomplete results (see table above)" >&2
    else
        echo
        echo "ℹ suite=$SUITE is a partial task set; skipping the 25-task aggregation."
        echo "  Per-directory numbers:"
        echo "    python3 -m src.aggregators.aggregate_specific_results \\"
        echo "      --result-dir results/$EXP_NAME/<model>__playwright[-ptc] --k $K"
        echo "  (note: that command overwrites summary.json in the result dir)"
    fi
fi

if [ ${#FAILED[@]} -gt 0 ]; then
    echo "✗ failed passes: ${FAILED[*]}" >&2
    exit 1
fi
echo "✓ all passes done: ${PASSES[*]} (suite=$SUITE)"
