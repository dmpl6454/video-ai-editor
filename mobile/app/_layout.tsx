/**
 * The root layout: fonts, splash, and the one Stack every screen lives in.
 *
 * The store is hydrated here — before the first screen renders — because a
 * paired phone that flashes the Connect screen for half a second on every cold
 * start looks broken. `hydrate()` reads the saved connection and its keychain
 * token; `app/index.tsx` waits for that and routes once.
 *
 * What it deliberately does NOT wait for is the probe. `hydrate()` starts the
 * `whoami` round trip and returns, because that request has a twelve-second
 * timeout and a Mac with its lid shut consumes every second of it — which used
 * to mean twelve seconds of an empty dark rectangle with no spinner and no
 * text before the Connect screen appeared to say what was wrong. The screen
 * renders first; `ConnectionBar` narrates the probe.
 *
 * A NOTE ON THE URL SCHEME: app.json declares `scheme: "videoaieditor"` and it
 * must stay. It is NOT for deep links — nothing here registers a linking
 * handler and no screen reads route params to pair (a pairing payload is a
 * bearer credential; it only ever arrives via the camera scan or manual entry
 * in lib/pair.ts). The scheme exists because expo-router resolves the app's
 * root URL through expo-linking at startup, and in a RELEASE build with no
 * scheme expo-linking throws ("Cannot make a deep link into a standalone app
 * with no custom scheme defined") before any ErrorBoundary exists — the app
 * opened and closed instantly on the first device install. In development it
 * is only a console warning, which is why no test or dev run ever saw it.
 * __tests__/release/scheme.test.ts pins this.
 */

// Imported one weight at a time, from the per-weight entry points rather than
// the package root. The root re-exports every face a family ships — 44 files,
// about 6 MB of italics and hairline weights this design never uses — and
// Metro adds all of them to the asset graph. These eight are the whole system.
import { IBMPlexMono_400Regular } from "@expo-google-fonts/ibm-plex-mono/400Regular";
import { IBMPlexMono_500Medium } from "@expo-google-fonts/ibm-plex-mono/500Medium";
import { Inter_400Regular } from "@expo-google-fonts/inter/400Regular";
import { Inter_500Medium } from "@expo-google-fonts/inter/500Medium";
import { Inter_600SemiBold } from "@expo-google-fonts/inter/600SemiBold";
import { Inter_700Bold } from "@expo-google-fonts/inter/700Bold";
import { Sora_600SemiBold } from "@expo-google-fonts/sora/600SemiBold";
import { Sora_700Bold } from "@expo-google-fonts/sora/700Bold";
import { useFonts } from "expo-font";
import { Stack } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { StatusBar } from "expo-status-bar";
import { useEffect, useState } from "react";
import { View } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { color } from "../constants/theme";
import { useStore } from "../lib/store";
import { useAutoReconnect } from "../lib/useAutoReconnect";

export { ErrorBoundary } from "expo-router";

export const unstable_settings = { initialRouteName: "index" };

void SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  // Fonts are bundled in the app; nothing is fetched at runtime. This is a
  // local-first product and it should work with the Wi-Fi router unplugged
  // from the internet, as long as the Mac is on the same LAN.
  const [fontsLoaded, fontError] = useFonts({
    Sora_600SemiBold,
    Sora_700Bold,
    Inter_400Regular,
    Inter_500Medium,
    Inter_600SemiBold,
    Inter_700Bold,
    IBMPlexMono_400Regular,
    IBMPlexMono_500Medium,
  });

  const hydrate = useStore((s) => s.hydrate);
  const [hydrated, setHydrated] = useState(false);

  // Mounted exactly once, here, so the retry loop outlives every navigation.
  // Without it `retrying`, `unreachable` and `throttled` are terminal states
  // whose copy promises a recovery that never arrives — see the file header.
  useAutoReconnect();

  useEffect(() => {
    // hydrate() never rejects — an unreachable Mac is a connection STATE, not
    // an exception — so a catch here would only hide a genuine programmer
    // error. Let one surface through the ErrorBoundary instead.
    void hydrate().finally(() => setHydrated(true));
  }, [hydrate]);

  useEffect(() => {
    if (fontError) throw fontError;
  }, [fontError]);

  useEffect(() => {
    if (fontsLoaded && hydrated) void SplashScreen.hideAsync();
  }, [fontsLoaded, hydrated]);

  if (!fontsLoaded || !hydrated) return <View style={{ flex: 1, backgroundColor: color.bg0 }} />;

  return (
    <SafeAreaProvider>
      <StatusBar style="light" />
      <Stack
        screenOptions={{
          headerShown: false,
          contentStyle: { backgroundColor: color.bg0 },
          animation: "slide_from_right",
        }}
      >
        <Stack.Screen name="index" />
        <Stack.Screen name="connect" options={{ animation: "fade" }} />
        <Stack.Screen name="projects" />
        <Stack.Screen name="edit" />
        <Stack.Screen name="import" options={{ presentation: "modal" }} />
        <Stack.Screen name="ai" options={{ presentation: "modal" }} />
        <Stack.Screen name="chat" options={{ presentation: "modal" }} />
        <Stack.Screen name="export" options={{ presentation: "modal" }} />
        <Stack.Screen name="settings" options={{ presentation: "modal" }} />
      </Stack>
    </SafeAreaProvider>
  );
}
