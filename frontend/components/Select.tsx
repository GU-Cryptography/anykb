"use client";

import { ChevronDown } from "lucide-react";
import {
  forwardRef,
  type SelectHTMLAttributes,
} from "react";
import { cn } from "@/lib/cn";

export type SelectOption = {
  value: string;
  label: string;
  prefix?: string; // e.g. emoji "🔒" / "📚"
};

type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, "size"> & {
  options: SelectOption[];
  size?: "sm" | "md";
  /** Optional first option (placeholder), e.g. `{ value: "", label: "不绑定" }` */
  placeholderOption?: SelectOption;
};

/**
 * Styled native <select> — appearance-none + custom chevron, dark-mode safe.
 *
 * Keeps a11y / mobile native picker. For richer dropdowns (search, multi-select,
 * keyboard nav beyond native) consider Headless UI later.
 */
const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { options, size = "md", placeholderOption, className, ...rest },
  ref
) {
  const allOptions = placeholderOption ? [placeholderOption, ...options] : options;
  return (
    <div className="relative inline-flex items-center">
      <select
        ref={ref}
        {...rest}
        className={cn(
          "appearance-none rounded-md border bg-bg text-fg",
          "outline-none transition",
          "focus:border-accent focus:ring-2 focus:ring-accent/20",
          "disabled:opacity-50 disabled:cursor-not-allowed",
          size === "sm" ? "h-7 pl-2 pr-7 text-xs" : "h-9 pl-3 pr-8 text-sm",
          className
        )}
      >
        {allOptions.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.prefix ? `${opt.prefix} ${opt.label}` : opt.label}
          </option>
        ))}
      </select>
      <ChevronDown
        className={cn(
          "pointer-events-none absolute text-muted",
          size === "sm" ? "right-1.5 h-3.5 w-3.5" : "right-2 h-4 w-4"
        )}
        aria-hidden
      />
    </div>
  );
});

export default Select;
