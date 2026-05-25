"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { migrateLegacyKeys } from "@/lib/storage-migrate";

type Theme = "light" | "dark" | "system";

type Ctx = {
  theme: Theme;
  resolved: "light" | "dark";
  setTheme: (t: Theme) => void;
};

const ThemeContext = createContext<Ctx | null>(null);

const STORAGE_KEY = "anykb:theme";

function readStored(): Theme {
  if (typeof window === "undefined") return "system";
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* ignore */
  }
  return "system";
}

function systemPrefersDark(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function applyClass(resolved: "light" | "dark") {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("dark", resolved === "dark");
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  // SSR-safe initial — actual resolution happens in effect to avoid hydration mismatch.
  const [theme, setThemeState] = useState<Theme>("system");
  const [resolved, setResolved] = useState<"light" | "dark">("light");

  // Mount: hydrate from localStorage + sync the class (the no-flash inline
  // script in layout.tsx has already applied it pre-paint; this just keeps
  // React state in sync). Also run the one-time travelgpt → anykb migration.
  useEffect(() => {
    migrateLegacyKeys();
    const t = readStored();
    setThemeState(t);
    const r: "light" | "dark" =
      t === "system" ? (systemPrefersDark() ? "dark" : "light") : t;
    setResolved(r);
    applyClass(r);
  }, []);

  // Listen to system theme changes when user is on "system".
  useEffect(() => {
    if (theme !== "system" || typeof window === "undefined") return;
    const mql = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (e: MediaQueryListEvent) => {
      const r: "light" | "dark" = e.matches ? "dark" : "light";
      setResolved(r);
      applyClass(r);
    };
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, [theme]);

  const setTheme = (t: Theme) => {
    setThemeState(t);
    try {
      window.localStorage.setItem(STORAGE_KEY, t);
    } catch {
      /* ignore quota / disabled */
    }
    const r: "light" | "dark" =
      t === "system" ? (systemPrefersDark() ? "dark" : "light") : t;
    setResolved(r);
    applyClass(r);
  };

  return (
    <ThemeContext.Provider value={{ theme, resolved, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): Ctx {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    // Soft fallback for use during SSR / outside provider — avoids crashes in
    // components that render before mount.
    return {
      theme: "system",
      resolved: "light",
      setTheme: () => {},
    };
  }
  return ctx;
}
