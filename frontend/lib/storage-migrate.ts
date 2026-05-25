/**
 * One-time localStorage namespace migration: `travelgpt:*` → `anykb:*`.
 *
 * The app was renamed from "TravelGPT" to "AnyKB" in 2026-05-14. Existing
 * users had auth tokens, conversation history and theme preference stored
 * under the old prefix; we copy them over on first load after the rename
 * so nobody gets logged out / loses chats.
 *
 * Idempotent: once new keys exist the migration short-circuits. Old keys
 * are removed only after the new keys are confirmed written, so a crash
 * mid-migration leaves the user in a recoverable state.
 */

const SENTINEL_KEY = "anykb:_migrated_from_travelgpt";
const OLD_PREFIX = "travelgpt:";
const NEW_PREFIX = "anykb:";

export function migrateLegacyKeys(): void {
  if (typeof window === "undefined") return;
  let ls: Storage;
  try {
    ls = window.localStorage;
  } catch {
    return;
  }

  // Already migrated — short-circuit.
  if (ls.getItem(SENTINEL_KEY)) return;

  const oldKeys: string[] = [];
  for (let i = 0; i < ls.length; i++) {
    const k = ls.key(i);
    if (k && k.startsWith(OLD_PREFIX)) oldKeys.push(k);
  }

  if (oldKeys.length === 0) {
    // Fresh install — nothing to migrate. Still mark sentinel so we don't
    // re-scan on every page load.
    try {
      ls.setItem(SENTINEL_KEY, "1");
    } catch {
      /* ignore */
    }
    return;
  }

  // Phase 1: copy.
  const copied: string[] = [];
  for (const oldKey of oldKeys) {
    const newKey = NEW_PREFIX + oldKey.slice(OLD_PREFIX.length);
    // Don't overwrite if the user somehow already has new-key data
    // (e.g. tested both before this migration shipped).
    if (ls.getItem(newKey) != null) continue;
    const value = ls.getItem(oldKey);
    if (value == null) continue;
    try {
      ls.setItem(newKey, value);
      copied.push(oldKey);
    } catch {
      /* quota — abort the migration mid-flight; will retry next load */
      return;
    }
  }

  // Phase 2: cleanup — delete old keys we successfully copied.
  for (const oldKey of copied) {
    try {
      ls.removeItem(oldKey);
    } catch {
      /* ignore */
    }
  }

  // Phase 3: sentinel so we don't repeat the scan.
  try {
    ls.setItem(SENTINEL_KEY, "1");
  } catch {
    /* ignore */
  }
}
