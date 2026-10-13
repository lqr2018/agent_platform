# 平台概览

本平台是一个**配置驱动**的 LLM Agent 编排与执行平台：Agent、工具、知识库、工作流都是配置，
不需要为每个新场景写代码。

## 三类 Agent（第 8 章的演示口径）

1. **研究型 Agent（Research Agent）**：绑 `research-flow` 工作流，规划 → 检索 → 撰写；
2. **数据型 Agent（Data Analysis Agent）**：读取文件、执行代码、汇总数据，危险工具默认关闭；
3. **知识库问答型 Agent（Knowledge Assistant）**：只依据知识库回答，未命中时明确拒答。

## 核心概念

- **工具（Tool）**：内置 5 个 —— `web_search`、`python_execute`、`file_read`、`file_write`、`calculator`；
- **知识库（Knowledge Base）**：上传文档 → 摄取（解析 / 切片 / 向量化）→ 检索时按 `score_threshold` 过滤；
- **工作流（Workflow）**：节点图（start / agent / tool / condition / retriever / end），支持断点续跑。

## 可观测性

每次运行（Run）都会留下 `runs` / `workflow_runs` / `node_runs` 记录，
前端可以查看工具调用入参出参、检索命中与节点耗时。
