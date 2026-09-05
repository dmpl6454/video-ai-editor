/**
 * Settings and About.
 *
 * This screen is where the app is allowed to be boring and is required to be
 * exact. Everything on it is a fact the user might need in order to work out
 * why something else is not working, or to undo the trust they extended when
 * they scanned a pairing code:
 *
 *   - which Mac this phone is talking to, and whether it is answering;
 *   - what that Mac says about itself (version, how many renders it runs at
 *     once, how big an upload it will accept) — all read from
 *     `GET /api/pair/whoami`, never assumed;
 *   - what this phone is called in that Mac's paired-device list, which is the
 *     only way someone with two phones can revoke the right one;
 *   - how to end the pairing, and — the honest part — what ending it from
 *     HERE does and does not do;
 *   - the shared-undo semantics, because the Mac and the phone drive ONE store
 *     and ONE undo stack, and a phone Undo can take back an edit made at the
 *     Mac thirty seconds ago. That is surprising enough to be worth writing
 *     down rather than letting someone discover it.
 *
 * ON "REVOKE". A device is revoked on the MAC (`POST /api/pair/revoke`, which
 * `api/pair_routes.py` restricts to loopback so a paired phone can never
 * unpair another phone). What this screen can do is destroy this phone's copy
 * of the credential, which stops this phone using it — and it says exactly
 * that instead of claiming a revocation it cannot perform.
 */

import Constants from "expo-constants";
import { router } from "expo-router";
import { useCallback, useMemo, useState } from "react";
import { Alert, View } from "react-native";

import {
  Badge,
  Body,
  Button,
  Caption,
  Card,
  Divider,
  Heading,
  Mono,
  Note,
  Row,
  Screen,
  Stat,
  Type,
} from "../components/ui";
import { color, space } from "../constants/theme";
import { connectionMessage } from "../lib/connection";
import { humanBytes, relativeTime, relativeTimeFromEpochSeconds } from "../lib/format";
import { useStore } from "../lib/store";
import { activeConnection } from "../lib/vault";

export default function Settings() {
  const conn = useStore((s) => s.conn);
  const vault = useStore((s) => s.vault);
  const probe = useStore((s) => s.probe);
  const forget = useStore((s) => s.forget);

  const [checking, setChecking] = useState(false);

  const saved = useMemo(() => activeConnection(vault), [vault]);
  const server = conn.whoami?.server ?? null;
  const device = conn.whoami?.device ?? null;
  const connected = conn.status === "connected";

  const appVersion = Constants.expoConfig?.version ?? "unknown";

  const onCheck = useCallback(async () => {
    setChecking(true);
    try {
      await probe();
    } finally {
      setChecking(false);
    }
  }, [probe]);

  const onUnpair = useCallback(() => {
    if (!saved) return;
    Alert.alert(
      `Stop using ${saved.host}?`,
      "This phone deletes its copy of the pairing token, so it can no longer reach that Mac. The Mac keeps its record of this phone until you remove it there, in the Phone panel. Your projects are not touched.",
      [
        { text: "Keep it", style: "cancel" },
        {
          text: "Delete the pairing",
          style: "destructive",
          onPress: () => {
            void forget(saved.id).then(() => router.replace("/connect"));
          },
        },
      ],
    );
  }, [saved, forget]);

  return (
    <Screen
      kicker="Settings"
      title="This phone and your Mac"
      lede="Every fact on this screen is read from the Mac you are paired with, not assumed."
      header={<TopStrip onClose={() => router.back()} />}
      inset="none"
    >
      <Card>
        <Row style={{ justifyContent: "space-between" }}>
          <Heading>Connection</Heading>
          <Badge tone={connected ? "good" : "warn"} label={connected ? "Connected" : "Not connected"} />
        </Row>
        <Body tone="dim" accessibilityLiveRegion="polite">
          {connectionMessage(conn)}
        </Body>
        {saved !== null && (
          <View style={{ gap: space[1] }}>
            <Mono>{`${saved.host}:${saved.port}`}</Mono>
            <Caption>
              {saved.lastConnectedAt === null
                ? "Not reached yet."
                : `Last reached ${relativeTime(saved.lastConnectedAt)}.`}
            </Caption>
          </View>
        )}
        <Row>
          <Button
            label={checking ? "Checking…" : "Check now"}
            variant="neutral"
            busy={checking}
            onPress={() => void onCheck()}
            style={{ flexGrow: 1 }}
          />
          <Button
            label="Pair another Mac"
            variant="ghost"
            onPress={() => router.push("/connect")}
            style={{ flexGrow: 1 }}
          />
        </Row>
      </Card>

      <Card>
        <Heading>What that Mac reports</Heading>
        {server === null ? (
          <Body tone="dim">
            Nothing yet — these are read from the Mac when this phone reaches it.
          </Body>
        ) : (
          <>
            <Row>
              <Stat value={server.version} label="Mac app" />
              <Stat value={String(server.job_workers)} label="Renders at once" />
              <Stat value={humanBytes(server.max_upload_bytes)} label="Upload limit" />
            </Row>
            <Caption>
              {`Those ${server.job_workers} render slots are shared with whoever is sitting at the Mac. An export or an upscale started there will make one started here wait.`}
            </Caption>
            <Divider />
            <Row>
              <Type variant="label" tone="dim">
                Local network mode
              </Type>
              <Badge tone={server.lan_enabled ? "good" : "warn"} label={server.lan_enabled ? "On" : "Off"} />
            </Row>
            <Caption>
              It is off until someone turns it on in the Mac app's Phone panel. With it off, the Mac
              only listens to itself and this phone cannot reach it at all.
            </Caption>
          </>
        )}
      </Card>

      <Card>
        <Heading>This phone, on that Mac</Heading>
        {device === null ? (
          <Body tone="dim">
            The Mac has not identified this phone yet. Connect, and its name for this device shows
            here.
          </Body>
        ) : (
          <View style={{ gap: space[1] }}>
            <Type variant="label">{device.name}</Type>
            <Mono>{device.id}</Mono>
            <Caption>{`Paired ${relativeTimeFromEpochSeconds(device.created_at)}.`}</Caption>
          </View>
        )}
        <Caption>
          That name is what appears in the Mac's Phone panel. If you have more than one phone, it is
          how you tell which is which before removing one.
        </Caption>
      </Card>

      <Card>
        <Heading>Two people, one undo stack</Heading>
        <Body tone="dim">
          The Mac and this phone edit the same project through the same store. There is one undo
          history between you: an Undo here can take back an edit someone just made at the Mac, and
          an edit made there appears here within a few seconds rather than instantly. If you are
          both working at once, say what you are doing.
        </Body>
      </Card>

      <Card>
        <Heading>What runs where</Heading>
        <Body tone="dim">
          Nothing is edited, rendered, transcribed or uploaded to a server by this app. Captions,
          background removal, upscaling, chat and export all run on your Mac, using its models and
          its ffmpeg. Your clips never leave it. When the Mac is asleep or the app is closed, this
          phone can show you what it last saw and nothing more.
        </Body>
        <Caption>
          The pairing token is kept in this iPhone's keychain, tied to this device, and is never
          included in an iCloud backup.
        </Caption>
      </Card>

      <Card>
        <Heading>End the pairing</Heading>
        <Body tone="dim">
          Deleting the pairing removes this phone's copy of the token. It cannot reach that Mac
          again until you scan a new code. Nothing on the Mac is deleted — no projects, no renders —
          and the Mac keeps its own record of this phone until you remove it in its Phone panel.
        </Body>
        <Button
          label="Delete the pairing on this phone"
          variant="danger"
          disabled={saved === null}
          onPress={onUnpair}
          hint="Asks for confirmation first."
        />
        {saved === null && <Note tone="warn">There is no saved pairing on this phone.</Note>}
      </Card>

      <Card tone="inset">
        <Heading>About</Heading>
        <Row>
          <Stat value={appVersion} label="This app" />
          <Stat value={server?.version ?? "—"} label="Mac app" />
        </Row>
        <Caption>
          Both ends ship together. If these two numbers disagree, update whichever is behind — the
          companion is written against the Mac app's API of the same version.
        </Caption>
      </Card>
    </Screen>
  );
}

/** A modal presentation gets no native header, so it brings its own. */
function TopStrip({ onClose }: { onClose: () => void }) {
  return (
    <View
      style={{
        flexDirection: "row",
        alignItems: "center",
        justifyContent: "space-between",
        paddingHorizontal: space[5],
        paddingVertical: space[3],
        backgroundColor: color.bg1,
        borderBottomWidth: 1,
        borderBottomColor: color.line,
      }}
    >
      <Type variant="label" tone="dim">
        Settings
      </Type>
      <Button label="Done" variant="ghost" onPress={onClose} />
    </View>
  );
}
