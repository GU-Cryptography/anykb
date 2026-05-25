"use client";

import Link from "next/link";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  BookOpen,
  Brain,
  Globe2,
  KeyRound,
  Layers,
  MessageSquareText,
  Search,
  ShieldCheck,
  Sparkles,
  Workflow,
  Zap,
} from "lucide-react";

import Brand, { APP_NAME } from "@/components/Brand";
import ThemeToggle from "@/components/ThemeToggle";
import { getToken } from "@/lib/auth";

/**
 * Public landing page — entry point for unauthenticated visitors.
 * Authenticated users are redirected to /app (the chat workspace).
 */
export default function WelcomePage() {
  const router = useRouter();
  useEffect(() => {
    if (getToken()) router.replace("/");
  }, [router]);

  return (
    <div className="min-h-screen bg-bg text-fg">
      {/* Top nav */}
      <header className="sticky top-0 z-30 border-b bg-bg/80 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-7xl items-center px-4 sm:px-6 lg:px-8">
          <Brand size="sm" showWordmark />
          <nav className="ml-8 hidden items-center gap-6 text-sm text-muted md:flex">
            <a href="#features" className="transition hover:text-fg">
              功能
            </a>
            <a href="#how" className="transition hover:text-fg">
              如何工作
            </a>
            <a href="#scenarios" className="transition hover:text-fg">
              使用场景
            </a>
            <a
              href="https://github.com"
              target="_blank"
              rel="noreferrer"
              className="transition hover:text-fg"
            >
              开源
            </a>
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
            <Link
              href="/login"
              className="btn btn-ghost btn-sm hidden sm:inline-flex"
            >
              登录
            </Link>
            <Link href="/register" className="btn btn-primary btn-sm">
              免费开始
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          </div>
        </div>
      </header>

      {/* Hero */}
      <section className="relative overflow-hidden">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 -z-10 bg-gradient-to-br from-accent/10 via-transparent to-info/10"
        />
        <div
          aria-hidden
          className="pointer-events-none absolute -top-32 left-1/2 -z-10 h-[600px] w-[600px] -translate-x-1/2 rounded-full bg-accent/15 blur-3xl"
        />
        <div className="mx-auto max-w-7xl px-4 pb-20 pt-16 sm:px-6 sm:pt-24 lg:px-8 lg:pt-32">
          <div className="mx-auto max-w-3xl text-center">
            <div className="mb-6 inline-flex items-center gap-2 rounded-full border bg-surface/60 px-3 py-1 text-xs text-muted backdrop-blur">
              <Sparkles className="h-3.5 w-3.5 text-accent" />
              <span>v3 · 混合检索 + 二阶段重排 + 按 KB 配置</span>
            </div>
            <h1 className="text-4xl font-bold tracking-tight sm:text-5xl lg:text-6xl">
              <span className="bg-gradient-to-r from-accent via-info to-accent bg-clip-text text-transparent">
                你的私有知识库
              </span>
              <br />
              一句话问，秒级出答案
            </h1>
            <p className="mx-auto mt-6 max-w-2xl text-base text-muted sm:text-lg">
              上传文档 / 抓取网页 → 选中知识库 → 用一句话问出来。
              30 秒内吐一份带原文引用的 Markdown 报告，知识源头清晰可追溯。
            </p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <Link
                href="/register"
                className="btn btn-primary inline-flex items-center px-6 py-2.5 text-sm"
              >
                免费开始
                <ArrowRight className="h-4 w-4" />
              </Link>
              <Link
                href="/login"
                className="btn btn-ghost inline-flex items-center px-6 py-2.5 text-sm"
              >
                已有账号？登录
              </Link>
            </div>
            <div className="mt-4 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-xs text-muted">
              <span className="inline-flex items-center gap-1">
                <ShieldCheck className="h-3.5 w-3.5" /> BYOK · API Key 加密存储
              </span>
              <span className="inline-flex items-center gap-1">
                <KeyRound className="h-3.5 w-3.5" /> 本地账号 · 数据自托管
              </span>
              <span className="inline-flex items-center gap-1">
                <Workflow className="h-3.5 w-3.5" /> MIT 开源
              </span>
            </div>
          </div>

          {/* Demo card */}
          <div className="mx-auto mt-16 max-w-4xl">
            <div className="relative overflow-hidden rounded-2xl border bg-surface shadow-lift">
              <div className="flex h-9 items-center gap-1.5 border-b bg-surface-2 px-4">
                <span className="h-2.5 w-2.5 rounded-full bg-danger/60" />
                <span className="h-2.5 w-2.5 rounded-full bg-warning/60" />
                <span className="h-2.5 w-2.5 rounded-full bg-success/60" />
                <span className="ml-3 text-[11px] text-muted">
                  {APP_NAME.toLowerCase()}.local · 我的论文库
                </span>
              </div>
              <div className="grid gap-4 p-6 sm:grid-cols-[1fr_2fr] sm:p-8">
                <div className="space-y-2">
                  <p className="text-xs font-semibold uppercase tracking-wider text-muted">
                    我的知识库
                  </p>
                  <DemoKbItem name="📚 论文笔记" count={32} active />
                  <DemoKbItem name="🍜 上海餐厅" count={18} />
                  <DemoKbItem name="💼 公司 Wiki" count={147} />
                  <DemoKbItem name="🌐 收藏文章" count={56} />
                </div>
                <div className="space-y-3">
                  <div className="rounded-lg border bg-bg px-4 py-3 text-sm">
                    <span className="text-muted">你：</span>
                    Transformer 里 attention 的 Q/K/V 是什么含义？
                  </div>
                  <div className="rounded-lg border bg-accent/5 px-4 py-3 text-sm">
                    <div className="mb-2 flex items-center gap-2 text-xs text-accent">
                      <Sparkles className="h-3.5 w-3.5" />
                      AnyKB · 命中 3 篇论文
                    </div>
                    Q (Query) 代表当前位置想"查询什么"，K (Key) 是其他位置提供的"标签"，
                    V (Value) 是实际内容。Attention(Q,K,V) = softmax(QK^T/√d) · V…
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      <span className="chip border-border bg-surface text-[11px]">
                        📚 Attention Is All You Need · p.3
                      </span>
                      <span className="chip border-border bg-surface text-[11px]">
                        📚 Transformer 综述 · §2.1
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Features */}
      <section id="features" className="border-t bg-surface/30">
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 sm:py-24 lg:px-8">
          <div className="mx-auto max-w-2xl text-center">
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
              不是黑盒 ChatGPT，是你能掌控的 RAG 平台
            </h2>
            <p className="mt-4 text-muted">
              从向量库到模型，每一层都可以替换；从检索到答案，每一步都看得见。
            </p>
          </div>
          <div className="mt-16 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
            <FeatureCard
              icon={<Brain className="h-5 w-5" />}
              title="智能检索 · 混合排序"
              desc="稠密向量 + BM25 关键词混合召回；Cross-encoder reranker 二阶段重排，关键词查询不再被语义近邻盖过。"
            />
            <FeatureCard
              icon={<BookOpen className="h-5 w-5" />}
              title="多源知识库"
              desc="支持 PDF / Markdown / Word / 纯文本 / URL 抓取，自动 chunk + embed + 向量化，文档秒级可问。"
            />
            <FeatureCard
              icon={<MessageSquareText className="h-5 w-5" />}
              title="透明 Agent"
              desc="实时展示思考链：每一步调用了什么工具、命中几条、用了多少 token。不是黑盒。"
            />
            <FeatureCard
              icon={<Layers className="h-5 w-5" />}
              title="按 KB 配置"
              desc="每个知识库可以独立指定 embedding / reranker 提供商。论文库用 OpenAI，公司 Wiki 用本地 Ollama，互不干扰。"
            />
            <FeatureCard
              icon={<Globe2 className="h-5 w-5" />}
              title="Web Search 兜底"
              desc="KB 没命中时自动调用 DuckDuckGo 网络搜索补充，答案按【📚 KB】【🌐 Web】分段标注来源。"
            />
            <FeatureCard
              icon={<ShieldCheck className="h-5 w-5" />}
              title="数据自托管"
              desc="本地账号 + JWT 守卫，API Key Fernet 加密存储。可部署到自己的服务器，知识不出域。"
            />
          </div>
        </div>
      </section>

      {/* How it works */}
      <section id="how" className="border-t">
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 sm:py-24 lg:px-8">
          <div className="mx-auto max-w-2xl text-center">
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
              30 秒上手
            </h2>
            <p className="mt-4 text-muted">注册即用，无需任何运维知识</p>
          </div>
          <div className="mt-16 grid gap-8 md:grid-cols-3">
            <StepCard
              n={1}
              icon={<KeyRound className="h-5 w-5" />}
              title="配置你的 LLM"
              desc="在「设置」填一次 base_url + api_key，支持 DeepSeek / OpenAI / Claude / vLLM / Ollama。"
            />
            <StepCard
              n={2}
              icon={<BookOpen className="h-5 w-5" />}
              title="创建知识库"
              desc="上传文档或粘贴网址，后台自动 ingest，状态可见。每个 KB 可单独配 embedding。"
            />
            <StepCard
              n={3}
              icon={<Zap className="h-5 w-5" />}
              title="一句话提问"
              desc="选中 KB，输入问题。30 秒内出带原文引用的 Markdown 报告，可导出 PDF。"
            />
          </div>
        </div>
      </section>

      {/* Scenarios */}
      <section id="scenarios" className="border-t bg-surface/30">
        <div className="mx-auto max-w-7xl px-4 py-20 sm:px-6 sm:py-24 lg:px-8">
          <div className="mx-auto max-w-2xl text-center">
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
              谁在用 {APP_NAME}
            </h2>
          </div>
          <div className="mt-16 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
            <ScenarioCard
              emoji="🎓"
              title="研究人员"
              desc="把读过的论文喂进去，写综述时秒查关键论点 + 出处页码。"
            />
            <ScenarioCard
              emoji="💼"
              title="产品经理"
              desc="积累用户调研 / 竞品分析，决策时一句话问历史结论。"
            />
            <ScenarioCard
              emoji="👨‍💻"
              title="工程团队"
              desc="技术 Wiki + 设计文档统一检索，新人入职查找速度 10x。"
            />
            <ScenarioCard
              emoji="✍️"
              title="内容创作者"
              desc="积累灵感素材，写作时从私人素材库快速调取参考。"
            />
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="border-t">
        <div className="mx-auto max-w-4xl px-4 py-20 text-center sm:px-6 sm:py-24 lg:px-8">
          <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
            把零散的知识，变成你的第二大脑
          </h2>
          <p className="mt-4 text-muted">
            注册即用 · 无需信用卡 · 数据完全自托管
          </p>
          <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
            <Link
              href="/register"
              className="btn btn-primary inline-flex items-center px-8 py-3 text-base"
            >
              立即免费开始
              <ArrowRight className="h-4 w-4" />
            </Link>
            <Link
              href="/login"
              className="btn btn-ghost inline-flex items-center px-6 py-3 text-base"
            >
              已有账号？登录
            </Link>
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t bg-surface/40">
        <div className="mx-auto max-w-7xl px-4 py-10 sm:px-6 lg:px-8">
          <div className="flex flex-col items-center gap-4 sm:flex-row sm:justify-between">
            <div className="flex items-center gap-3">
              <Brand size="sm" showWordmark={false} />
              <span className="text-sm text-muted">
                © {new Date().getFullYear()} {APP_NAME} · MIT License
              </span>
            </div>
            <div className="flex items-center gap-5 text-sm text-muted">
              <a
                href="https://github.com"
                target="_blank"
                rel="noreferrer"
                className="transition hover:text-fg"
              >
                GitHub
              </a>
              <Link href="/login" className="transition hover:text-fg">
                登录
              </Link>
              <Link href="/register" className="transition hover:text-fg">
                注册
              </Link>
            </div>
          </div>
        </div>
      </footer>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------
function FeatureCard({
  icon,
  title,
  desc,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
}) {
  return (
    <div className="group rounded-2xl border bg-bg p-6 transition hover:border-accent/50 hover:shadow-lift">
      <div className="mb-4 inline-flex h-10 w-10 items-center justify-center rounded-lg bg-accent/10 text-accent transition group-hover:bg-accent/20">
        {icon}
      </div>
      <h3 className="font-semibold">{title}</h3>
      <p className="mt-2 text-sm leading-relaxed text-muted">{desc}</p>
    </div>
  );
}

function StepCard({
  n,
  icon,
  title,
  desc,
}: {
  n: number;
  icon: React.ReactNode;
  title: string;
  desc: string;
}) {
  return (
    <div className="relative rounded-2xl border bg-surface p-6">
      <div className="absolute -top-3 -left-3 flex h-10 w-10 items-center justify-center rounded-full bg-accent text-sm font-bold text-white shadow-lift">
        {n}
      </div>
      <div className="mb-3 inline-flex h-9 w-9 items-center justify-center rounded-lg bg-accent/10 text-accent">
        {icon}
      </div>
      <h3 className="font-semibold">{title}</h3>
      <p className="mt-2 text-sm leading-relaxed text-muted">{desc}</p>
    </div>
  );
}

function ScenarioCard({
  emoji,
  title,
  desc,
}: {
  emoji: string;
  title: string;
  desc: string;
}) {
  return (
    <div className="rounded-2xl border bg-bg p-6 transition hover:border-accent/40">
      <div className="text-3xl">{emoji}</div>
      <h3 className="mt-3 font-semibold">{title}</h3>
      <p className="mt-2 text-sm leading-relaxed text-muted">{desc}</p>
    </div>
  );
}

function DemoKbItem({
  name,
  count,
  active,
}: {
  name: string;
  count: number;
  active?: boolean;
}) {
  return (
    <div
      className={`flex items-center justify-between rounded-lg border px-3 py-2 text-xs ${
        active ? "border-accent/50 bg-accent/5" : "border-border bg-bg"
      }`}
    >
      <span>{name}</span>
      <span className="text-muted">{count}</span>
    </div>
  );
}
