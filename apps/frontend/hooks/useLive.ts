"use client";

import { create } from "zustand";
import { wsUrl } from "@/services/api";
import type { BalanceSummary, TwinState } from "@/services/types";

export interface LiveEvent {
  type: string;
  data: any;
  receivedAt: number;
}

interface LiveStore {
  state: TwinState | null;
  balance: BalanceSummary | null;
  events: LiveEvent[];
  connected: boolean;
  lastMessageAt: number | null;
  selected: { kind: "tank" | "pipeline" | "pump" | "valve" | "sensor"; id: string } | null;
  setSelected: (s: LiveStore["selected"]) => void;
  ingest: (msg: { type: string; data: any }) => void;
  setConnected: (c: boolean) => void;
}

export const useLive = create<LiveStore>((set) => ({
  state: null,
  balance: null,
  events: [],
  connected: false,
  lastMessageAt: null,
  selected: null,
  setSelected: (selected) => set({ selected }),
  setConnected: (connected) => set({ connected }),
  ingest: (msg) =>
    set((s) => {
      const now = Date.now();
      if (msg.type === "state") {
        return { state: msg.data as TwinState, balance: (msg.data as TwinState).balance ?? s.balance, lastMessageAt: now };
      }
      if (msg.type === "balance") {
        return { balance: msg.data as BalanceSummary, lastMessageAt: now,
          events: [{ ...msg, receivedAt: now }, ...s.events].slice(0, 200) };
      }
      return { events: [{ ...msg, receivedAt: now }, ...s.events].slice(0, 200), lastMessageAt: now };
    }),
}));

let socket: WebSocket | null = null;
let retry = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
let ping: ReturnType<typeof setInterval> | null = null;

/** Open (once) the live WebSocket; reconnects with exponential back-off. */
export function connectLive() {
  if (typeof window === "undefined") return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  const ws = new WebSocket(wsUrl());
  socket = ws;
  ws.onopen = () => {
    retry = 0;
    useLive.getState().setConnected(true);
    if (ping) clearInterval(ping);
    ping = setInterval(() => ws.readyState === WebSocket.OPEN && ws.send("ping"), 20000);
  };
  ws.onmessage = (ev) => {
    try {
      useLive.getState().ingest(JSON.parse(ev.data));
    } catch {
      /* ignore malformed */
    }
  };
  ws.onclose = () => {
    useLive.getState().setConnected(false);
    socket = null;
    if (ping) clearInterval(ping);
    const delay = Math.min(30000, 1000 * 2 ** retry++);
    if (timer) clearTimeout(timer);
    timer = setTimeout(connectLive, delay);
  };
  ws.onerror = () => ws.close();
}

export function disconnectLive() {
  if (timer) clearTimeout(timer);
  socket?.close();
  socket = null;
}
