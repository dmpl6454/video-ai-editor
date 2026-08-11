#!/usr/bin/env bash
# One-command post-pull check for macOS: "did this branch land in a state where
# the app actually runs here?"
#
#   bash verify_mac.sh              # full check (includes the pytest suite, ~10-15 min)
#   bash verify_mac.sh --no-tests   # everything except pytest (~2 min)
#
# Why this exists: the app is developed and packaged on two platforms, and CI
# can only reach the logic-and-boot layer (see .github/workflows/ci.yml). The
# native window, the .app bundle, VideoToolbox and avfoundation capture all
# need a human on a Mac. This script does every check that CAN be automated
# here, in the order that fails fastest, so a real Mac session starts from a
# known-good base instead of debugging setup and the app at the same time.
#
# It deliberately does NOT use `set -e`: every check runs and the summary at the
# end lists all failures at once. Chasing them one relaunch at a time is the
# slow way to do this.
#
# Runs under Git Bash on Windows too — the venv layout is detected — which is
# how it was smoke-tested before ever reaching a Mac.
#
# Kept to bash 3.2 constructs on purpose: macOS still ships bash 3.2 as
# /bin/bash, so no associative arrays, no `mapfile`, no `${var^^}`, and no
# expansion of a possibly-empty array (`"${arr[@]}"` under `set -u` is an
# "unbound variable" error there, though `${#arr[@]}` is fine).

set -uo pipefail
cd "$(dirname "$0")"

RUN_TESTS=1
[ "${1:-}" = "--no-tests" ] && RUN_TESTS=0

PASS=(); FAIL=(); WARN=()
ok()   { PASS+=("$1"); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
bad()  { FAIL+=("$1"); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }
warn() { WARN+=("$1"); printf '  \033[33mWARN\033[0m  %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

# The venv's python lives in bin/ on POSIX and Scripts/ on Windows.
VENV_PY=".venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY=".venv/Scripts/python.exe"

step "toolchain"
if command -v ffmpeg > /dev/null 2>&1; then
  ok "ffmpeg on PATH — $(ffmpeg -version 2>/dev/null | head -1 | cut -c1-60)"
else
  bad "ffmpeg not on PATH" "brew install ffmpeg   (ffmpeg-full if you want stabilize/libvidstab)"
fi
command -v ffprobe > /dev/null 2>&1 && ok "ffprobe on PATH" || bad "ffprobe not on PATH"
# libvidstab is optional by design — `stabilize` degrades rather than crashing.
if ffmpeg -hide_banner -filters 2>/dev/null | grep -q vidstabdetect; then
  ok "libvidstab present (stabilize will work)"
else
  warn "no libvidstab — 'stabilize' will report unavailable" "expected with plain 'brew install ffmpeg'; use ffmpeg-full to enable it"
fi
command -v npm > /dev/null 2>&1 && ok "npm on PATH — $(npm -v 2>/dev/null)" || bad "npm not on PATH" "install Node 22+"
command -v uv  > /dev/null 2>&1 && ok "uv on PATH"  || bad "uv not on PATH" "curl -LsSf https://astral.sh/uv/install.sh | sh"

step "python environment"
if [ -x "$VENV_PY" ]; then
  ok "venv python — $("$VENV_PY" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null)"
else
  bad "no venv" "uv sync --python 3.13 --all-extras --group dev"
fi

step "frontend build (this is what run.sh triggers on first launch)"
if [ ! -d frontend/node_modules ]; then
  echo "  installing node deps…"
  (cd frontend && npm install > /tmp/vae_npm_install.log 2>&1) \
    && ok "npm install" || bad "npm install failed" "see /tmp/vae_npm_install.log"
fi
# MUST be `tsc -b`. tsconfig.json is a solution file, so plain `tsc --noEmit`
# type-checks ZERO files and exits 0 — see CLAUDE.md.
if (cd frontend && npx tsc -b --force > /tmp/vae_tsc.log 2>&1); then
  ok "npx tsc -b --force (type-check)"
else
  bad "type-check failed" "$(tail -3 /tmp/vae_tsc.log 2>/dev/null | tr '\n' ' ')"
fi
if (cd frontend && npx vite build > /tmp/vae_vite.log 2>&1); then
  ok "vite build → frontend/dist"
else
  bad "vite build failed" "see /tmp/vae_vite.log"
fi
[ -f frontend/dist/index.html ] && ok "frontend/dist/index.html exists" \
  || bad "frontend/dist/index.html missing — desktop.py would exit 1 on launch"

step "backend imports (every module together, as the server loads them)"
if [ -x "$VENV_PY" ]; then
  PYTHONPATH=src "$VENV_PY" - <<'PY' > /tmp/vae_imports.log 2>&1
import importlib
for m in ("video_ai_editor.main", "video_ai_editor.desktop",
          "video_ai_editor.agent.dispatch", "video_ai_editor.agent.tools",
          "video_ai_editor.ai.features", "video_ai_editor.render.compositor",
          "video_ai_editor.render.pip", "video_ai_editor.storage_project"):
    importlib.import_module(m)
from video_ai_editor.agent.dispatch import DISPATCH
from video_ai_editor.agent.tools import ALL_TOOLS
missing = [t["name"] for t in ALL_TOOLS if t["name"] not in DISPATCH]
assert not missing, f"advertised tools with no handler: {missing}"
print(f"{len(DISPATCH)} tools, {len(ALL_TOOLS)} advertised, all resolve")
PY
  if [ $? -eq 0 ]; then ok "imports + tool registry — $(cat /tmp/vae_imports.log)"
  else bad "backend import failed" "$(tail -4 /tmp/vae_imports.log | tr '\n' ' ')"; fi
else
  bad "skipped backend imports (no venv)"
fi

step "the app boots"
if [ -x "$VENV_PY" ]; then
  PORT="${VAE_PORT:-8799}"
  PYTHONPATH=src "$VENV_PY" -m uvicorn video_ai_editor.main:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning > /tmp/vae_boot.log 2>&1 &
  BOOT_PID=$!
  up=0
  for _ in $(seq 1 45); do
    curl -fsS "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1 && { up=1; break; }
    sleep 1
  done
  if [ "$up" = "1" ]; then
    ok "uvicorn bound and /api/health answered — $(curl -fsS "http://127.0.0.1:$PORT/api/health")"
    ok "version — $(curl -fsS "http://127.0.0.1:$PORT/api/version")"
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/readyz")
    # /readyz is 503 when the app cannot find a usable ffmpeg — it exercises
    # discovery through the app's own code path, not just PATH.
    [ "$code" = "200" ] && ok "/readyz 200 (the app found ffmpeg)" \
      || bad "/readyz returned $code — the app could not find a usable ffmpeg"
  else
    bad "backend never answered /api/health" "$(tail -5 /tmp/vae_boot.log | tr '\n' ' ')"
  fi
  kill $BOOT_PID 2>/dev/null || true
  wait $BOOT_PID 2>/dev/null || true
else
  bad "skipped boot check (no venv)"
fi

step "optional feature availability on this machine"
if [ -x "$VENV_PY" ]; then
  PYTHONPATH=src "$VENV_PY" -c "
from video_ai_editor.ai.features import feature_report
r = feature_report()
print('  ' + r['summary'])
for m in r['unavailable']:
    print(f\"    - {m['key']}: {m.get('fix','')[:90]}\")
" 2>/dev/null || warn "could not read feature report"
fi

if [ "$RUN_TESTS" = "1" ]; then
  step "pytest (the same suite CI runs; ~10-15 min)"
  if [ -x "$VENV_PY" ]; then
    # Force the keys empty so no test can reach a real endpoint, exactly as CI does.
    if ANTHROPIC_API_KEY="" HUGGINGFACE_TOKEN="" "$VENV_PY" -m pytest -q --tb=short > /tmp/vae_pytest.log 2>&1; then
      ok "pytest — $(tail -1 /tmp/vae_pytest.log)"
    else
      bad "pytest failed" "$(grep -E '^(FAILED|ERROR)' /tmp/vae_pytest.log | head -5 | tr '\n' ' ')  (full log: /tmp/vae_pytest.log)"
    fi
  else
    bad "skipped pytest (no venv)"
  fi
else
  step "pytest"; echo "  skipped (--no-tests)"
fi

step "summary"
printf '  %d passed, %d failed, %d warnings\n' "${#PASS[@]}" "${#FAIL[@]}" "${#WARN[@]}"
if [ "${#FAIL[@]}" -gt 0 ]; then
  printf '\n  FAILED:\n'
  for f in "${FAIL[@]}"; do printf '    - %s\n' "$f"; done
  printf '\n  Fix these before judging the app itself.\n'
  exit 1
fi
cat <<'DONE'

  Everything automatable here passed. Launch it:

      bash run.sh

  Then check by hand the things no script can reach on a Mac:
    - the native window opens (pywebview / WKWebView) and the UI is interactive
    - upload a clip, scrub the timeline, and export — VideoToolbox is the
      encoder on this platform and CI never exercises it
    - Record Voiceover (avfoundation + the TCC mic prompt)
    - if you package: uv run bash build_app.sh, then open dist/'Video AI Editor.app'
DONE
