"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  BookOpen,
  Feather,
  FileText,
  GitCompare,
  Globe,
  Lightbulb,
  MapPin,
  MessageSquare,
  Quote,
  Sparkles,
  Utensils,
} from "lucide-react";

import Brand, { APP_NAME } from "@/components/Brand";
import ChatBox from "@/components/ChatBox";
import MessageBubble from "@/components/MessageBubble";
import Select from "@/components/Select";
import Sidebar, { SidebarToggle } from "@/components/Sidebar";
import ThemeToggle from "@/components/ThemeToggle";
import type { ToolEvent } from "@/components/ThinkingChain";
import { connectChat, type ChatEvent, type ChatMessage } from "@/lib/sseClient";
import { getToken, getUser, logout, type User } from "@/lib/auth";
import { listKbs, type KB } from "@/lib/kb-api";
import { toast } from "sonner";
import {
  appendAssistantMessage,
  appendUserMessage,
  createConversation,
  deleteConversation,
  finalizeConversation,
  getConversation,
  listConversations,
  migrateFromLocalStorage,
  patchConversation,
  type ConversationSummary,
  type MessagePayload,
} from "@/lib/conversations-api";
import {
  deriveTitle,
  genMessageId,
  type Conversation,
  type Message,
} from "@/lib/conversationStore";

type HeroMode = "unbound" | "travel" | "user-kb";

const SUGGESTIONS_BY_MODE: Record<
  HeroMode,
  { text: string; Icon: typeof MessageSquare }[]
> = {
  unbound: [
    { text: "介绍 2026 年 AI 领域的几个重要进展", Icon: Globe },
    { text: "解释一下零知识证明的核心思想", Icon: Lightbulb },
    { text: "搜一下 Python 3.13 引入了哪些新特性", Icon: Globe },
    { text: "帮我写一首关于秋天的现代诗", Icon: Feather },
  ],
  travel: [
    { text: "5月13号上海，想找一家做酸菜鱼的本地小店", Icon: MapPin },
    { text: "周末去成都两天，本地老饕去哪儿吃", Icon: Utensils },
    { text: "北京哪家烤鸭值得排队", Icon: Utensils },
    { text: "杭州梅雨天有什么暖胃馆子", Icon: Utensils },
  ],
  "user-kb": [
    { text: "这个知识库主要讲了什么？", Icon: MessageSquare },
    { text: "总结一下最近一份文档的要点", Icon: FileText },
    { text: "对比一下文档里提到的几种方案", Icon: GitCompare },
    { text: "找一段能直接引用的原文出处", Icon: Quote },
  ],
};

const DEFAULT_TITLE = "新对话";

// ---------------------------------------------------------------------------
// Helpers — keep the local discriminated-union Message shape that downstream
// components (MessageBubble, ThinkingChain) already understand.
// ---------------------------------------------------------------------------
function serverMsgToLocal(m: MessagePayload): Message {
  const ts = m.created_at ? new Date(m.created_at).getTime() : Date.now();
  if (m.role === "user") {
    return { id: m.id, role: "user", content: m.content, created_at: ts };
  }
  return {
    id: m.id,
    role: "assistant",
    content: m.content,
    tools: m.tools ?? [],
    cost_usd: m.cost_usd ?? undefined,
    error: m.error ?? undefined,
    created_at: ts,
  };
}

function summaryToConv(s: ConversationSummary, messages: Message[] = []): Conversation {
  const createdMs = s.created_at ? new Date(s.created_at).getTime() : Date.now();
  const updatedMs = s.updated_at ? new Date(s.updated_at).getTime() : createdMs;
  return {
    id: s.id,
    title: s.title,
    messages,
    kb_id: s.kb_id,
    llm_model: s.llm_model,
    finalized_at: s.finalized_at,
    created_at: createdMs,
    updated_at: updatedMs,
  };
}

export default function Page() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  const [summaries, setSummaries] = useState<ConversationSummary[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [currentMessages, setCurrentMessages] = useState<Message[]>([]);
  const [currentKbId, setCurrentKbId] = useState<string | null>(null);
  /** v3-M6: per-conversation LLM model override (null = user default). */
  const [currentModel, setCurrentModel] = useState<string | null>(null);
  /** v3-M6: cached LLM model list (probed once on settings load). */
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  // Messages cache so flipping back to a previously-loaded conv is instant.
  const messagesCache = useRef<Map<string, Message[]>>(new Map());
  // Live mirror of the currently-streaming assistant message — used by the
  // SSE callback to capture final content for server persistence without
  // racing against React state.
  const streamingRef = useRef<{
    convId: string;
    msgId: string;
    content: string;
    tools: ToolEvent[];
  } | null>(null);

  const [kbs, setKbs] = useState<KB[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const cleanupRef = useRef<(() => void) | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // -------------------------------------------------------------------------
  // Mount: auth → migrate → list → maybe select first conv
  // -------------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!getToken()) {
        router.replace("/welcome");
        return;
      }
      const u = getUser();
      if (cancelled) return;
      setUser(u);
      setAuthChecked(true);

      if (u) {
        try {
          const imported = await migrateFromLocalStorage(u.id);
          if (!cancelled && imported > 0) {
            toast.success(`已从本地恢复 ${imported} 条历史对话`);
          }
        } catch (e) {
          console.warn("conv migration failed (non-fatal)", e);
        }

        // v3-M6: probe the LLM model list once on mount so the model selector
        // has options. Fail-soft: empty list = selector stays disabled.
        try {
          const { getMySettings, probeLLM } = await import("@/lib/settings-api");
          const settings = await getMySettings();
          if (
            !cancelled &&
            settings.llm.configured &&
            settings.llm.provider &&
            settings.llm.base_url
          ) {
            const { models } = await probeLLM({
              provider: settings.llm.provider,
              base_url: settings.llm.base_url,
              api_key: "",
            });
            if (!cancelled) setModelOptions(models);
          }
        } catch (e) {
          console.warn("LLM model probe failed (non-fatal)", e);
        }
      }

      try {
        const list = await listConversations();
        if (cancelled) return;
        setSummaries(list);
        if (list.length > 0) {
          // Auto-select the most-recently-updated one (server sorts desc).
          await loadConversation(list[0].id);
        }
      } catch (e) {
        if (!cancelled) {
          console.error("list conversations failed", e);
          toast.error((e as Error)?.message ?? "加载会话历史失败");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // KB list (non-fatal on failure — selector just shows empty).
  useEffect(() => {
    if (!authChecked) return;
    listKbs()
      .then(setKbs)
      .catch(() => {});
  }, [authChecked]);

  // Auto scroll to bottom on new content.
  useEffect(() => {
    if (!scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [
    currentMessages.length,
    currentMessages[currentMessages.length - 1]?.role === "assistant"
      ? (currentMessages[currentMessages.length - 1] as Message & { content: string })?.content
          ?.length
      : 0,
  ]);

  // -------------------------------------------------------------------------
  // Conversation load (with cache)
  // -------------------------------------------------------------------------
  const loadConversation = useCallback(async (id: string) => {
    setCurrentId(id);
    const cached = messagesCache.current.get(id);
    if (cached) {
      setCurrentMessages(cached);
      // Pull kb_id / llm_model from the latest summary if possible.
      setSummaries((cur) => {
        const found = cur.find((c) => c.id === id);
        if (found) {
          setCurrentKbId(found.kb_id);
          setCurrentModel(found.llm_model ?? null);
        }
        return cur;
      });
      return;
    }
    try {
      const detail = await getConversation(id);
      const msgs = detail.messages.map(serverMsgToLocal);
      messagesCache.current.set(id, msgs);
      setCurrentMessages(msgs);
      setCurrentKbId(detail.kb_id);
      setCurrentModel(detail.llm_model ?? null);
    } catch (e) {
      toast.error((e as Error)?.message ?? "加载会话失败");
    }
  }, []);

  const setMessagesForCurrent = useCallback(
    (next: Message[] | ((prev: Message[]) => Message[])) => {
      setCurrentMessages((prev) => {
        const resolved = typeof next === "function" ? (next as (p: Message[]) => Message[])(prev) : next;
        if (currentId) messagesCache.current.set(currentId, resolved);
        return resolved;
      });
    },
    [currentId]
  );

  const updateLastAssistant = useCallback(
    (mutator: (m: Message) => Message) => {
      setMessagesForCurrent((prev) => {
        const next = [...prev];
        for (let i = next.length - 1; i >= 0; i--) {
          if (next[i].role === "assistant") {
            next[i] = mutator(next[i]);
            break;
          }
        }
        return next;
      });
    },
    [setMessagesForCurrent]
  );

  const bumpSummary = useCallback(
    (
      convId: string,
      patch: Partial<ConversationSummary>,
      messageCountDelta: number = 0,
      moveToTop = false
    ) => {
      setSummaries((prev) => {
        const idx = prev.findIndex((c) => c.id === convId);
        if (idx === -1) return prev;
        const updated: ConversationSummary = {
          ...prev[idx],
          ...patch,
          message_count: Math.max(0, prev[idx].message_count + messageCountDelta),
          updated_at: new Date().toISOString(),
        };
        if (!moveToTop) {
          const next = [...prev];
          next[idx] = updated;
          return next;
        }
        return [updated, ...prev.slice(0, idx), ...prev.slice(idx + 1)];
      });
    },
    []
  );

  // -------------------------------------------------------------------------
  // Send
  // -------------------------------------------------------------------------
  const handleSend = useCallback(
    async (q: string) => {
      let convId = currentId;
      let isFreshConv = false;
      // Lazily create a conversation if none is selected yet.
      if (!convId) {
        try {
          const created = await createConversation({ kb_id: currentKbId });
          convId = created.id;
          isFreshConv = true;
          const summary: ConversationSummary = {
            id: created.id,
            title: created.title,
            kb_id: created.kb_id,
            llm_model: created.llm_model,
            message_count: 0,
            created_at: created.created_at,
            updated_at: created.updated_at,
            finalized_at: created.finalized_at,
          };
          setSummaries((prev) => [summary, ...prev]);
          setCurrentId(created.id);
          messagesCache.current.set(created.id, []);
          setCurrentMessages([]);
        } catch (e) {
          toast.error((e as Error)?.message ?? "创建会话失败");
          return;
        }
      }

      // Persist user message first so it survives even if SSE fails.
      let userMsg: Message;
      try {
        const persisted = await appendUserMessage(convId!, q);
        userMsg = serverMsgToLocal(persisted) as Message;
      } catch (e) {
        toast.error((e as Error)?.message ?? "保存消息失败");
        return;
      }

      // Build chat context from the *currently loaded* messages (before we
      // optimistically append the new turn).
      const priorHistory: ChatMessage[] = currentMessages
        .filter((m) => {
          if (m.role === "user") return true;
          return !!m.content && !m.error && !m.streaming;
        })
        .map((m) => ({ role: m.role, content: m.content }));
      const messagesForBackend: ChatMessage[] = [
        ...priorHistory,
        { role: "user", content: q },
      ];

      // Placeholder assistant — local-only until SSE done/error fires.
      const aiId = genMessageId();
      const aiMsg: Message = {
        id: aiId,
        role: "assistant",
        content: "",
        tools: [],
        streaming: true,
        created_at: Date.now(),
      };
      setMessagesForCurrent((prev) => [...prev, userMsg, aiMsg]);
      streamingRef.current = {
        convId: convId!,
        msgId: aiId,
        content: "",
        tools: [],
      };

      // Optimistic sidebar title/count bump. Server will derive the same
      // title for the first user message, so they line up.
      bumpSummary(
        convId!,
        {
          title:
            isFreshConv || (summaries.find((c) => c.id === convId)?.message_count ?? 0) === 0
              ? deriveTitle(q)
              : summaries.find((c) => c.id === convId)?.title ?? DEFAULT_TITLE,
        },
        1,
        true
      );

      setBusy(true);

      const persistFinal = async (opts: {
        error?: string;
        costUsd?: number;
      }) => {
        const snap = streamingRef.current;
        streamingRef.current = null;
        if (!snap || snap.convId !== convId) return;
        try {
          // For error/aborted turns we still persist whatever we have so
          // the user can see the partial state after refresh.
          const result = await appendAssistantMessage(snap.convId, {
            content: snap.content,
            tools: snap.tools,
            cost_usd: opts.costUsd,
            error: opts.error,
          });
          // Swap the placeholder id with the server id so future ops align.
          setMessagesForCurrent((prev) =>
            prev.map((m) => (m.id === snap.msgId ? { ...m, id: result.id } : m))
          );
          bumpSummary(snap.convId, {}, 1, true);
        } catch (e) {
          console.error("persist assistant failed", e);
          toast.error("助手回复保存失败，刷新后可能丢失");
        }
      };

      const cleanup = connectChat(
        messagesForBackend,
        (evt: ChatEvent) => {
          switch (evt.event) {
            case "tool_start": {
              const newTool: ToolEvent = {
                name: evt.name!,
                input: evt.input,
                status: "running",
                t0: Date.now(),
              };
              if (streamingRef.current) {
                streamingRef.current.tools = [...streamingRef.current.tools, newTool];
              }
              updateLastAssistant((m) =>
                m.role === "assistant" ? { ...m, tools: [...m.tools, newTool] } : m
              );
              break;
            }
            case "tool_end": {
              if (streamingRef.current) {
                const tools = [...streamingRef.current.tools];
                for (let i = tools.length - 1; i >= 0; i--) {
                  if (tools[i].name === evt.name && tools[i].status === "running") {
                    tools[i] = {
                      ...tools[i],
                      status: evt.ok ? "ok" : "error",
                      latency_ms: evt.latency_ms ?? null,
                      error: evt.error ?? null,
                    };
                    break;
                  }
                }
                streamingRef.current.tools = tools;
              }
              updateLastAssistant((m) => {
                if (m.role !== "assistant") return m;
                const tools = [...m.tools];
                for (let i = tools.length - 1; i >= 0; i--) {
                  if (tools[i].name === evt.name && tools[i].status === "running") {
                    tools[i] = {
                      ...tools[i],
                      status: evt.ok ? "ok" : "error",
                      latency_ms: evt.latency_ms ?? null,
                      error: evt.error ?? null,
                    };
                    break;
                  }
                }
                return { ...m, tools };
              });
              break;
            }
            case "tool_blocked": {
              const newTool: ToolEvent = {
                name: evt.name!,
                status: "blocked",
                reason: evt.reason ?? "",
              };
              if (streamingRef.current) {
                streamingRef.current.tools = [...streamingRef.current.tools, newTool];
              }
              updateLastAssistant((m) =>
                m.role === "assistant" ? { ...m, tools: [...m.tools, newTool] } : m
              );
              break;
            }
            case "token": {
              if (streamingRef.current) {
                streamingRef.current.content += evt.text ?? "";
              }
              updateLastAssistant((m) =>
                m.role === "assistant" ? { ...m, content: m.content + (evt.text ?? "") } : m
              );
              break;
            }
            case "error": {
              const errMsg = evt.message ?? "unknown error";
              updateLastAssistant((m) =>
                m.role === "assistant" ? { ...m, error: errMsg, streaming: false } : m
              );
              if (evt.code === "llm_not_configured" || evt.code === "embedding_not_configured") {
                toast.error(errMsg, {
                  action: {
                    label: "去配置",
                    onClick: () => router.push(evt.settings_url ?? "/settings"),
                  },
                });
              }
              void persistFinal({ error: errMsg });
              setBusy(false);
              cleanupRef.current = null;
              break;
            }
            case "done": {
              const costUsd = typeof evt.cost_usd === "number" ? evt.cost_usd : undefined;
              updateLastAssistant((m) =>
                m.role === "assistant" ? { ...m, streaming: false, cost_usd: costUsd } : m
              );
              void persistFinal({ costUsd });
              setBusy(false);
              cleanupRef.current = null;
              break;
            }
            default:
              break;
          }
        },
        // v3-M2: convId is always resolved by now (existing conversation or
        // lazily created above), so plan_node can pick up its L4 summary.
        { kbId: currentKbId, model: currentModel, conversationId: convId }
      );

      cleanupRef.current = cleanup;
    },
    [
      currentId,
      currentKbId,
      currentMessages,
      setMessagesForCurrent,
      updateLastAssistant,
      bumpSummary,
      summaries,
      router,
    ]
  );

  const handleStop = useCallback(() => {
    cleanupRef.current?.();
    cleanupRef.current = null;
    setBusy(false);
    updateLastAssistant((m) =>
      m.role === "assistant" && m.streaming
        ? { ...m, streaming: false, error: m.error ?? "用户已停止生成" }
        : m
    );
    // Persist whatever we had so the partial turn isn't lost on refresh.
    const snap = streamingRef.current;
    if (snap) {
      streamingRef.current = null;
      void appendAssistantMessage(snap.convId, {
        content: snap.content,
        tools: snap.tools,
        error: "用户已停止生成",
      })
        .then((result) => {
          setMessagesForCurrent((prev) =>
            prev.map((m) => (m.id === snap.msgId ? { ...m, id: result.id } : m))
          );
          bumpSummary(snap.convId, {}, 1, true);
        })
        .catch((e) => console.error("persist stopped turn failed", e));
    }
  }, [updateLastAssistant, setMessagesForCurrent, bumpSummary]);

  // -------------------------------------------------------------------------
  // New / Select / Delete / KB switch
  // -------------------------------------------------------------------------
  const handleNew = useCallback(async () => {
    try {
      const created = await createConversation({ kb_id: currentKbId });
      const summary: ConversationSummary = {
        id: created.id,
        title: created.title,
        kb_id: created.kb_id,
        llm_model: created.llm_model,
        message_count: 0,
        created_at: created.created_at,
        updated_at: created.updated_at,
        finalized_at: created.finalized_at,
      };
      setSummaries((prev) => [summary, ...prev]);
      setCurrentId(created.id);
      messagesCache.current.set(created.id, []);
      setCurrentMessages([]);
      setCurrentKbId(created.kb_id);
      setCurrentModel(created.llm_model ?? null);
      setSidebarOpen(false);
    } catch (e) {
      toast.error((e as Error)?.message ?? "新建会话失败");
    }
  }, [currentKbId]);

  const handleSelect = useCallback(
    async (id: string) => {
      setSidebarOpen(false);
      await loadConversation(id);
    },
    [loadConversation]
  );

  const handleKbChange = useCallback(
    async (kbId: string | null) => {
      setCurrentKbId(kbId);
      if (!currentId) return;
      try {
        await patchConversation(currentId, { kb_id: kbId });
        setSummaries((prev) =>
          prev.map((c) => (c.id === currentId ? { ...c, kb_id: kbId } : c))
        );
      } catch (e) {
        toast.error((e as Error)?.message ?? "保存 KB 绑定失败");
      }
    },
    [currentId]
  );

  // v3-M6: per-conversation LLM model picker (null = use user default).
  const handleModelChange = useCallback(
    async (model: string | null) => {
      const prevModel = currentModel;
      setCurrentModel(model);
      if (!currentId) return;
      try {
        await patchConversation(currentId, { llm_model: model });
        setSummaries((prev) =>
          prev.map((c) => (c.id === currentId ? { ...c, llm_model: model } : c))
        );
      } catch (e) {
        // Revert on error
        setCurrentModel(prevModel);
        toast.error((e as Error)?.message ?? "保存模型选择失败");
      }
    },
    [currentId, currentModel]
  );

  const handleDelete = useCallback(
    async (id: string) => {
      try {
        await deleteConversation(id);
      } catch (e) {
        toast.error((e as Error)?.message ?? "删除会话失败");
        return;
      }
      messagesCache.current.delete(id);
      setSummaries((prev) => {
        const next = prev.filter((c) => c.id !== id);
        if (currentId === id) {
          const newId = next[0]?.id ?? null;
          setCurrentId(newId);
          if (newId) {
            void loadConversation(newId);
          } else {
            setCurrentMessages([]);
            setCurrentKbId(null);
          }
        }
        return next;
      });
    },
    [currentId, loadConversation]
  );

  const handleRename = useCallback(
    async (id: string, newTitle: string) => {
      try {
        const updated = await patchConversation(id, { title: newTitle });
        setSummaries((prev) =>
          prev.map((c) => (c.id === id ? { ...c, title: updated.title } : c))
        );
      } catch (e) {
        toast.error((e as Error)?.message ?? "重命名失败");
      }
    },
    []
  );

  // v3-M4: end a conversation → extract long-term memories (PRD §8).
  const handleFinalize = useCallback(
    async (id: string) => {
      try {
        const res = await finalizeConversation(id);
        // Reflect finalize state so the sidebar disables the action.
        setSummaries((prev) =>
          prev.map((c) =>
            c.id === id
              ? { ...c, finalized_at: c.finalized_at ?? new Date().toISOString() }
              : c
          )
        );
        if (res.already_finalized) {
          toast.info("该会话已结束");
        } else if (res.memory_extracted > 0) {
          toast.success(`已结束会话，提取了 ${res.memory_extracted} 条记忆`);
        } else {
          toast.success("已结束会话");
        }
      } catch (e) {
        toast.error((e as Error)?.message ?? "结束会话失败");
      }
    },
    []
  );

  const handleLogout = useCallback(() => {
    cleanupRef.current?.();
    logout();
    router.replace("/login");
  }, [router]);

  // -------------------------------------------------------------------------
  // Render-time bridge: Sidebar still wants Conversation[], we have summaries.
  // -------------------------------------------------------------------------
  const sidebarConversations: Conversation[] = summaries.map((s) =>
    summaryToConv(s, s.id === currentId ? currentMessages : [])
  );
  const current = sidebarConversations.find((c) => c.id === currentId) ?? null;

  const messages = currentMessages;
  const isEmpty = messages.length === 0;

  if (!authChecked) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 bg-bg text-muted">
        <Sparkles className="h-8 w-8 animate-pulse text-accent" />
        <div className="text-sm">正在加载 {APP_NAME}…</div>
      </div>
    );
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-bg text-fg">
      <Sidebar
        conversations={sidebarConversations}
        currentId={currentId}
        onSelect={handleSelect}
        onNew={handleNew}
        onDelete={handleDelete}
        onRename={handleRename}
        onFinalize={handleFinalize}
        open={sidebarOpen}
        onToggle={() => setSidebarOpen((v) => !v)}
        user={user}
        onLogout={handleLogout}
        onUserChanged={setUser}
      />

      <main className="flex flex-1 flex-col min-w-0">
        {/* Top bar */}
        <header className="flex h-14 shrink-0 items-center gap-2 border-b bg-bg/80 px-3 backdrop-blur md:gap-3 md:px-6">
          <SidebarToggle onClick={() => setSidebarOpen(true)} />

          {/* Brand: icon-only on mobile, full on md+ */}
          <Brand size="sm" showWordmark={false} className="md:hidden" />
          <Brand size="sm" className="hidden md:inline-flex" />
          <div className="hidden h-5 w-px bg-border md:block" />

          {/* Current conversation title */}
          <div className="flex-1 min-w-0 truncate text-sm text-muted">
            {current?.title ?? "开始新对话"}
          </div>

          {/* Right group */}
          <div className="flex items-center gap-1.5 md:gap-2">
            <div className="hidden items-center gap-1.5 text-muted sm:flex">
              <BookOpen className="h-3.5 w-3.5" />
              <Select
                size="sm"
                value={currentKbId ?? ""}
                onChange={(e) => handleKbChange(e.target.value || null)}
                disabled={busy}
                placeholderOption={{ value: "", label: "通用聊天（无知识库）" }}
                options={kbs.map((kb) => ({
                  value: kb.id,
                  label: kb.name,
                  prefix: kb.is_system ? "🔒" : "📚",
                }))}
                className="max-w-[180px]"
                title={
                  currentKbId
                    ? "当前对话绑定到此知识库"
                    : "未绑定 KB，纯聊天模式（仅用模型预训练知识）"
                }
                aria-label="选择知识库"
              />
            </div>

            {/* v3-M6: model selector (only show when settings already configured) */}
            {modelOptions.length > 0 && (
              <div className="hidden items-center gap-1.5 text-muted sm:flex">
                <Sparkles className="h-3.5 w-3.5" />
                <Select
                  size="sm"
                  value={currentModel ?? ""}
                  onChange={(e) => handleModelChange(e.target.value || null)}
                  disabled={busy}
                  placeholderOption={{ value: "", label: "默认模型" }}
                  options={modelOptions.map((m) => ({ value: m, label: m }))}
                  className="max-w-[200px]"
                  title={
                    currentModel
                      ? `当前会话使用模型：${currentModel}`
                      : "使用设置中配置的默认模型"
                  }
                  aria-label="选择模型"
                />
              </div>
            )}

            <ThemeToggle />
          </div>
        </header>

        {/* Mobile KB selector strip (shown below header on sm and smaller) */}
        <div className="flex shrink-0 items-center gap-1.5 border-b bg-bg/80 px-3 py-1.5 text-xs text-muted backdrop-blur sm:hidden">
          <BookOpen className="h-3.5 w-3.5" />
          <Select
            size="sm"
            value={currentKbId ?? ""}
            onChange={(e) => handleKbChange(e.target.value || null)}
            disabled={busy}
            placeholderOption={{ value: "", label: "通用聊天（无知识库）" }}
            options={kbs.map((kb) => ({
              value: kb.id,
              label: kb.name,
              prefix: kb.is_system ? "🔒" : "📚",
            }))}
            className="flex-1"
            aria-label="选择知识库"
          />
        </div>

        {/* Messages area */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-none px-6 py-6 sm:px-10 md:px-16 lg:px-24">
            {isEmpty ? (
              <Hero
                mode={
                  !currentKbId
                    ? "unbound"
                    : kbs.find((k) => k.id === currentKbId)?.is_system
                    ? "travel"
                    : "user-kb"
                }
                kbName={kbs.find((k) => k.id === currentKbId)?.name ?? null}
                onPick={(p) => handleSend(p)}
              />
            ) : (
              <div className="space-y-6 pb-6">
                {messages.map((m, i) => {
                  // For assistant messages, pass the nearest preceding user
                  // message so the share-card can show "Q: ..." context.
                  let prevUser: string | undefined;
                  if (m.role === "assistant") {
                    for (let j = i - 1; j >= 0; j--) {
                      if (messages[j].role === "user") {
                        prevUser = messages[j].content;
                        break;
                      }
                    }
                  }
                  return (
                    <MessageBubble
                      key={m.id}
                      message={m}
                      prevUserMessage={prevUser}
                    />
                  );
                })}
              </div>
            )}
          </div>
        </div>

        {/* Input area */}
        <div className="shrink-0 border-t bg-bg/80 backdrop-blur">
          <div className="mx-auto w-full max-w-none px-6 py-3 sm:px-10 md:px-16 lg:px-24">
            <ChatBox onSend={handleSend} onStop={handleStop} busy={busy} />
            <p className="mt-2 text-center text-[11px] text-muted">
              {APP_NAME} 可能产生不准确的信息。请以原文为准。
            </p>
          </div>
        </div>
      </main>
    </div>
  );
}

function Hero({
  mode,
  kbName,
  onPick,
}: {
  mode: HeroMode;
  kbName: string | null;
  onPick: (p: string) => void;
}) {
  const suggestions = SUGGESTIONS_BY_MODE[mode];
  const subtitle =
    mode === "unbound"
      ? "通用聊天模式 · 模型直答 + 实时搜索兜底"
      : mode === "travel"
      ? "TravelGPT 演示库 · 4 城本地老饕"
      : `📚 在「${kbName ?? "知识库"}」中提问`;
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
      <Brand size="lg" showWordmark={false} className="mb-5" />
      <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
        {APP_NAME}
      </h1>
      <p className="mt-2 text-sm text-fg/80 sm:text-base">{subtitle}</p>
      <p className="mt-1 text-xs text-muted">
        上传文档 · 抓取网页 · 一句话问
      </p>

      <div className="mt-8 grid w-full max-w-xl grid-cols-1 gap-2 sm:grid-cols-2">
        {suggestions.map(({ text, Icon }) => (
          <button
            key={text}
            onClick={() => onPick(text)}
            className="card card-hover group flex items-start gap-3 px-4 py-3 text-left text-sm"
            type="button"
          >
            <Icon className="mt-0.5 h-4 w-4 flex-none text-accent transition group-hover:scale-110" />
            <span>{text}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
