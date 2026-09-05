/**
 * The one place that actually retries a failed connection.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `lib/connection.ts` has always exported `shouldAutoRetry()` and
 * `retryDelay()`, with unit tests, a documented backoff curve, and three
 * statuses (`retrying`, `unreachable`, `throttled`) whose copy promises a
 * recovery. Nothing in `app/`, `components/` or `lib/` ever called either
 * function. They were dead code, and the states they governed were terminal:
 *
 *   * FIRST PAIRING. The first request to a LAN address triggers the iOS Local
 *     Network permission prompt, and iOS fails that request immediately rather
 *     than holding it. `reduceFailure` diagnoses it as `retry` (the grace
 *     window has not elapsed), so the screen settles on the reassuring
 *     "Looking for your Mac at 192.168.1.20…" — and then never dialled again.
 *     The user taps Allow and watches a calm sentence forever.
 *
 *   * A 429 BURST. One filmstrip repaint racing an ops poll trips the Mac's
 *     rate limiter. `throttled` turns `mediaEnabled()` off, so the player and
 *     every thumbnail stop, under a bar that reads "Pausing for a moment." It
 *     never un-paused.
 *
 *   * A SLEEPING MAC. `unreachable` backs off from 2 s toward 30 s — a curve
 *     that was computed and never used, so a Mac waking up was only noticed if
 *     the user happened to tap Retry.
 *
 * DESIGN NOTES
 * ------------
 * Mounted once, in `app/_layout.tsx`, rather than per screen: a retry loop that
 * dies with the screen is the same bug in a smaller box, and two screens
 * mounting two loops would double the request rate against a limiter this app
 * is already careful with.
 *
 * It never retries `unauthorized`, `not_local`, `blocked` or `refused` —
 * `shouldAutoRetry` excludes them because nothing changes until the user acts,
 * and hammering a revoked token is how a phone earns the Mac's auth lockout.
 *
 * IT ALSO NEVER RETRIES WITHOUT A CREDENTIAL. This loop is the thing that used
 * to fire the token-less probe during a first pairing: the claim failed while
 * the iOS prompt was up, the reducer said `retrying`, this timer called
 * `probe()`, and the anonymous `whoami` it sent came back 401 the instant the
 * user tapped Allow — telling a phone that had never paired that it had been
 * revoked, and stopping this loop for good (`unauthorized` is not retryable).
 * So the gate below is `selectCanReconnect`, and the call is `reconnect()`,
 * which resumes an unfinished pairing by re-claiming rather than by probing —
 * the claim code is still unburnt when the claim never reached the Mac, and it
 * is the only request that can produce a credential.
 *
 * It never polls in the background. iOS freezes timers on suspend anyway, but
 * the AppState listener also gives us the other half for free: a probe the
 * moment the app comes forward, which is exactly when the user is looking and
 * exactly when the answer is most likely to have changed.
 */

import { useEffect } from "react";
import { AppState, type AppStateStatus } from "react-native";

import { retryDelay, shouldAutoRetry } from "./connection";
import { selectCanReconnect, useStore } from "./store";

/** One question, asked identically by the timer and by the foreground probe. */
function retryable(): boolean {
  const state = useStore.getState();
  return shouldAutoRetry(state.conn) && selectCanReconnect(state);
}

export function useAutoReconnect(): void {
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const clear = () => {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };

    /** Arm (or disarm) the retry for whatever the current state is. */
    const schedule = () => {
      clear();
      if (cancelled) return;
      if (!retryable()) return;
      if (AppState.currentState !== "active") return;
      const conn = useStore.getState().conn;
      timer = setTimeout(() => {
        timer = null;
        // `reconnect()` drives the reducer itself, so the subscription below
        // re-arms us with the NEW state — including the longer backoff that
        // `retryDelay` derives from how long this has been failing.
        void useStore.getState().reconnect();
      }, retryDelay(conn, Date.now()));
    };

    // Re-evaluate whenever the connection changes at all. Cheap: zustand calls
    // this synchronously on set, and `schedule` does nothing but read state and
    // arm one timer.
    const unsubscribe = useStore.subscribe(schedule);

    const onAppState = (state: AppStateStatus) => {
      if (state !== "active") {
        clear();
        return;
      }
      // Coming back to the front. Retry now rather than waiting out a backoff
      // that was measured against time the phone spent asleep — this is also
      // the moment the user has just answered the Local Network prompt, which
      // is precisely when an unfinished pairing becomes claimable.
      if (retryable()) void useStore.getState().reconnect();
      schedule();
    };
    const sub = AppState.addEventListener("change", onAppState);

    schedule();

    return () => {
      cancelled = true;
      clear();
      unsubscribe();
      sub.remove();
    };
  }, []);
}
