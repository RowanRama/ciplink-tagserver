import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { TagStore, type ServerInfo, type TagState, type WriteResult } from "./store.js";

const Context = createContext<TagStore | null>(null);

export interface TagServerProviderProps {
  /** WebSocket URL, e.g. "ws://127.0.0.1:8765/ws" */
  url: string;
  token: string;
  /** called with the PLC name when its tag table was reloaded (fetch /tags again) */
  onMeta?: (plc: string) => void;
  /** the current route path, e.g. "/line1": the server learns each page's tags, which <TagLink> prefetches */
  page?: string;
  children?: ReactNode;
}

/** Owns one WebSocket to the tag server for everything below it. */
export function TagServerProvider({ url, token, onMeta, page, children }: TagServerProviderProps) {
  const store = useMemo(() => new TagStore(url, token), [url, token]);
  store.options.onMeta = onMeta;
  store.page = page; // set while rendering, before the new page's components subscribe
  useEffect(() => {
    store.connect();
    return () => store.close();
  }, [store]);
  return <Context.Provider value={store}>{children}</Context.Provider>;
}

export function useTagStore(): TagStore {
  const store = useContext(Context);
  if (!store) throw new Error("ciplink: wrap the app in <TagServerProvider>");
  return store;
}

/** Subscribes the tag while mounted; re-renders only when this tag changes. */
export function useTag(name: string, options?: { tier?: number }): TagState {
  const store = useTagStore();
  const tier = options?.tier ?? 1;
  useEffect(() => store.acquire(name, tier), [store, name, tier]);
  const listen = useCallback((cb: () => void) => store.listen(name, cb), [store, name]);
  const get = () => store.get(name);
  return useSyncExternalStore(listen, get, get);
}

/** Several tags at once, as a record keyed by name. */
export function useTags(names: string[], options?: { tier?: number }): Record<string, TagState> {
  const store = useTagStore();
  const tier = options?.tier ?? 1;
  const key = names.join("\n");
  useEffect(() => {
    const releases = names.map((name) => store.acquire(name, tier));
    return () => releases.forEach((release) => release());
  }, [store, key, tier]); // eslint-disable-line react-hooks/exhaustive-deps
  const listen = useCallback(
    (cb: () => void) => {
      const offs = names.map((name) => store.listen(name, cb));
      return () => offs.forEach((off) => off());
    },
    [store, key], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const cache = useRef<{ key: string; states: TagState[]; record: Record<string, TagState> } | null>(null);
  const get = () => {
    const states = names.map((name) => store.get(name));
    const c = cache.current;
    if (c && c.key === key && c.states.every((s, i) => s === states[i])) return c.record;
    const record = Object.fromEntries(names.map((name, i) => [name, states[i]]));
    cache.current = { key, states, record };
    return record;
  };
  return useSyncExternalStore(listen, get, get);
}

/** Returns an async function that writes the tag and resolves with the server's ack. */
export function useWriteTag(name: string): (value: unknown) => Promise<WriteResult> {
  const store = useTagStore();
  return useCallback((value: unknown) => store.write(name, value), [store, name]);
}

/** Connection state, PLC states and whether this token may write. */
export function useTagServer(): ServerInfo {
  const store = useTagStore();
  const listen = useCallback((cb: () => void) => store.listenInfo(cb), [store]);
  const get = () => store.info;
  return useSyncExternalStore(listen, get, get);
}
