import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["-apple-system", "BlinkMacSystemFont", "PingFang SC", "Microsoft YaHei", "system-ui", "sans-serif"],
        mono: ["Menlo", "Monaco", "Consolas", "monospace"],
      },
      colors: {
        bg: "rgb(var(--bg) / <alpha-value>)",
        fg: "rgb(var(--fg) / <alpha-value>)",
        muted: "rgb(var(--muted) / <alpha-value>)",
        accent: "rgb(var(--accent) / <alpha-value>)",
        surface: "rgb(var(--surface) / <alpha-value>)",
        "surface-2": "rgb(var(--surface-2) / <alpha-value>)",
        border: "rgb(var(--border) / <alpha-value>)",
        "border-strong": "rgb(var(--border-strong) / <alpha-value>)",
        success: "rgb(var(--success) / <alpha-value>)",
        warning: "rgb(var(--warning) / <alpha-value>)",
        danger: "rgb(var(--danger) / <alpha-value>)",
        info: "rgb(var(--info) / <alpha-value>)",
      },
      borderColor: {
        DEFAULT: "rgb(var(--border) / <alpha-value>)",
      },
      boxShadow: {
        "soft": "0 1px 2px 0 rgb(var(--fg) / 0.04), 0 1px 1px 0 rgb(var(--fg) / 0.02)",
        "lift": "0 4px 12px -2px rgb(var(--fg) / 0.08), 0 2px 4px 0 rgb(var(--fg) / 0.04)",
      },
    },
  },
  plugins: [],
};
export default config;
