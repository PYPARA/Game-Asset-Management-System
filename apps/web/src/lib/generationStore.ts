import { useSyncExternalStore } from "react";
import { fetchGenerationConversationEvents, subscribeToGenerationConversationEvents } from "./api";
import type { GenerationConversationEvent } from "../types";

type Snapshot = { events: GenerationConversationEvent[]; connected: boolean; cursor: number; error: string };
/** Raw audit events are durable on the server. Build text once per render batch. */
export class ConversationProjection {
  private rows = new Map<string, GenerationConversationEvent>();
  private seen = new Set<string>();
  private streams = new Map<string, Map<number, string>>();
  private dirtyStreams = new Set<string>();
  cursor = 0;
  apply(event: GenerationConversationEvent) {
    if (this.seen.has(event.id)) return;
    this.seen.add(event.id);
    this.cursor = Math.max(this.cursor, event.sequence);
    if (event.event_type === "assistant.delta") {
      const key = `${event.turn_id}:${String(event.data.item_id ?? event.data.itemId ?? "assistant")}`;
      const chunks = this.streams.get(key) ?? new Map<number, string>();
      chunks.set(event.sequence, String(event.data.content ?? event.data.text ?? ""));
      this.streams.set(key, chunks);
      const prior = this.rows.get(key);
      if (!prior || event.sequence < prior.sequence) this.rows.set(key, { ...event, id: key });
      this.dirtyStreams.add(key);
    } else this.rows.set(event.id, event);
  }
  values() {
    for (const key of this.dirtyStreams) {
      const row = this.rows.get(key)!;
      this.rows.set(key, {...row, data: {...row.data, content: [...this.streams.get(key)!].sort((a,b)=>a[0]-b[0]).map(x=>x[1]).join("")}});
    }
    this.dirtyStreams.clear();
    return [...this.rows.values()].sort((a,b) => a.sequence-b.sequence);
  }
}
const stores = new Map<string, ReturnType<typeof createStore>>();
function createStore(id: string) {
  const projection = new ConversationProjection();
  let snapshot: Snapshot = { events: [], connected: false, cursor: 0, error: "" };
  const listeners = new Set<() => void>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disconnect: (()=>void) | undefined;
  let pending: GenerationConversationEvent[] = [];
  const publish = () => {
    if (timer) clearTimeout(timer);
    timer = undefined;
    for (const event of pending) projection.apply(event);
    pending = [];
    snapshot = { ...snapshot, events: projection.values(), cursor: projection.cursor };
    listeners.forEach(listener => listener());
  };
  const queue = (events: GenerationConversationEvent[]) => {
    pending.push(...events);
    timer ??= setTimeout(publish, 50);
  };
  return {
    reset: () => { disconnect?.(); if(timer) clearTimeout(timer); },
    snapshot: () => snapshot,
    subscribe(listener: () => void) {
      listeners.add(listener);
      if (!disconnect) {
        void fetchGenerationConversationEvents(id, projection.cursor).then(queue).catch(error => {
          snapshot = { ...snapshot, error: String(error) }; publish();
        });
        disconnect = subscribeToGenerationConversationEvents(id, event => queue([event]), connected => {
          snapshot = { ...snapshot, connected }; publish();
        }, projection.cursor);
      }
      return () => {
        listeners.delete(listener);
        if (!listeners.size) {
          disconnect?.(); disconnect = undefined;
          // The projection survives navigation; worker execution is independent.
          publish();
        }
      };
    },
  };
}
const empty: Snapshot = { events: [], connected: false, cursor: 0, error: "" };
const noopSubscribe = () => () => {};
const emptySnapshot = () => empty;
export function useGenerationEvents(id: string | null | undefined) {
  if (id && !stores.has(id)) stores.set(id, createStore(id));
  const store = id ? stores.get(id)! : null;
  return useSyncExternalStore(store?.subscribe ?? noopSubscribe, store?.snapshot ?? emptySnapshot, emptySnapshot);
}
export function resetGenerationStores() { for(const store of stores.values()) store.reset(); stores.clear(); }
