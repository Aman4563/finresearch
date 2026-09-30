"use client";

// Profile → Notifications: phone and Mac delivery for alerts (monitor/notify.py). Secrets are masked by the API and
// kept server-side when the masked value is sent back.
import {
  BellRing, CircleCheck, CircleX, Dices, Eye, EyeOff, Laptop, Loader2, MessageCircle, RefreshCw, Save, Send, Smartphone,
} from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useState } from "react";

import { Labelled, Switch } from "@/components/profile/common";
import { Badge, Button, Callout, Card, cx, ErrorNote, Field, InfoTip, inputClass, Segmented, Skeleton } from "@/components/ui";
import { api, type Channel, type Delivery, type NotificationSettings, useApi, when } from "@/lib/api";

const CH: Record<Channel, string> = { ntfy: "ntfy", telegram: "Telegram", macos: "Mac" };

function SecretInput({ label, value, set, placeholder, hint, isSet }: {
  label: ReactNode; value: string; set: (v: string) => void; placeholder?: string; hint?: ReactNode; isSet: boolean;
}) {
  const [show, setShow] = useState(false);
  const masked = value.startsWith("••••");
  return (
    <Field label={label} hint={hint}>
      <div className="relative">
        <input className={cx(inputClass, "num w-full pr-10")} type={show || masked ? "text" : "password"} autoComplete="off" spellCheck={false}
          value={value} placeholder={isSet ? "saved (hidden)" : placeholder} onChange={(e) => set(e.target.value)}
          onFocus={() => masked && set("")} />
        {!masked && value && (
          <button type="button" aria-label={show ? "Hide" : "Show"} onClick={() => setShow(!show)}
            className="absolute inset-y-0 right-2 grid place-items-center text-muted hover:text-foreground">
            {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
          </button>
        )}
      </div>
    </Field>
  );
}

export function NotificationsCard() {
  const { data, error, reload } = useApi<NotificationSettings>("/api/notifications");
  const log = useApi<Delivery[]>("/api/notifications/deliveries?limit=12", 60000);
  const [d, setD] = useState<NotificationSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testing, setTesting] = useState<Channel | null>(null);
  const [testResult, setTestResult] = useState<Partial<Record<Channel, Delivery | string>>>({});

  useEffect(() => {
    if (data && !d) setD(data);
  }, [data, d]);

  if (!d) {
    return (
      <Card icon={<Smartphone className="size-4" />} title="Notifications" subtitle="Alerts on your phone and Mac.">
        {error ? <ErrorNote error={error} onRetry={reload} /> : <Skeleton className="h-64 rounded-xl" />}
      </Card>
    );
  }
  const dirty = JSON.stringify(d) !== JSON.stringify(data);
  const upd = <K extends keyof NotificationSettings>(k: K, patch: Partial<NotificationSettings[K]>) =>
    setD({ ...d, [k]: { ...(d[k] as object), ...patch } });

  const save = async () => {
    setSaving(true);
    try {
      const out = await api<NotificationSettings>("/api/notifications", { method: "PUT", body: JSON.stringify(d) });
      setD(out);
      setSaveError(null);
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
      reload();
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  const test = async (c: Channel) => {
    setTesting(c);
    try {
      const r = await api<Delivery>("/api/notifications/test", { method: "POST", body: JSON.stringify({ channel: c }) });
      setTestResult((x) => ({ ...x, [c]: r }));
      log.reload();
    } catch (e) {
      setTestResult((x) => ({ ...x, [c]: (e as Error).message }));
    } finally {
      setTesting(null);
    }
  };
  const newTopic = async () => {
    const r = await api<{ topic: string }>("/api/notifications/random-topic");
    upd("ntfy", { topic: r.topic });
  };

  const channelHead = (c: Channel, icon: ReactNode, title: string, desc: ReactNode) => {
    const st = data?.[c].status ?? "turned off";
    const res = testResult[c];
    return (
      <div className="space-y-2">
        <div className="flex items-start gap-3">
          <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-brand-soft text-brand">{icon}</span>
          <div className="min-w-0 flex-1">
            <Switch checked={d[c].enabled} onChange={(v) => upd(c, { enabled: v })}
              label={<span className="inline-flex items-center gap-2">{title}<Badge tone={st === "ready" ? "gain" : "neutral"}>{st}</Badge></span>}
              description={desc} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 pl-11">
          <Button variant="secondary" disabled={testing !== null || dirty || st !== "ready"} onClick={() => test(c)}
            title={dirty ? "Save first" : st !== "ready" ? `Not ready: ${st}` : "Send a test notification now"}
            icon={testing === c ? <Loader2 className="size-3.5 animate-spin" /> : <Send className="size-3.5" />}>
            Send test notification
          </Button>
          {res && (typeof res === "string" ? <span className="text-xs text-loss">{res}</span> : res.status === "sent" ? (
            <span className="inline-flex items-center gap-1 text-xs text-gain"><CircleCheck className="size-3.5" />Sent. Check your {CH[c] === "Mac" ? "Mac" : "phone"}.</span>
          ) : (
            <span className="inline-flex items-center gap-1 text-xs text-loss"><CircleX className="size-3.5" />{res.last_error ?? "failed"}</span>
          ))}
        </div>
      </div>
    );
  };

  return (
    <Card icon={<Smartphone className="size-4" />} title="Notifications"
      subtitle="Where alert rules (and, if you like, the monitor’s own alerts) are sent. In-app alerts are always kept."
      help="Secrets (the ntfy topic and token, the Telegram bot token) are stored only in your local database. The app never shows them again and never sends them to the AI."
      actions={dirty ? (
        <Button onClick={save} disabled={saving} icon={saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}>Save</Button>
      ) : saved ? <span className="text-xs text-gain">Saved</span> : undefined}>
      <div className="space-y-6">
        <ErrorNote error={saveError} />

        {/* ntfy */}
        <section className="space-y-3">
          {channelHead("ntfy", <BellRing className="size-4" />, "ntfy push",
            <>Free push app (Android, iOS). Install <span className="font-medium">ntfy</span>, subscribe to your topic, done.</>)}
          {d.ntfy.enabled && (
            <div className="grid gap-3 pl-11 sm:grid-cols-2">
              <div className="space-y-1.5 sm:col-span-2">
                <SecretInput label={<span className="inline-flex items-center gap-1">Topic<InfoTip>On ntfy.sh anyone who knows the topic name can read your alerts, so it works like a password. Use a long random one.</InfoTip></span>}
                  value={d.ntfy.topic} isSet={d.ntfy.topic_set} set={(v) => upd("ntfy", { topic: v })} placeholder="finresearch-…" />
                <button type="button" onClick={newTopic} className="inline-flex items-center gap-1 text-xs text-brand hover:underline">
                  <Dices className="size-3.5" />Generate a random topic
                </button>
              </div>
              <Field label="Server" hint="ntfy.sh, or your own server.">
                <input className={cx(inputClass, "w-full")} value={d.ntfy.server} onChange={(e) => upd("ntfy", { server: e.target.value })} />
              </Field>
              <SecretInput label="Access token (optional)" value={d.ntfy.token} isSet={d.ntfy.token_set} set={(v) => upd("ntfy", { token: v })}
                placeholder="tk_… for a private server" hint="Only for a self-hosted server with access control." />
            </div>
          )}
        </section>

        {/* Telegram */}
        <section className="space-y-3 border-t border-border pt-5">
          {channelHead("telegram", <MessageCircle className="size-4" />, "Telegram bot",
            <>Your own bot messages you. Create it with @BotFather, send it “hi”, then read your chat id from getUpdates.</>)}
          {d.telegram.enabled && (
            <div className="grid gap-3 pl-11 sm:grid-cols-2">
              <SecretInput label="Bot token" value={d.telegram.bot_token} isSet={d.telegram.bot_token_set}
                set={(v) => upd("telegram", { bot_token: v })} placeholder="123456789:AA…" />
              <Field label="Chat id" hint={<>Open api.telegram.org/bot&lt;token&gt;/getUpdates after messaging the bot; use <span className="num">message.chat.id</span>.</>}>
                <input className={cx(inputClass, "num w-full")} value={d.telegram.chat_id} onChange={(e) => upd("telegram", { chat_id: e.target.value.trim() })} placeholder="e.g. 123456789" />
              </Field>
            </div>
          )}
        </section>

        {/* macOS */}
        <section className="space-y-3 border-t border-border pt-5">
          {channelHead("macos", <Laptop className="size-4" />, "Mac notifications",
            <>Notification Centre on this Mac (the one running the monitor). Uses terminal-notifier when installed, else AppleScript.</>)}
          {d.macos.enabled && (
            <div className="pl-11">
              <Switch checked={d.macos.sound} onChange={(v) => upd("macos", { sound: v })} label="Sound for high and urgent alerts" />
            </div>
          )}
        </section>

        {/* general */}
        <section className="space-y-3 border-t border-border pt-5">
          <Field label="Link alerts to" hint="Tapping a notification opens this address. Use your Mac’s LAN or Tailscale address to open it from the phone.">
            <input className={cx(inputClass, "num w-full")} value={d.general.app_url} onChange={(e) => upd("general", { app_url: e.target.value })} />
          </Field>
          <Labelled label={<span className="inline-flex items-center gap-1">Also send the monitor’s alerts<InfoTip>Listing, allotment, rule changes, big moves and results from watched IPOs and stocks. Only new alerts are sent, from the moment you turn this on.</InfoTip></span>}>
            <div className="flex flex-wrap items-center gap-3">
              <Segmented value={d.general.forward_level} onChange={(v) => upd("general", { forward_level: v })}
                options={[{ value: "off", label: "Off" }, { value: "action", label: "Action only" }, { value: "warn", label: "Action + warnings" }]} />
              {d.general.forward_level !== "off" && (["ntfy", "telegram", "macos"] as Channel[]).map((c) => {
                const on = d.general.forward_channels.includes(c);
                return (
                  <button key={c} type="button" aria-pressed={on}
                    onClick={() => upd("general", { forward_channels: on ? d.general.forward_channels.filter((x) => x !== c) : [...d.general.forward_channels, c] })}
                    className={cx("h-8 rounded-lg px-3 text-xs font-medium ring-1 ring-inset transition", on ? "bg-brand-soft text-brand-strong ring-brand/40" : "text-muted ring-border hover:bg-card-hover")}>
                    {CH[c]}
                  </button>
                );
              })}
            </div>
          </Labelled>
          <Callout tone="info">Quiet hours (Time frames &amp; watch, above) hold phone and Mac deliveries until they end, except <span className="font-medium">urgent</span> rules. Pick channels and priority per rule on the{" "}
            <Link href="/rules?kind=stock" className="text-brand underline-offset-2 hover:underline">rules page</Link>.</Callout>
        </section>

        {/* delivery log */}
        <section className="border-t border-border pt-5">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold">Delivery log</h3>
            <button type="button" onClick={log.reload} aria-label="Refresh the delivery log" className="text-muted hover:text-foreground"><RefreshCw className="size-3.5" /></button>
          </div>
          {log.error ? <ErrorNote error={log.error} onRetry={log.reload} /> : !log.data ? <Skeleton className="h-24 rounded-lg" /> : !log.data.length ? (
            <p className="text-sm text-muted">Nothing sent yet. Send a test notification above.</p>
          ) : (
            <ul className="space-y-1.5">
              {log.data.map((x) => (
                <li key={x.id} className="rounded-lg bg-background-subtle/60 px-3 py-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={x.status === "sent" ? "gain" : x.status === "failed" ? "loss" : "neutral"}>{x.status}</Badge>
                    <span className="font-medium">{CH[x.channel]}</span>
                    <span className="min-w-0 flex-1 truncate text-muted">{x.test ? "test" : x.rule_id ? `rule ${x.rule_id}` : x.alert_kind?.replaceAll("_", " ")} · {x.message}</span>
                    <span className="num text-muted">{when(x.sent_at ?? x.created_at)}</span>
                  </div>
                  {(x.last_error || x.held) && (
                    <p className={cx("mt-1 break-words", x.status === "failed" ? "text-loss" : "text-muted")}>
                      {x.held ?? x.last_error}{x.status === "pending" && x.attempts > 0 ? ` · attempt ${x.attempts}, next ${when(x.next_try_at)}` : ""}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </Card>
  );
}
