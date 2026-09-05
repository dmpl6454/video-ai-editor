/**
 * Pairing with the Mac.
 *
 * This screen carries the whole product's honesty burden. Everything the app
 * can do happens on the Mac; if this screen is vague about whether the Mac is
 * there, every later failure gets blamed on the wrong thing. So it states, in
 * order: that the Mac does the work, what to do on the Mac first, and exactly
 * what went wrong when something does.
 *
 * THREE FAILURE BRANCHES, NOT ONE. iOS reports "blocked by Local Network
 * permission", "denied months ago", "that address can never work" and "the Mac
 * is asleep" with one identical error. `lib/net.ts` reasons them apart and
 * `lib/connection.ts` turns that into a state; this screen only renders it and
 * offers the one action that can fix it. There is no simulator on the machine
 * this ships from, so getting that reasoning right in a unit test was the only
 * defence available.
 */

import { CameraView, useCameraPermissions } from "expo-camera";
import { router } from "expo-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Linking, Platform, View } from "react-native";

import {
  Badge,
  Body,
  Button,
  Caption,
  Card,
  Chip,
  Divider,
  Field,
  Heading,
  Mono,
  Note,
  Row,
  Screen,
  Type,
  useAnnounce,
} from "../components/ui";
import { color, radius, space } from "../constants/theme";
import { connectionMessage } from "../lib/connection";
import { relativeTime } from "../lib/format";
import { DEFAULT_PORT, isValidPort } from "../lib/net";
import { hostAdvisory, pairFailureMessage, parsePairPayload, type PairPayload } from "../lib/pair";
import { useStore } from "../lib/store";
import type { SavedConnection } from "../lib/vault";

type Mode = "scan" | "manual";

/** Tone for the status strip. `retrying` is deliberately NOT alarming — the
 *  permission prompt is probably on screen and the user is about to fix it. */
function statusTone(status: string): "good" | "warn" | "danger" | "info" {
  if (status === "connected") return "good";
  if (status === "connecting" || status === "retrying") return "info";
  if (status === "unauthorized" || status === "not_local" || status === "refused") return "danger";
  return "warn";
}

export default function Connect() {
  const conn = useStore((s) => s.conn);
  const vault = useStore((s) => s.vault);
  const pairWith = useStore((s) => s.pairWith);
  const useSaved = useStore((s) => s.useSaved);
  const forget = useStore((s) => s.forget);
  const probe = useStore((s) => s.probe);

  const [mode, setMode] = useState<Mode>("scan");
  const [permission, requestPermission] = useCameraPermissions();
  const [scanError, setScanError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [address, setAddress] = useState("");
  const [port, setPort] = useState(String(DEFAULT_PORT));
  const [code, setCode] = useState("");

  const line = connectionMessage(conn);
  useAnnounce(line);

  // Once connected there is nothing left to do here. Replace rather than push
  // so Back from the project list does not land on the pairing screen again.
  useEffect(() => {
    if (conn.status === "connected") router.replace("/projects");
  }, [conn.status]);

  const submit = useCallback(
    async (payload: PairPayload) => {
      setBusy(true);
      setScanError(null);
      try {
        await pairWith(payload);
      } finally {
        setBusy(false);
      }
    },
    [pairWith],
  );

  const onScanned = useCallback(
    ({ data }: { data: string }) => {
      if (busy) return; // the camera fires continuously; one scan is enough
      const parsed = parsePairPayload(data);
      if (!parsed.ok) {
        setScanError(pairFailureMessage(parsed.reason));
        return;
      }
      void submit(parsed.payload);
    },
    [busy, submit],
  );

  // Pasting the whole pairing link into the address field fills the rest in.
  // Typing a 43-character token by hand is not a thing anyone should be asked
  // to do, and this is the path someone takes when the camera will not focus.
  const onAddressChange = useCallback((next: string) => {
    const parsed = parsePairPayload(next);
    if (parsed.ok) {
      setAddress(parsed.payload.host);
      setPort(String(parsed.payload.port));
      setCode(parsed.payload.code);
      return;
    }
    setAddress(next);
  }, []);

  const manualPayload = useMemo(() => {
    const portNum = Number(port);
    if (!address.trim() || !code.trim() || !isValidPort(portNum)) return null;
    // Routed through the same parser the scanner uses, so a typed address gets
    // exactly the same host validation a scanned one does — including the
    // refusal to pair with anything off the local network.
    return parsePairPayload(
      `vaepair:h=${encodeURIComponent(address.trim())}&p=${portNum}&c=${encodeURIComponent(code.trim())}`,
    );
  }, [address, port, code]);

  const manualError =
    manualPayload === null || manualPayload.ok ? null : pairFailureMessage(manualPayload.reason);

  // A note for an address that WORKS but depends on something the user should
  // know about — today that means a Tailscale/CGNAT address, which the Mac
  // advertises on purpose and which stops working the moment the VPN drops.
  // Shown for whichever address is in play: the one already connected, or the
  // one about to be submitted.
  const advisory = useMemo(
    () => hostAdvisory(conn.host ?? (manualPayload?.ok ? manualPayload.payload.host : "")),
    [conn.host, manualPayload],
  );

  const canSubmitManual = manualPayload !== null && manualPayload.ok && !busy;

  return (
    <Screen
      kicker="Video AI Editor"
      title="Connect to your Mac"
      lede="Your clips, your edits and every render stay on the Mac. This phone is the remote control — it does no editing of its own."
      header={<StatusStrip line={line} status={conn.status} onRetry={probe} />}
    >
      {conn.status === "blocked" && (
        <Note tone="danger" title="iPhone is blocking the connection">
          <View style={{ gap: space[3] }}>
            <Body tone="dim">
              Turn on Local Network for Video AI Editor, then come back and try again.
            </Body>
            <Button
              label="Open iPhone Settings"
              variant="neutral"
              onPress={() => {
                void Linking.openSettings();
              }}
            />
          </View>
        </Note>
      )}

      {vault.connections.length > 0 && (
        <Card>
          <Heading>Saved Macs</Heading>
          <View style={{ gap: space[2] }}>
            {vault.connections.map((saved, i) => (
              <View key={saved.id} style={{ gap: space[2] }}>
                {i > 0 && <Divider />}
                <SavedRow
                  saved={saved}
                  busy={busy}
                  active={conn.connectionId === saved.id}
                  onUse={() => void useSaved(saved)}
                  onForget={() => void forget(saved.id)}
                />
              </View>
            ))}
          </View>
        </Card>
      )}

      <Card>
        <Heading>Pair a Mac</Heading>
        <Body tone="dim">
          On the Mac, open Video AI Editor and choose Phone. Turn on local network mode — it is off
          until you turn it on — then show the pairing code. A code works once and expires after ten
          minutes.
        </Body>

        <Row accessibilityRole="radiogroup">
          <Chip label="Scan the code" kind="choice" selected={mode === "scan"} onPress={() => setMode("scan")} />
          <Chip label="Type it in" kind="choice" selected={mode === "manual"} onPress={() => setMode("manual")} />
        </Row>

        {mode === "scan" ? (
          <Scanner
            granted={permission?.granted === true}
            canAsk={permission === null || permission.canAskAgain}
            busy={busy}
            error={scanError}
            onRequest={() => void requestPermission()}
            onScanned={onScanned}
          />
        ) : (
          <View style={{ gap: space[3] }}>
            <Field
              label="Mac address"
              value={address}
              onChangeText={onAddressChange}
              placeholder="192.168.1.20"
              keyboardType="url"
              hint="Paste the whole pairing string here and the other two fill themselves in."
            />
            <Field
              label="Port"
              value={port}
              onChangeText={setPort}
              placeholder={String(DEFAULT_PORT)}
              keyboardType="numeric"
            />
            <Field
              label="Pairing code"
              value={code}
              onChangeText={setCode}
              placeholder="32 letters and digits, shown under the code"
              error={manualError}
            />
            <Button
              label={busy ? "Connecting…" : "Connect"}
              busy={busy}
              disabled={!canSubmitManual}
              onPress={() => {
                if (manualPayload?.ok) void submit(manualPayload.payload);
              }}
            />
          </View>
        )}
      </Card>

      {advisory !== null && (
        <Note tone="info" title="This is a VPN address">
          <Body tone="dim">{advisory}</Body>
        </Note>
      )}

      <Card tone="inset">
        <Heading>What crosses the Wi-Fi</Heading>
        <Body tone="dim">
          The link between this phone and your Mac is not encrypted. Anyone who can watch the
          network can see the video you preview and could reuse this phone&rsquo;s access to the
          Mac. Pair on a network you trust — home or office, not a café or a hotel — and remove the
          phone from the Mac&rsquo;s Phone panel if you ever pair somewhere you would rather not
          have.
        </Body>
      </Card>

      <Card tone="inset">
        <Heading>Why the Mac has to be on</Heading>
        <Body tone="dim">
          Every tool — captions, background removal, upscaling, export — runs on the Mac using its
          models and its ffmpeg. Nothing is uploaded to a server, and nothing runs here. When the Mac
          is asleep or the app is closed, this phone can show you what it last saw and nothing more.
        </Body>
        {conn.whoami !== null && (
          <Caption>
            {`That Mac runs ${conn.whoami.server.job_workers} render job${conn.whoami.server.job_workers === 1 ? "" : "s"} at a time, shared with whoever is sitting at it.`}
          </Caption>
        )}
      </Card>
    </Screen>
  );
}

function StatusStrip({ line, status, onRetry }: { line: string; status: string; onRetry: () => void }) {
  const tone = statusTone(status);
  const canRetry = status !== "connected" && status !== "connecting" && status !== "idle";
  return (
    <View
      style={{
        flexDirection: "row",
        alignItems: "center",
        gap: space[3],
        paddingHorizontal: space[5],
        paddingVertical: space[3],
        backgroundColor: color.bg1,
        borderBottomWidth: 1,
        borderBottomColor: color.line,
      }}
    >
      <View style={{ flex: 1, gap: space[1] }}>
        <Badge tone={tone} label={status === "connected" ? "Connected" : "Not connected"} />
        <Type variant="caption" tone="dim" accessibilityLiveRegion="polite">
          {line}
        </Type>
      </View>
      {canRetry && <Button label="Retry" variant="ghost" onPress={onRetry} />}
    </View>
  );
}

function SavedRow({
  saved,
  active,
  busy,
  onUse,
  onForget,
}: {
  saved: SavedConnection;
  active: boolean;
  busy: boolean;
  onUse: () => void;
  onForget: () => void;
}) {
  return (
    <View style={{ gap: space[2] }}>
      <View style={{ gap: 2 }}>
        <Type variant="label">{saved.host}</Type>
        <Mono>{`port ${saved.port}`}</Mono>
        <Caption>
          {saved.lastConnectedAt === null
            ? "Never connected"
            : `Last reached ${relativeTime(saved.lastConnectedAt)}`}
        </Caption>
      </View>
      <Row>
        <Button
          label={active ? "Reconnect" : "Connect"}
          variant="primary"
          disabled={busy}
          onPress={onUse}
          style={{ flexGrow: 1 }}
        />
        <Button label="Remove" variant="ghost" disabled={busy} onPress={onForget} />
      </Row>
    </View>
  );
}

function Scanner({
  granted,
  canAsk,
  busy,
  error,
  onRequest,
  onScanned,
}: {
  granted: boolean;
  canAsk: boolean;
  busy: boolean;
  error: string | null;
  onRequest: () => void;
  onScanned: (r: { data: string }) => void;
}) {
  if (!granted) {
    return (
      <View style={{ gap: space[3] }}>
        <Body tone="dim">
          The camera is used once, to read the pairing code on your Mac. It never records.
        </Body>
        {canAsk ? (
          <Button label="Allow the camera" onPress={onRequest} />
        ) : (
          <View style={{ gap: space[3] }}>
            <Note tone="warn">
              Camera access is turned off for this app, so the code cannot be scanned. Type it in
              instead, or turn the camera back on in Settings.
            </Note>
            <Button
              label="Open iPhone Settings"
              variant="neutral"
              onPress={() => {
                void Linking.openSettings();
              }}
            />
          </View>
        )}
      </View>
    );
  }

  return (
    <View style={{ gap: space[2] }}>
      <View
        accessible
        accessibilityLabel="Camera viewfinder. Point it at the pairing code on your Mac."
        style={{
          height: 260,
          borderRadius: radius.md,
          overflow: "hidden",
          borderWidth: 1,
          borderColor: color.lineStrong,
          backgroundColor: color.bg2,
        }}
      >
        {/* Web has no camera module in this app; the manual path covers it and
            every other case where the viewfinder cannot run. */}
        {Platform.OS !== "web" && (
          <CameraView
            style={{ flex: 1 }}
            facing="back"
            barcodeScannerSettings={{ barcodeTypes: ["qr"] }}
            onBarcodeScanned={busy ? undefined : onScanned}
          />
        )}
      </View>
      {error !== null ? (
        <Type variant="caption" tone="warn" accessibilityLiveRegion="polite">
          {error}
        </Type>
      ) : (
        <Caption>Point the camera at the code on your Mac.</Caption>
      )}
    </View>
  );
}
