import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge Tailwind class strings safely.
 *
 * `clsx` handles falsy / conditional values; `twMerge` resolves conflicts
 * (e.g. `px-2 px-4` → `px-4`).
 *
 *   cn("rounded-md", isActive && "bg-accent text-white", className)
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
