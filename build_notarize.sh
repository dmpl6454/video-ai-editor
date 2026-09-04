#!/usr/bin/env bash
# Sign, notarize and staple the macOS build for distribution. Run:
#   bash build_notarize.sh --build                            # rebuild via build_app.sh, then sign → notarize → staple
#   bash build_notarize.sh                                    # reuse the existing dist/Video AI Editor.app
#   VAE_SIGN_IDENTITY=- bash build_notarize.sh --sign-only    # ad-hoc DRY RUN: no keychain, no network
#
# Output: dist/Video AI Editor.app (re-signed with Developer ID, notarization
#         ticket stapled) and dist/Video-AI-Editor.dmg (signed, notarized,
#         stapled) — the file to publish.
#
# Why a second script instead of teaching build_app.sh to do this:
# build_app.sh is the tested dev path and it AD-HOC signs with `--deep`,
# which is exactly right for local TCC attribution (its own comments) and
# exactly wrong for distribution: Gatekeeper rejects ad-hoc outright, and
# `--deep` is unpredictable on a PyInstaller layout (see sign_main_bundle
# for why). Keeping the two concerns apart means a broken
# notarization run can never regress the everyday build. This script takes
# build_app.sh's FINISHED bundle — Info.plist already carries the mic usage
# string and the VERSION stamp, so nothing here rewrites the plist — strips
# its ad-hoc seal by re-signing inside-out with a real identity, then does the
# two notarization round-trips (app, then DMG) and stamps a ticket onto each.
#
# One-time setup (CLAUDE.md → "Notarized release"): a "Developer ID
# Application" certificate in the login keychain, plus a notarytool keychain
# profile (default name "vae-notary"). This script never reads, stores or
# prompts for credentials — notarytool pulls them from that keychain profile.
#
# Environment:
#   VAE_SIGN_IDENTITY   codesign identity (SHA-1 or full name). Default: the first
#                       "Developer ID Application" identity in
#                       `security find-identity -v -p codesigning`. The literal
#                       "-" means ad-hoc: signing + verification + DMG run as a
#                       dry run and every notarization step is skipped (Apple
#                       will not notarize an ad-hoc signature).
#   VAE_NOTARY_PROFILE  notarytool keychain profile name. Default: vae-notary
#   VAE_APP             the .app to process. Default: dist/Video AI Editor.app
#   VAE_DMG             DMG output path. Default: dist/Video-AI-Editor.dmg, or
#                       next to VAE_APP when VAE_APP is not the default (so a
#                       dry run against a copy never touches dist/).
# Flags: --build  --sign-only  --skip-dmg  --help

set -euo pipefail
# Every guarded command below explains itself via die; this catches the rest
# (a failed assignment, a pipeline in a command substitution) so the script
# can never stop without saying where. errtrace makes the ERR trap fire
# inside functions too. Found the hard way: the ad-hoc dry run once exited 1
# after a grep-with-no-match inside print_summary, printing nothing.
set -o errtrace
trap 'echo "[notarize] ERROR: \"$BASH_COMMAND\" failed (exit $?) at line $LINENO" >&2' ERR

DEFAULT_APP="dist/Video AI Editor.app"
# Version- and arch-tagged, so the artifact a user downloads states the one
# requirement they cannot discover any other way: this build is arm64-only and
# an Intel Mac cannot launch it at all. Kept in sync with build_dmg.sh's own
# default (which derives the arch tag from the built binary); if the two ever
# disagree the explicit VAE_DMG passed below still wins, so the artifact and
# the notarization/stapling always refer to the same file.
VERSION_TAG="$(tr -d "[:space:]" < VERSION 2>/dev/null || echo 0.0.0)"
DMG_BASENAME="Video-AI-Editor-${VERSION_TAG}-AppleSilicon.dmg"
ENTITLEMENTS="entitlements.plist"
# notarytool --wait polls Apple; typical turnaround is 1-10 min, occasionally
# longer under load. 30m is generous without hanging a terminal forever.
NOTARY_TIMEOUT="30m"

SIGN_IDENTITY="${VAE_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${VAE_NOTARY_PROFILE:-vae-notary}"
APP="${VAE_APP:-$DEFAULT_APP}"
DMG="${VAE_DMG:-}"
DO_BUILD=0
SIGN_ONLY=0
SKIP_DMG=0

# Filled in as the run progresses; read by print_summary.
IS_ADHOC=0
IDENTITY_LABEL=""
NOTARY_ID_APP="n/a"
NOTARY_ID_DMG="n/a"
VERDICT_APP="not assessed"
VERDICT_DMG="not assessed"
NESTED_COUNT=0
DMG_BUILT=0   # set by make_dmg; the summary must never describe a DMG from an earlier run
TMP_ROOT=""

log() { echo "[notarize] $*"; }
die() { echo "[notarize] ERROR: $*" >&2; exit 1; }

usage() {
  # `--help | head -1` closes stdout early: the last sed takes SIGPIPE
  # (exit 141). Under `set -euo pipefail` that would both fire the ERR trap
  # and become the script's exit status, so drop pipefail around the
  # pipeline and swallow the status — help text is best-effort output.
  trap - ERR
  set +o pipefail
  sed -n '2,/^# Flags:/p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' 2>/dev/null || true
  set -o pipefail
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --build) DO_BUILD=1 ;;
      --sign-only) SIGN_ONLY=1 ;;
      --skip-dmg) SKIP_DMG=1 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown argument: $1 (see --help)" ;;
    esac
    shift
  done
  if [ -z "$DMG" ]; then
    if [ "$APP" = "$DEFAULT_APP" ]; then
      DMG="dist/$DMG_BASENAME"
    else
      DMG="$(dirname "$APP")/$DMG_BASENAME"
    fi
  fi
}

make_tmp() {
  # Everything transient (staging copy, zip for upload, notarytool JSON) lives
  # under one mktemp root so a single trap cleans up on any exit path.
  TMP_ROOT="$(mktemp -d /tmp/vae_notarize.XXXXXX)"
  trap 'rm -rf "$TMP_ROOT"' EXIT
}

# --- identity ---------------------------------------------------------------

resolve_identity() {
  if [ "$SIGN_IDENTITY" = "-" ]; then
    IS_ADHOC=1
    IDENTITY_LABEL="ad-hoc (-)"
    log "identity: AD-HOC. Signing/verification/DMG run as a dry run; notarization is impossible with an ad-hoc signature and will be skipped."
    # `--timestamp` asks Apple's timestamp authority to countersign the
    # signature and that only works for a real certificate — codesign
    # rejects the combination with ad-hoc, so the flag is omitted here.
    # Notarization REQUIRES a secure timestamp, which is one more reason an
    # ad-hoc run cannot proceed past signing.
    log "identity: omitting --timestamp (secure timestamps need a real certificate; not valid with ad-hoc)"
    TIMESTAMP_ARGS=()
    return
  fi
  TIMESTAMP_ARGS=(--timestamp)
  local listing
  listing="$(security find-identity -v -p codesigning 2>/dev/null || true)"
  if [ -z "$SIGN_IDENTITY" ]; then
    # First "Developer ID Application" line, e.g.
    #   1) CAA1…BFC0 "Developer ID Application: Name (TEAMID)"
    # Use the SHA-1 hash, not the name: names can be ambiguous when an
    # expired/renewed cert coexists with the current one, hashes cannot.
    local line
    line="$(printf '%s\n' "$listing" | grep 'Developer ID Application' | head -1 || true)"
    [ -n "$line" ] || die "no 'Developer ID Application' identity in the keychain. Create one (Xcode → Settings → Accounts → Manage Certificates, or developer.apple.com) — CLAUDE.md → 'Notarized release'. For an ad-hoc dry run: VAE_SIGN_IDENTITY=- bash $0 --sign-only"
    SIGN_IDENTITY="$(printf '%s\n' "$line" | sed -E 's/^ *[0-9]+\) ([0-9A-F]{40}) .*/\1/')"
    IDENTITY_LABEL="$(printf '%s\n' "$line" | sed -E 's/^[^"]*"([^"]*)".*/\1/')"
  else
    # An explicit identity is still required to be a "Developer ID
    # Application" cert when the keychain can tell us: an "Apple Development"
    # cert also has a TeamIdentifier and signs + timestamps fine, so without
    # this check the mistake only surfaces after all the nested signing and
    # the upload, as an Invalid verdict on every file.
    local line
    line="$(printf '%s\n' "$listing" | grep -F "$SIGN_IDENTITY" | head -1 || true)"
    if [ -n "$line" ]; then
      case "$line" in
        *"Developer ID Application"*) ;;
        *) die "VAE_SIGN_IDENTITY resolves to '$line' — not a 'Developer ID Application' certificate; the notary service only accepts Developer ID signatures" ;;
      esac
    fi
    IDENTITY_LABEL="$(printf '%s\n' "$line" | sed -E 's/^[^"]*"([^"]*)".*/\1/' || true)"
    [ -n "$IDENTITY_LABEL" ] || IDENTITY_LABEL="$SIGN_IDENTITY"
  fi
  log "identity: $IDENTITY_LABEL"
}

# --- build / preconditions --------------------------------------------------

run_build() {
  [ "$APP" = "$DEFAULT_APP" ] || die "--build always writes $DEFAULT_APP; it cannot be combined with VAE_APP=$APP"
  command -v uv >/dev/null 2>&1 || die "--build needs 'uv' on PATH (build_app.sh runs under uv)"
  log "running build_app.sh (frontend + PyInstaller + plist stamps + ad-hoc sign)"
  uv run bash build_app.sh || die "build_app.sh failed — fix that first; nothing was signed"
}

require_app() {
  [ -f "$APP/Contents/Info.plist" ] || die "$APP not found. Run 'uv run bash build_app.sh' first, or pass --build."
  [ -f "$ENTITLEMENTS" ] || die "$ENTITLEMENTS missing from $(pwd) — run from the repo root"
  for tool in codesign xattr ditto hdiutil spctl; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' not found on PATH"
  done
  xcrun --find notarytool >/dev/null 2>&1 || die "xcrun notarytool not found — install the Command Line Tools (xcode-select --install)"
}

plist_get() {  # plist key
  /usr/libexec/PlistBuddy -c "Print :$2" "$1" 2>/dev/null || true
}

# --- signing ----------------------------------------------------------------

# Every Mach-O below the main executable, one path per line on stdout:
# *.so / *.dylib by name, plus anything `file` calls Mach-O (extensionless
# helper binaries, if a future dependency ships one). Symlinks are skipped
# (-type f): PyInstaller 6 fills Contents/Resources with symlinks into
# Contents/Frameworks, and signing through a link would just sign the target
# twice. *.framework directories are listed separately by list_frameworks.
list_nested_machos() {
  local stage_app="$1" main_exe="$2" root f
  for root in Frameworks Resources MacOS; do
    [ -d "$stage_app/Contents/$root" ] || continue
    find "$stage_app/Contents/$root" -type f ! -path "$main_exe" -print0
  done | while IFS= read -r -d '' f; do
    case "$f" in
      *.so|*.dylib) printf '%s\n' "$f" ;;
      *) case "$(file -b -- "$f")" in *Mach-O*) printf '%s\n' "$f" ;; esac ;;
    esac
  done
}

# Deepest first, so a framework nested inside another is sealed before its
# parent's resource seal is computed.
list_frameworks() {
  find "$1/Contents" -type d -name '*.framework' 2>/dev/null \
    | awk '{ print length($0) " " $0 }' | sort -rn | cut -d' ' -f2-
}

# Libraries get hardened runtime + timestamp and NO entitlements: entitlements
# are a property of the process (the main executable); on a dylib they are
# meaningless noise, and keeping them off is what makes the --deep spot-check
# in verify_signature meaningful.
sign_nested_machos() {
  local stage_app="$1" main_exe="$2" list="$TMP_ROOT/nested.txt" f
  list_nested_machos "$stage_app" "$main_exe" > "$list"
  NESTED_COUNT="$(grep -c . "$list" || true)"
  log "signing $NESTED_COUNT nested Mach-O files (inside-out step 1/3)"
  # codesign prints "replacing existing signature" per file (build_app.sh
  # already ad-hoc signed everything); keep that out of the log and show
  # codesign's stderr only when it actually fails.
  local err="$TMP_ROOT/codesign.err"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    codesign --force --options runtime ${TIMESTAMP_ARGS[@]+"${TIMESTAMP_ARGS[@]}"} \
      --sign "$SIGN_IDENTITY" "$f" 2>"$err" \
      || { cat "$err" >&2; die "codesign failed on nested binary: ${f#$stage_app/}"; }
  done < "$list"
  local fw n=0
  while IFS= read -r fw; do
    [ -n "$fw" ] || continue
    codesign --force --options runtime ${TIMESTAMP_ARGS[@]+"${TIMESTAMP_ARGS[@]}"} \
      --sign "$SIGN_IDENTITY" "$fw" 2>"$err" \
      || { cat "$err" >&2; die "codesign failed on framework: ${fw#$stage_app/}"; }
    n=$((n + 1))
  done < <(list_frameworks "$stage_app")
  log "signed $n framework bundle(s) (inside-out step 2/3)"
}

# The bundle itself: hardened runtime, timestamp, entitlements, and
# deliberately NO --deep. `--deep` applies the OUTER item's options
# (including --entitlements) to whatever nested code it finds, and it only
# recurses into the standard nested-code locations — so on a PyInstaller
# layout (hundreds of .so under Resources/Frameworks, symlinked both ways)
# which Mach-O end up signed, with which flags, is not something you can
# reason about from the command line. Signing inside-out makes every
# Mach-O's flags explicit and is what Apple recommends. Because everything
# underneath was already sealed in sign_nested_machos, the bundle's
# resource seal now covers correctly-signed leaves.
sign_main_bundle() {
  local stage_app="$1"
  log "signing the bundle with hardened runtime + $ENTITLEMENTS, no --deep (inside-out step 3/3)"
  codesign --force --options runtime ${TIMESTAMP_ARGS[@]+"${TIMESTAMP_ARGS[@]}"} \
    --entitlements "$ENTITLEMENTS" \
    --sign "$SIGN_IDENTITY" "$stage_app" \
    || die "codesign of the bundle failed"
  # codesign writes com.apple.FinderInfo onto the bundle root as a side
  # effect of sealing it (build_app.sh notes the same); --strict rejects it.
  xattr -d com.apple.FinderInfo "$stage_app" 2>/dev/null || true
}

verify_signature() {
  local path="$1" info ents verify_out="$TMP_ROOT/verify.txt"
  # --verbose=2 prints one --prepared/--validated line per sealed item (hundreds);
  # keep the exact command, summarise its output, dump it all only on failure.
  log "codesign --verify --strict --deep --verbose=2"
  if ! codesign --verify --strict --deep --verbose=2 "$path" >"$verify_out" 2>&1; then
    cat "$verify_out" >&2
    die "signature verification failed for $path"
  fi
  log "  $(grep -c -- '--validated:' "$verify_out" || true) nested items validated; $(tail -2 "$verify_out" | sed "s|^$path: ||" | tr '\n' ';' | sed 's/;$//; s/;/; /')"
  # Never `cmd | grep -q` under pipefail: grep -q exits on the first match,
  # the writer takes SIGPIPE and the pipeline reports failure. Capture, then
  # match in bash.
  info="$(codesign -dvv "$path" 2>&1)"
  printf '%s\n' "$info" | grep -E '^(Authority=|TeamIdentifier=|Signature=|Timestamp=|CodeDirectory )' | sed 's/^/[notarize]   /'
  [[ "$info" == *"flags="*"runtime"* ]] \
    || die "hardened runtime flag missing from the signature — notarization would be rejected"
  if [ "$IS_ADHOC" = 1 ]; then
    [[ "$info" == *"Signature=adhoc"* ]] || die "expected an ad-hoc signature"
  else
    [[ "$info" == *$'\n'"Timestamp="* ]] \
      || die "no secure timestamp on the signature — notarization requires one"
    [[ "$info" =~ $'\n'TeamIdentifier=[A-Z0-9] ]] \
      || die "TeamIdentifier not set — the signature carries no team, i.e. it was not made with an Apple-issued certificate"
    # The notary service accepts Developer ID signatures only; an "Apple
    # Development" cert passes every check above and fails only at Apple.
    [[ "$info" == *$'\n'"Authority=Developer ID Application"* ]] \
      || die "signing identity is not a Developer ID Application certificate: $IDENTITY_LABEL"
  fi
  ents="$(codesign -d --entitlements - "$path" 2>/dev/null || true)"
  [[ "$ents" == *"com.apple.security.device.audio-input"* ]] \
    || die "the mic entitlement did not land on the bundle (entitlements.plist not applied?)"
  # Spot-check a nested library: the runtime flag must be on the leaves
  # too, and the entitlements must NOT be (that is what --deep would do).
  local sample
  sample="$(head -1 "$TMP_ROOT/nested.txt" || true)"
  if [ -n "$sample" ]; then
    info="$(codesign -dvv "$sample" 2>&1)"
    [[ "$info" == *"flags="*"runtime"* ]] \
      || die "nested library lacks hardened runtime: ${sample#$path/}"
    ents="$(codesign -d --entitlements - "$sample" 2>/dev/null || true)"
    if [[ "$ents" == *"audio-input"* ]]; then
      die "nested library carries the app entitlements (a --deep leak): ${sample#$path/}"
    fi
  fi
}

# Sign in a /tmp staging copy and move the finished bundle back, exactly as
# build_app.sh does: a Desktop-rooted checkout gets com.apple.FinderInfo
# re-stamped onto the bundle directory within ~1s by Finder/LaunchServices,
# which makes `codesign --strict` fail and can make an in-place --force sign
# fail outright. Route around the daemon, don't race it.
sign_app() {
  local stage_dir="$TMP_ROOT/stage" stage_app main_exe mic ver
  stage_app="$stage_dir/$(basename "$APP")"
  mic="$(plist_get "$APP/Contents/Info.plist" NSMicrophoneUsageDescription)"
  ver="$(plist_get "$APP/Contents/Info.plist" CFBundleShortVersionString)"
  [ -n "$mic" ] || die "Info.plist lacks NSMicrophoneUsageDescription — this is not a build_app.sh output"
  mkdir -p "$stage_dir"
  log "staging a copy in $stage_dir"
  cp -R "$APP" "$stage_app"
  xattr -cr "$stage_app" || true
  main_exe="$stage_app/Contents/MacOS/$(plist_get "$stage_app/Contents/Info.plist" CFBundleExecutable)"
  [ -f "$main_exe" ] || die "main executable not found: $main_exe"
  sign_nested_machos "$stage_app" "$main_exe"
  sign_main_bundle "$stage_app"
  verify_signature "$stage_app"
  log "moving the signed bundle back to $APP"
  rm -rf "$APP"
  mv "$stage_app" "$APP"
  # Non-strict functional verify at the destination (strict may trip on the
  # Desktop-path FinderInfo xattr again — build_app.sh explains why).
  codesign --verify --deep "$APP" || die "final codesign --verify failed at $APP"
  [ "$(plist_get "$APP/Contents/Info.plist" NSMicrophoneUsageDescription)" = "$mic" ] \
    || die "NSMicrophoneUsageDescription changed during signing"
  [ "$(plist_get "$APP/Contents/Info.plist" CFBundleShortVersionString)" = "$ver" ] \
    || die "CFBundleShortVersionString changed during signing"
  log "signed OK: $APP (version $ver, mic usage string preserved)"
}

# --- Gatekeeper -------------------------------------------------------------

# $1 app|dmg, $2 "informational" or "required". Before stapling the expected
# verdict is "rejected" (source=Unnotarized Developer ID, or plain rejected
# for ad-hoc) — printed so the before/after is visible. After stapling
# anything but accepted + Notarized Developer ID is a failure.
assess() {
  local kind="$1" mode="$2" out rc=0 f="$TMP_ROOT/spctl-$1.txt"
  # spctl exits 3 for "rejected", which is the EXPECTED answer before
  # stapling. Run it as a `|| rc=$?` list into a file rather than inside a
  # command substitution: with errtrace the ERR trap fires inside `$( )`
  # subshells too and would report the expected rejection as an error.
  if [ "$kind" = app ]; then
    spctl --assess --type execute --verbose=2 "$APP" >"$f" 2>&1 || rc=$?
    out="$(tr '\n' ' ' <"$f" | sed 's/  */ /g; s/ $//')"
    VERDICT_APP="$out"
  else
    spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG" >"$f" 2>&1 || rc=$?
    out="$(tr '\n' ' ' <"$f" | sed 's/  */ /g; s/ $//')"
    VERDICT_DMG="$out"
  fi
  if [ "$mode" = informational ]; then
    log "Gatekeeper ($kind, before notarization — 'rejected' is expected here): $out"
    return 0
  fi
  if [ $rc -ne 0 ] || [[ "$out" != *"accepted"* ]] \
     || [[ "$out" != *"source=Notarized Developer ID"* ]]; then
    die "Gatekeeper did not accept the $kind after stapling: $out"
  fi
  log "Gatekeeper ($kind): $out"
}

# --- notarization -----------------------------------------------------------

json_field() {  # file key — first string value for "key" in notarytool's JSON
  # `|| true`: head closing the pipe early must not turn into a pipefail
  # that aborts the caller's assignment.
  sed -n 's/.*"'"$2"'" *: *"\([^"]*\)".*/\1/p' "$1" | head -1 || true
}

# $1 path, $2 app|dmg. Submits, waits, and on anything but Accepted prints
# the notary log — its "issues" array names the offending file and reason
# (unsigned binary, missing timestamp, no hardened runtime…) — then exits 1.
notarize() {
  local target="$1" kind="$2" upload="$1" out="$TMP_ROOT/notary-$2.json" rc=0 sub_id status
  if [ "$kind" = app ]; then
    # The notary service takes a zip of an .app. Zip a CLEAN copy: the
    # bundle in dist/ picks the FinderInfo xattr back up (see sign_app), and
    # ditto would faithfully archive it alongside the seal.
    local clean="$TMP_ROOT/upload/$(basename "$target")"
    mkdir -p "$TMP_ROOT/upload"
    cp -R "$target" "$clean"
    xattr -cr "$clean" || true
    upload="$TMP_ROOT/upload/$(basename "$target").zip"
    ditto -c -k --keepParent "$clean" "$upload" || die "ditto failed to zip $target"
  fi
  log "submitting $kind to Apple's notary service (profile '$NOTARY_PROFILE'; --wait, up to $NOTARY_TIMEOUT)"
  xcrun notarytool submit "$upload" --keychain-profile "$NOTARY_PROFILE" \
    --wait --timeout "$NOTARY_TIMEOUT" --output-format json > "$out" 2> "$out.err" || rc=$?
  sub_id="$(json_field "$out" id)"
  status="$(json_field "$out" status)"
  if [ -z "$sub_id" ]; then
    cat "$out" "$out.err" >&2
    die "notarytool submit did not return a submission id (exit $rc). Usual causes: no keychain profile named '$NOTARY_PROFILE' (create it once — CLAUDE.md → 'Notarized release'), a revoked app-specific password, or no network."
  fi
  if [ "$status" != "Accepted" ]; then
    log "$kind submission $sub_id finished with status '${status:-unknown}' — fetching the notary log (look at the 'issues' array):"
    xcrun notarytool log "$sub_id" --keychain-profile "$NOTARY_PROFILE" 2>&1 || log "could not fetch the log for $sub_id"
    die "$kind was not accepted by the notary service (submission $sub_id)"
  fi
  log "$kind accepted (submission $sub_id)"
  if [ "$kind" = app ]; then NOTARY_ID_APP="$sub_id"; else NOTARY_ID_DMG="$sub_id"; fi
}

# Attach the ticket so Gatekeeper passes offline, then have stapler confirm.
staple_and_validate() {
  log "stapling $1"
  xcrun stapler staple "$1" || die "stapler staple failed for $1 (was it accepted? is it the same file that was submitted?)"
  xcrun stapler validate "$1" || die "stapler validate failed for $1"
}

# --- DMG --------------------------------------------------------------------

make_dmg() {
  log "building DMG via build_dmg.sh → $DMG"
  VAE_APP="$APP" VAE_DMG="$DMG" bash build_dmg.sh || die "build_dmg.sh failed"
  [ -f "$DMG" ] || die "build_dmg.sh did not produce $DMG"
  DMG_BUILT=1
}

# A DMG carries its own signature (a flat-file seal, no runtime options or
# entitlements apply) and its own notarization ticket. The notary service
# DOES look inside a submitted DMG and would ticket the nested .app too, so
# a single DMG submission is a legitimate flow; the two-step here is kept
# on purpose: notarizing the .app first lets its ticket be STAPLED into the
# bundle (a user who drags the app out of the DMG launches it offline, with
# no ticket lookup), and an app-level Invalid log names the offending file
# directly instead of through a disk-image path. The DMG is then notarized
# and stapled itself so the image opens cleanly.
sign_dmg() {
  log "signing DMG"
  codesign --force ${TIMESTAMP_ARGS[@]+"${TIMESTAMP_ARGS[@]}"} --sign "$SIGN_IDENTITY" "$DMG" \
    || die "codesign of the DMG failed"
  codesign --verify --verbose=2 "$DMG" || die "DMG signature does not verify"
}

# --- reporting --------------------------------------------------------------

print_summary() {
  local info team="" auth="" ver size sha
  info="$(codesign -dvv "$APP" 2>&1)"
  # No Authority= line exists for an ad-hoc signature; grep's miss must not
  # abort the summary under pipefail.
  auth="$(printf '%s\n' "$info" | grep '^Authority=' | head -1 | cut -d= -f2- || true)"
  team="$(printf '%s\n' "$info" | grep '^TeamIdentifier=' | cut -d= -f2- || true)"
  ver="$(plist_get "$APP/Contents/Info.plist" CFBundleShortVersionString)"
  echo ""
  echo "================= release summary ================="
  echo "identity      : ${auth:-$IDENTITY_LABEL}"
  echo "team          : ${team:-not set}"
  echo "app           : $APP (version ${ver:-?}, $NESTED_COUNT nested Mach-O signed)"
  echo "notarization  : app=$NOTARY_ID_APP  dmg=$NOTARY_ID_DMG"
  # Keyed on DMG_BUILT, not on the file existing: a stale DMG from an
  # earlier --sign-only run would otherwise be reported (size, sha256) as
  # if it were this run's notarized artifact.
  if [ "$DMG_BUILT" = 1 ]; then
    # Apparent size, not allocated blocks: plain `du -h` reported 142M for the
    # 126M image after stapler rewrote it (APFS block slack), which disagreed
    # with build_dmg.sh's own line and with what the sha256 covers.
    size="$(du -Ah "$DMG" | cut -f1), $(stat -f %z "$DMG") bytes"
    sha="$(shasum -a 256 "$DMG" | cut -d' ' -f1)"
    echo "dmg           : $DMG ($size)"
    echo "sha256        : $sha"
  else
    echo "dmg           : (skipped — not built this run)"
  fi
  echo "gatekeeper app: $VERDICT_APP"
  echo "gatekeeper dmg: $VERDICT_DMG"
  echo "==================================================="
}

print_sign_only_next_steps() {
  echo ""
  if [ "$IS_ADHOC" = 1 ]; then
    log "DRY RUN complete. Nothing was notarized: an ad-hoc signature has no Developer ID and no timestamp, so Apple cannot notarize it."
    [ "$DMG_BUILT" = 1 ] && log "The DMG above is a dry-run artifact; the real run rebuilds it from the STAPLED app."
    log "Next (real identity, one-time setup in CLAUDE.md → 'Notarized release'):"
    echo "        bash build_notarize.sh --build"
  else
    log "signed but NOT notarized (--sign-only). Do not publish anything from this run. Next:"
    [ "$DMG_BUILT" = 1 ] && log "(the DMG above will be rebuilt from the stapled app)"
    echo "        bash build_notarize.sh"
    log "(re-signing is idempotent — the run above is repeated, then notarization continues)"
  fi
}

# A DMG left in place by an earlier --sign-only run is signed but NOT
# notarized; with --skip-dmg nothing in this run will touch it, and it is
# exactly the file a hurried operator would publish. Say so up front.
warn_stale_dmg() {
  [ "$SKIP_DMG" = 1 ] && [ -f "$DMG" ] || return 0
  log "WARNING: --skip-dmg, but $DMG already exists from an earlier run. It will NOT be rebuilt, notarized or stapled — do not publish it."
}

main() {
  parse_args "$@"
  resolve_identity
  warn_stale_dmg
  make_tmp
  if [ "$DO_BUILD" = 1 ]; then run_build; fi
  require_app
  sign_app
  assess app informational
  if [ "$SIGN_ONLY" = 1 ] || [ "$IS_ADHOC" = 1 ]; then
    if [ "$SKIP_DMG" = 0 ]; then
      make_dmg
      sign_dmg
      assess dmg informational
    fi
    print_summary
    print_sign_only_next_steps
    exit 0
  fi
  notarize "$APP" app
  staple_and_validate "$APP"
  assess app required
  if [ "$SKIP_DMG" = 0 ]; then
    make_dmg
    sign_dmg
    notarize "$DMG" dmg
    staple_and_validate "$DMG"
    assess dmg required
  fi
  print_summary
  if [ "$DMG_BUILT" = 1 ]; then
    log "done — publish $DMG"
  else
    log "done — app notarized and stapled; no DMG built (--skip-dmg). Run again without --skip-dmg to produce the publishable DMG."
  fi
}

main "$@"
