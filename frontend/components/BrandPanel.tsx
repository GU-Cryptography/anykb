"use client";

import { Sparkles, ShieldCheck, Globe2, Layers } from "lucide-react";

import Brand, { APP_NAME } from "@/components/Brand";

/**
 * Brand panel — left-side hero used by /login and /register on lg+ screens.
 * Hidden on mobile (the form fills the screen there).
 */
export default function BrandPanel() {
  return (
    <div className="relative hidden overflow-hidden bg-gradient-to-br from-accent via-info to-accent text-white lg:flex lg:flex-col">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-32 -left-32 h-96 w-96 rounded-full bg-white/10 blur-3xl"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute -bottom-32 -right-32 h-96 w-96 rounded-full bg-white/10 blur-3xl"
      />

      <div className="relative z-10 flex h-full flex-col p-10 xl:p-14">
        <div className="flex items-center gap-2">
          <Brand size="sm" showWordmark />
        </div>

        <div className="flex flex-1 flex-col justify-center">
          <h2 className="text-3xl font-bold leading-tight xl:text-4xl">
            把零散的知识，
            <br />
            变成你的第二大脑
          </h2>
          <p className="mt-4 max-w-md text-white/80">
            {APP_NAME} 是开源的私有 RAG 知识库 — 上传文档，
            一句话提问，秒级出带原文引用的答案。
          </p>

          <ul className="mt-10 space-y-4">
            <BrandFeature
              icon={<Sparkles className="h-4 w-4" />}
              title="混合检索 + 二阶段重排"
              desc="稠密向量 + BM25 + Cross-encoder reranker"
            />
            <BrandFeature
              icon={<Layers className="h-4 w-4" />}
              title="按 KB 独立配置"
              desc="每个知识库可指定不同 embedding / reranker"
            />
            <BrandFeature
              icon={<Globe2 className="h-4 w-4" />}
              title="Web 兜底"
              desc="KB 没命中时调用网络搜索补充答案"
            />
            <BrandFeature
              icon={<ShieldCheck className="h-4 w-4" />}
              title="数据自托管"
              desc="本地账号 + API Key 加密存储 · MIT 开源"
            />
          </ul>
        </div>

        <p className="text-xs text-white/60">
          v3 · Milvus + 混合检索 + per-KB 配置
        </p>
      </div>
    </div>
  );
}

function BrandFeature({
  icon,
  title,
  desc,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
}) {
  return (
    <li className="flex items-start gap-3">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white/15 backdrop-blur">
        {icon}
      </div>
      <div>
        <p className="font-medium">{title}</p>
        <p className="text-sm text-white/70">{desc}</p>
      </div>
    </li>
  );
}
