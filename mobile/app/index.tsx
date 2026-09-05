/**
 * The entry route. Nothing renders here — `_layout.tsx` has already hydrated
 * the store by the time this mounts, so the only job left is to decide which
 * screen the user actually wanted, once, without a flash of the wrong one.
 */

import { Redirect } from "expo-router";

import { useStore } from "../lib/store";

export default function Index() {
  const connected = useStore((s) => s.conn.status === "connected");
  return <Redirect href={connected ? "/projects" : "/connect"} />;
}
