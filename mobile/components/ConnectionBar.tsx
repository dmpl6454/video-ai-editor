/**
 * The one-line answer to "is my Mac there?", pinned above every working
 * screen.
 *
 * This app is a remote control. If the link state is not on screen at all
 * times, every other failure gets attributed to the wrong thing — a black
 * player reads as a broken render, an empty project list reads as lost work.
 * So the bar is not decorative: it is the piece that keeps the rest of the UI
 * honest, and it renders the sentence `lib/connection.ts` wrote rather than
 * inventing wording per screen.
 *
 * It is deliberately quiet when everything is fine. A green badge and an
 * address, no animation, nothing that competes with the picture below it.
 */

import { router } from "expo-router";
import { View } from "react-native";

import { color, space } from "../constants/theme";
import { connectionMessage, type ConnectionState } from "../lib/connection";
import { Badge, Button, Type } from "./ui";
import type { TintName } from "../constants/theme";

/** `retrying` is deliberately not alarming: iOS is probably showing the Local
 *  Network prompt and the user is two seconds from fixing it themselves. */
function toneFor(status: ConnectionState["status"]): TintName {
  if (status === "connected") return "good";
  if (status === "connecting" || status === "retrying") return "info";
  if (status === "unauthorized" || status === "not_local" || status === "refused") return "danger";
  return "warn";
}

export interface ConnectionBarProps {
  conn: ConnectionState;
  /** Re-probe the Mac. Hidden while connected or mid-attempt. */
  onRetry: () => void;
}

export function ConnectionBar({ conn, onRetry }: ConnectionBarProps) {
  const status = conn.status;
  const canRetry = status !== "connected" && status !== "connecting" && status !== "idle";
  // A token the Mac has revoked cannot be retried into working; the only move
  // left is to pair again, so that is the button we offer instead.
  const needsPairing = status === "unauthorized" || status === "idle";

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
        <Badge tone={toneFor(status)} label={status === "connected" ? "Mac connected" : "Mac not connected"} />
        <Type variant="caption" tone="dim" accessibilityLiveRegion="polite">
          {connectionMessage(conn)}
        </Type>
      </View>
      {needsPairing ? (
        <Button label="Pair" variant="ghost" onPress={() => router.push("/connect")} />
      ) : (
        canRetry && <Button label="Retry" variant="ghost" onPress={onRetry} />
      )}
    </View>
  );
}
