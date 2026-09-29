#!/usr/bin/env bash
# Every CI log says which ffmpeg ran: the three backend jobs run three
# different builds, and run 36599751632 could only be read once that was known.
# Called from its own step, because $GITHUB_PATH reaches the NEXT step: this is
# the binary pytest will resolve, not the one the installer believes it
# installed. No pipe into `head`: that SIGPIPEs ffmpeg.
#
# FFMPEG_REQUIRED_MAJOR (optional): fail unless ffmpeg AND ffprobe are that
# major. Set on the job that is pinned (ubuntu: 8); empty on the jobs that
# follow the package manager's current ffmpeg, which is what a new user
# installs today.
set -euo pipefail

required="${FFMPEG_REQUIRED_MAJOR:-}"
summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
for tool in ffmpeg ffprobe; do
  command -v "$tool" > /dev/null || { echo "::error::$tool is not on PATH"; exit 1; }
  out=$("$tool" -version)
  line=${out%%$'\n'*}
  echo "$tool: $line ($(command -v "$tool"))"
  echo "- \`$line\`" >> "$summary"
  major=$(printf '%s\n' "$line" | sed -nE "s/^$tool version n?([0-9]+)\..*/\1/p")
  if [ -n "$required" ] && [ "$major" != "$required" ]; then
    echo "::error::$tool major is '${major:-unknown}', this job is pinned to $required"
    exit 1
  fi
done
