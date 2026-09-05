/**
 * Chat with Claude about the project on your Mac.
 *
 * THIS IS THE ONE SCREEN THAT DOES NOT USE `Screen`. Everything else in the
 * app is a scroll of cards under a title; a transcript has to pin its composer
 * above the keyboard and keep itself scrolled to the newest line, and neither
 * is something the kit's `Screen` can do without being bent out of shape. Every
 * other primitive — the type scale, the cards, the notes, the buttons — is the
 * kit's, so the screen still belongs to the same app.
 *
 * WHAT CLAUDE IS ACTUALLY DOING. It runs on the Mac, with the Mac's API key,
 * and it edits the project by calling the same dispatch tools the AI screen
 * does. Every tool call is shown as it happens, with its arguments — because a
 * chat that silently rewrote someone's timeline would be the least trustworthy
 * thing in this product. When a tool changes the EDL the Mac emits an `op` and
 * this screen refetches, so the rest of the app is not looking at a stale cut.
 *
 * WHEN THE STREAM DIES. `main.py` saves the transcript in a `finally`, so a
 * turn that was cut off mid-answer — and any edits its tools already applied —
 * are on the Mac's disk. The recovery is therefore to REFETCH the history, and
 * never to resend the message: resending would run those tools a second time.
 * The notice on screen says so in as many words, because "try again" is
 * exactly the wrong instinct here.
 *
 * NO API KEY IS NOT AN ERROR STATE OF OURS. `agent/loop.py` answers with one
 * specific sentence naming the file to put the key in; it is shown verbatim
 * rather than rewritten, because it is the Mac's own instruction about the
 * Mac's own filesystem.
 */

import { router } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import {
  Badge,
  Body,
  Button,
  Caption,
  Card,
  Chip,
  Display,
  Kicker,
  MacRequired,
  Mono,
  Note,
  Row,
  Type,
  useAnnounce,
} from "../components/ui";
import { color, HIT_SLOP_MIN, radius, space, type } from "../constants/theme";
import { fetchChatHistory, streamChatTurn } from "../lib/chat";
import {
  applyChatEvent,
  historyToMessages,
  noticeMessage,
  toolCallLine,
  userMessage,
  type ChatMessage,
} from "../lib/chatLog";
import { errorMessage, isCancellation } from "../lib/errors";
import { selectConnectionLine, selectIsConnected, useStore } from "../lib/store";

/**
 * Starters that show what this is for. Real instructions, not lorem — and the
 * chip carries a SHORT label with the full prompt behind it, because a chip
 * does not shrink below its own text and a forty-character one would push the
 * row off the side of the screen.
 */
const SUGGESTIONS: readonly { label: string; prompt: string }[] = [
  { label: "Tighten it up", prompt: "Cut the silences and the filler words out of v1, then add captions" },
  { label: "Write me a hook", prompt: "Suggest three hooks from the transcript and use the strongest one" },
  { label: "Make it vertical", prompt: "Reframe this to 9:16 and then audit the style" },
];

const DROPPED_COPY =
  "The connection dropped mid-answer. Anything your Mac already applied is saved — the transcript above has been reloaded from it. Do not send the same message again; it would run those tools a second time.";

const STOPPED_COPY =
  "Stopped listening. Your Mac may still be finishing the tool it had started; the transcript has been reloaded from it.";

export default function Chat() {
  const client = useStore((s) => s.client);
  const sessionId = useStore((s) => s.sessionId);
  const session = useStore((s) => s.session);
  const refreshEdl = useStore((s) => s.refreshEdl);
  const connected = useStore(selectIsConnected);
  const connectionLine = useStore(selectConnectionLine);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const scroller = useRef<ScrollView>(null);
  // Measured rather than guessed: the header grows with Dynamic Type, and a
  // hardcoded offset would tuck the composer under the keyboard at the larger
  // accessibility sizes — on the one screen whose whole job is typing.
  const [headerHeight, setHeaderHeight] = useState(0);
  const abort = useRef<AbortController | null>(null);
  // Coalesces EDL refetches: a turn can emit half a dozen ops in a second and
  // six overlapping refetches would fight each other for the rate limiter.
  const refreshing = useRef(false);
  const refreshAgain = useRef(false);

  const insets = useSafeAreaInsets();

  const loadHistory = useCallback(async () => {
    if (client === null || sessionId === null) return;
    try {
      const raw = await fetchChatHistory(client, sessionId);
      setMessages(historyToMessages(raw));
      setHistoryError(null);
    } catch (e) {
      setHistoryError(errorMessage(e));
    }
  }, [client, sessionId]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  // Stop listening when the screen goes away. The Mac keeps working — this
  // only releases the socket — and leaving it open would keep a suspended app
  // holding a connection the Mac cannot tell is dead.
  useEffect(() => () => abort.current?.abort(), []);

  const scheduleEdlRefresh = useCallback(() => {
    if (refreshing.current) {
      refreshAgain.current = true;
      return;
    }
    refreshing.current = true;
    void refreshEdl()
      .catch(() => undefined)
      .finally(() => {
        refreshing.current = false;
        if (refreshAgain.current) {
          refreshAgain.current = false;
          scheduleEdlRefresh();
        }
      });
  }, [refreshEdl]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (text === "" || sending || client === null || sessionId === null) return;

    setInput("");
    setMessages((m) => userMessage(m, text));
    setSending(true);

    const controller = new AbortController();
    abort.current = controller;

    try {
      for await (const evt of streamChatTurn({
        client,
        sessionId,
        message: text,
        signal: controller.signal,
      })) {
        if (evt.type === "op") {
          scheduleEdlRefresh();
          continue;
        }
        setMessages((m) => applyChatEvent(m, evt));
      }
    } catch (e) {
      const stopped = isCancellation(e) || controller.signal.aborted;
      // ORDER MATTERS. `loadHistory` REPLACES the transcript with the Mac's own
      // record — which is the point, since the Mac persists the turn in a
      // `finally` and therefore knows more about it than we do — so the notice
      // has to be appended AFTER that, or it would be thrown away with the
      // half-written reply it is there to explain.
      await loadHistory();
      setMessages((m) =>
        noticeMessage(
          m,
          stopped ? "warn" : "danger",
          stopped ? STOPPED_COPY : `${errorMessage(e)} ${DROPPED_COPY}`,
        ),
      );
      scheduleEdlRefresh();
    } finally {
      abort.current = null;
      setSending(false);
    }
  }, [client, input, loadHistory, scheduleEdlRefresh, sending, sessionId]);

  // VoiceOver hears the reply itself, once, when the turn ends. A constant
  // "Claude replied" would be announced for the first turn and then never
  // again, because the string would not have changed.
  const last = messages[messages.length - 1];
  const spokenReply =
    !sending && last !== undefined && last.kind === "assistant"
      ? `Claude replied. ${last.text.slice(0, 240)}`
      : null;
  useAnnounce(spokenReply);

  const canSend = connected && sessionId !== null && input.trim() !== "" && !sending;

  return (
    <View style={{ flex: 1, backgroundColor: color.bg0, paddingTop: insets.top }}>
      <View onLayout={(e) => setHeaderHeight(e.nativeEvent.layout.height)}>
        <Header name={session?.name ?? null} onClose={() => router.back()} />
      </View>

      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
        keyboardVerticalOffset={insets.top + headerHeight}
      >
        <ScrollView
          ref={scroller}
          style={{ flex: 1 }}
          contentContainerStyle={{
            paddingHorizontal: space[5],
            paddingTop: space[4],
            paddingBottom: space[6],
            gap: space[4],
          }}
          keyboardDismissMode="interactive"
          onContentSizeChange={() => scroller.current?.scrollToEnd({ animated: true })}
        >
          {!connected && <MacRequired message={connectionLine} />}

          {connected && sessionId === null && (
            <Note tone="warn" title="No project open">
              Claude edits a project. Open one first and come back.
            </Note>
          )}

          {historyError !== null && (
            <Note tone="warn" title="Could not load the earlier conversation">
              {historyError}
            </Note>
          )}

          {messages.length === 0 && connected && sessionId !== null && (
            <EmptyState onPick={setInput} />
          )}

          {messages.map((m) => (
            <MessageView key={m.seq} message={m} />
          ))}

          {sending && <Caption>Claude is working on your Mac…</Caption>}
        </ScrollView>

        <Composer
          value={input}
          onChange={setInput}
          onSend={() => void send()}
          onStop={() => abort.current?.abort()}
          canSend={canSend}
          sending={sending}
          disabled={!connected || sessionId === null}
          bottomInset={insets.bottom}
        />
      </KeyboardAvoidingView>
    </View>
  );
}

function Header({ name, onClose }: { name: string | null; onClose: () => void }) {
  return (
    <View
      style={{
        flexDirection: "row",
        alignItems: "flex-end",
        gap: space[3],
        paddingHorizontal: space[5],
        paddingBottom: space[3],
        borderBottomWidth: StyleSheet.hairlineWidth,
        borderBottomColor: color.line,
      }}
    >
      <View style={{ flex: 1, gap: space[1] }}>
        <Kicker>Runs on your Mac</Kicker>
        <Display>Chat</Display>
        {name !== null && <Caption>{name}</Caption>}
      </View>
      <Button label="Close" variant="ghost" onPress={onClose} />
    </View>
  );
}

function EmptyState({ onPick }: { onPick: (text: string) => void }) {
  return (
    <Card tone="inset">
      <Body tone="dim">
        Tell Claude what you want done. It runs the same tools you would tap — on your Mac, with
        your Mac’s API key — and shows you every one of them before it changes anything.
      </Body>
      <Row>
        {SUGGESTIONS.map((s) => (
          <Chip key={s.label} label={s.label} onPress={() => onPick(s.prompt)} />
        ))}
      </Row>
    </Card>
  );
}

function MessageView({ message }: { message: ChatMessage }) {
  if (message.kind === "user") {
    return (
      <View style={{ alignItems: "flex-end" }}>
        <View
          style={{
            maxWidth: "88%",
            backgroundColor: color.accent.secondarySoft,
            borderRadius: radius.md,
            borderTopRightRadius: radius.xs,
            paddingHorizontal: space[4],
            paddingVertical: space[3],
          }}
        >
          <Body>{message.text}</Body>
        </View>
      </View>
    );
  }

  if (message.kind === "assistant") {
    return <Body>{message.text}</Body>;
  }

  if (message.kind === "notice") {
    return (
      <Note tone={message.tone} title={message.tone === "danger" ? "Interrupted" : "Stopped"}>
        {message.text}
      </Note>
    );
  }

  return <ToolRow message={message} />;
}

/**
 * One tool call, collapsed to a line you can expand.
 *
 * Arguments are shown because they are the only way to tell "add a hook" from
 * "add THAT hook", and the result is shown because a tool that silently
 * returned nothing looks identical to one that worked.
 */
function ToolRow({ message }: { message: Extract<ChatMessage, { kind: "tool" }> }) {
  const [open, setOpen] = useState(false);
  const pending = message.result === undefined;
  const failed = message.ok === false;
  return (
    <Pressable
      onPress={() => setOpen((o) => !o)}
      accessibilityRole="button"
      accessibilityState={{ expanded: open }}
      accessibilityLabel={`Tool ${message.tool}${pending ? ", running" : failed ? ", failed" : ", finished"}`}
      style={({ pressed }) => ({
        minHeight: HIT_SLOP_MIN,
        justifyContent: "center",
        backgroundColor: pressed ? color.bg2 : color.bg1,
        borderRadius: radius.sm,
        borderLeftWidth: 3,
        borderLeftColor: failed ? color.danger : pending ? color.accent.warn : color.accent.good,
        paddingHorizontal: space[3],
        paddingVertical: space[2],
        gap: space[1],
      })}
    >
      <Row>
        <Badge
          tone={failed ? "danger" : pending ? "warn" : "good"}
          label={failed ? "failed" : pending ? "running" : "done"}
        />
        <View style={{ flex: 1 }}>
          <Type variant="label">{message.tool}</Type>
        </View>
      </Row>
      <Mono numberOfLines={open ? undefined : 1}>{toolCallLine(message.tool, message.args)}</Mono>
      {open && message.result !== undefined && (
        <Mono selectable>{safeJson(message.result)}</Mono>
      )}
    </Pressable>
  );
}

function safeJson(v: unknown): string {
  try {
    return JSON.stringify(v ?? null, null, 2);
  } catch {
    return String(v);
  }
}

function Composer({
  value,
  onChange,
  onSend,
  onStop,
  canSend,
  sending,
  disabled,
  bottomInset,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  canSend: boolean;
  sending: boolean;
  disabled: boolean;
  bottomInset: number;
}) {
  return (
    <View
      style={{
        paddingHorizontal: space[5],
        paddingTop: space[3],
        paddingBottom: bottomInset + space[3],
        borderTopWidth: StyleSheet.hairlineWidth,
        borderTopColor: color.line,
        backgroundColor: color.bg1,
        gap: space[2],
      }}
    >
      <TextInput
        value={value}
        onChangeText={onChange}
        editable={!disabled && !sending}
        multiline
        placeholder={disabled ? "Connect to your Mac first" : "What should Claude do?"}
        placeholderTextColor={color.textDim}
        accessibilityLabel="Message for Claude"
        maxFontSizeMultiplier={type.body.maxScale}
        style={{
          minHeight: HIT_SLOP_MIN,
          maxHeight: 140,
          borderRadius: radius.sm,
          borderWidth: 1,
          borderColor: color.line,
          backgroundColor: color.bg2,
          paddingHorizontal: space[3],
          paddingVertical: space[3],
          color: color.text,
          fontFamily: type.body.family,
          fontSize: type.body.size,
          lineHeight: type.body.lineHeight,
        }}
      />
      {sending ? (
        <Button
          label="Stop listening"
          variant="neutral"
          onPress={onStop}
          hint="Your Mac keeps working; this only stops the phone waiting"
        />
      ) : (
        <Button label="Send" disabled={!canSend} onPress={onSend} />
      )}
    </View>
  );
}
