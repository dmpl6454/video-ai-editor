/**
 * Where an unmatched URL lands — and the reason this file exists at all is
 * security, not aesthetics.
 *
 * app.json declares `scheme: "videoaieditor"` because a release build without
 * one crashes at launch (see `app/_layout.tsx`), and `expo prebuild` registers
 * the bundle identifier as a scheme regardless. So `videoaieditor://anything`
 * IS a way into this app from any web page the user visits, and the app's
 * answer to it has to be a dead end. Two built-in routes were not:
 *
 *   1. `_sitemap` — expo-router adds it to the linking table with no __DEV__
 *      or NODE_ENV gate (`getLinkingConfig.js::getNavigationConfig`), so
 *      `videoaieditor://_sitemap` opened a listing of every route in a SHIPPED
 *      build. Turned off with the supported knob, `extra.router.sitemap: false`
 *      in app.json, which expo-router reads in `global-state/useStore.js` and
 *      `global-state/utils.js::shouldAppendSitemap` — one config value rather
 *      than a patch of theirs, so an expo-router bump cannot quietly undo it.
 *
 *   2. `+not-found` — expo-router injects its own `Unmatched` view when the
 *      app supplies none (`getRoutesCore.js::appendNotFoundRoute`). That view
 *      prints the incoming URL back to the screen and renders a hardcoded
 *      `<Link href="/_sitemap">Sitemap</Link>`, in production as much as in
 *      development. This file is what `appendNotFoundRoute` checks for and
 *      skips, so shipping it is the least invasive way to replace that screen:
 *      no fork, no patch, and it keeps working when their view changes.
 *
 * WHAT THIS SCREEN DELIBERATELY DOES NOT DO: read the URL. No
 * `useLocalSearchParams`, no `usePathname`, nothing rendered from the path.
 * Echoing an attacker-authored URL into the UI is how a dead end turns into a
 * phishing surface ("your session expired, open …"), and there is nothing a
 * user could do with the text anyway. The route also stays enabled rather than
 * being turned off with `extra.router.notFound: false`, because a URL with no
 * matching route and no catch-all is an unhandled navigation action, and this
 * screen is a better answer than whatever that leaves on screen.
 */

import { router } from "expo-router";

import { Body, Button, Card, Heading, Screen } from "../components/ui";

export default function NotFound() {
  return (
    <Screen
      kicker="Video AI Editor"
      title="Nothing here"
      lede="That link does not open anything in this app."
    >
      <Card>
        <Heading>This app has no links</Heading>
        <Body tone="dim">
          Video AI Editor is paired with your Mac by scanning the code in the Mac app&apos;s Phone
          panel, or by typing that address in by hand. Nothing else can point this phone at a Mac —
          which is why a link like the one you followed does nothing.
        </Body>
      </Card>
      <Button label="Go to the app" onPress={() => router.replace("/")} />
    </Screen>
  );
}
