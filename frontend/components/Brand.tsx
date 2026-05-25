import { Sparkles } from "lucide-react";
import { cn } from "@/lib/cn";

export const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME || "AnyKB";

type BrandSize = "sm" | "md" | "lg";

type BrandProps = {
  size?: BrandSize;
  showWordmark?: boolean;
  className?: string;
};

const SIZES: Record<BrandSize, { box: string; icon: string; text: string }> = {
  sm: {
    box: "h-7 w-7 rounded-lg",
    icon: "h-4 w-4",
    text: "text-sm font-semibold",
  },
  md: {
    box: "h-8 w-8 rounded-xl",
    icon: "h-4 w-4",
    text: "text-base font-semibold",
  },
  lg: {
    box: "h-14 w-14 rounded-2xl",
    icon: "h-7 w-7",
    text: "text-3xl font-semibold tracking-tight sm:text-4xl",
  },
};

/**
 * AnyKB brand mark — gradient square with Sparkles icon + optional wordmark.
 * Use `size="sm"` in compact bars, `"md"` in sidebar headers, `"lg"` in heroes / auth pages.
 */
export default function Brand({
  size = "md",
  showWordmark = true,
  className,
}: BrandProps) {
  const s = SIZES[size];
  return (
    <div className={cn("inline-flex items-center gap-2", className)}>
      <div
        className={cn(
          "flex items-center justify-center text-white",
          "bg-gradient-to-br from-accent to-accent/70 shadow-soft",
          s.box
        )}
        aria-hidden
      >
        <Sparkles className={s.icon} />
      </div>
      {showWordmark && <span className={s.text}>{APP_NAME}</span>}
    </div>
  );
}
