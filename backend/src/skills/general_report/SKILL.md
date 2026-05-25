---
name: general_report
description: 通用 KB 知识总结报告，按结构化字段渲染成 Markdown
---

# 输出格式（严格遵守）

## {{title}}

> **TL;DR**：{{tldr}}

{{#each sections}}
### {{this.heading}}

{{this.content}}

{{/each}}

---

## 来源

{{#each citations}}
- 【{{this.tag}}】{{this.source}}{{#if this.score}} (相关度 {{this.score}}){{/if}}
{{/each}}

# 写作风格

- 标题简明扼要，能概括整篇主旨
- TL;DR 用一句话讲清结论，不超过 80 字
- 每个 section heading 是名词短语，content 用完整段落或列表
- citations 严格列出引用过的 chunk filename / web URL，tag 用「📚 KB」(KB chunks) 或「🌐 Web」(web_search 结果)
- 不要写 "希望这份报告对你有帮助" 这类客套话
- 不要在报告里再次声明 "这是 AI 生成"，模板 footer 自带
