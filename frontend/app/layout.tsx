import type { Metadata } from "next";
import { Toaster } from "sonner";
import { ThemeProvider } from "@/components/ThemeProvider";
import "./globals.css";

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME || "AnyKB";

export const metadata: Metadata = {
  title: APP_NAME,
  description:
    "把任意知识库变成可对话的：上传文档、抓取网页，然后一句话问出来。",
};

// Inline script that runs synchronously before paint so the dark class is
// applied before the first frame — avoids the flash of light theme for
// users who prefer dark.
const NO_FLASH = `(function(){try{var t=localStorage.getItem('anykb:theme')||'system';var d=t==='dark'||(t==='system'&&window.matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.classList.toggle('dark',d);}catch(e){}})()`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        {/* eslint-disable-next-line react/no-danger */}
        <script dangerouslySetInnerHTML={{ __html: NO_FLASH }} />
      </head>
      <body className="min-h-screen bg-bg text-fg antialiased">
        <ThemeProvider>
          {children}
          <Toaster
            position="top-center"
            richColors
            closeButton
            toastOptions={{
              classNames: {
                toast: "rounded-xl border shadow-lift",
              },
            }}
          />
        </ThemeProvider>
        {process.env.NEXT_PUBLIC_PLAUSIBLE_DOMAIN && (
          /* eslint-disable-next-line @next/next/no-sync-scripts */
          <script
            defer
            data-domain={process.env.NEXT_PUBLIC_PLAUSIBLE_DOMAIN}
            src="https://plausible.io/js/script.js"
          />
        )}
      </body>
    </html>
  );
}
