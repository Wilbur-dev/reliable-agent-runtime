# Reliable Agent Runtime：Phase 0 + Phase 1 工程验收报告

## 1. 报告目的

本报告用于展示 Reliable Agent Runtime 在 Phase 0 和 Phase 1 完成后的工程能力、设计边界和可复现实验结果。

项目目标不是实现一个特定业务 Agent，而是从零构建一个轻量、可控、可测试的单 Agent Runtime。当前两个阶段完成了从“协议定义”到“只读任务闭环”的最小可用链路。

## 2. 版本与实验环境

- 验收日期：2026-09-09
- 验收分支：`feat/phase-1-readonly-loop`
- Phase 0 提交：`3073931 feat: implement phase 0 runtime protocols`
- Phase 1 提交：`38db93a feat: implement phase 1 read-only agent loop`
- 对比基线：`main`（`0b1617c`，已合并 Phase 0）
- Conda 环境：`llm-inference-lab`
- Python：3.11.13

## 3. 阶段能力验收

### Phase 0：可验证的运行时协议

Phase 0 建立 Runtime 的基础抽象，使模型输出、任务状态和资源限制不再依赖非结构化约定。

已完成：

- 定义 `Task`、`Budget`、`TaskStatus`、`TerminationReason` 和 `ToolResult`；
- 定义 `ToolCallAction`、`FinishAction` 和结构化 LLM 响应；
- 提供 LLM Client 抽象接口；
- 实现按预设动作稳定响应的 `FakeLLMClient`；
- 使用严格 Schema 拒绝未知字段、非法动作和无效预算。

验收结论：核心协议具备明确类型、严格校验和确定性测试能力，可作为后续执行循环、持久化、策略及评估模块的稳定接口。

### Phase 1：受控的只读 Agent 闭环

Phase 1 在 Phase 0 协议之上实现最小 Agent 执行链路：模型选择动作，Runtime 校验并执行工具，将结果反馈给模型，直至完成任务或触发预算终止。

已完成：

- 实现工作区范围内的 `list_files`、`search_text` 和 `read_file`；
- 实现工具注册、Schema 导出、参数校验和结构化失败结果；
- 阻止父目录穿越、绝对路径访问和符号链接逃逸；
- 限制文件大小、搜索结果数量和输出字符数；
- 记录完整步骤轨迹、工具结果、任务状态和终止原因；
- 支持最大步骤数和最大工具调用数预算；
- 提供 CLI、固定动作文件和可重复运行的示例工作区。

验收结论：Runtime 已具备一个确定、可追踪且受工作区边界保护的只读闭环。

## 4. 自动化验证结果

执行命令：

```bash
conda run -n llm-inference-lab pytest -o addopts='' -ra
conda run -n llm-inference-lab ruff check .
conda run -n llm-inference-lab ruff format --check .
git diff --check main...HEAD
```

实际结果：

| 检查项 | 结果 |
| --- | --- |
| pytest | `23 passed in 0.24s` |
| Ruff 静态检查 | `All checks passed!` |
| Ruff 格式检查 | `28 files already formatted` |
| Git diff 检查 | 通过，无空白错误 |

23 项测试覆盖：

- Action、Task 和 Budget Schema 校验；
- Fake LLM 动作顺序及动作耗尽行为；
- 工具注册、Schema 导出、未知工具和非法参数处理；
- 隐藏文件过滤、分段读取和文本搜索；
- 父目录穿越、绝对路径及符号链接逃逸拦截；
- 大文件拒绝、结果数量限制和输出截断；
- Phase 1 完整 Agent 执行轨迹。

## 5. 可复现端到端实验

### 实验目标

示例工作区包含一个虚构的订单服务。本实验要求 Agent 在不知道目标文件位置的情况下，找出订单折扣逻辑并解释规则。

该任务只是测试夹具，用来验证 Runtime 能否完成“观察工作区 → 搜索代码 → 读取证据 → 返回结论”的完整链路；折扣计算本身不是本项目要实现的业务功能。

### 复现命令

```bash
conda run -n llm-inference-lab python -m app.cli \
  --workspace examples/sample_repo \
  --goal 'Find the order discount logic and explain the rules.' \
  --fake-actions examples/phase1_actions.json
```

### 执行轨迹

| 步骤 | Agent 动作 | Runtime 观察结果 |
| --- | --- | --- |
| 1 | `list_files` | 在示例工作区发现 `src/order_service.py` |
| 2 | `search_text` | 找到 6 行包含 `discount` 的代码 |
| 3 | `read_file` | 成功读取目标文件的 21 行内容 |
| 4 | `finish` | 基于读取到的代码返回结论 |

### 实验结果

- 任务状态：`COMPLETED`
- 终止原因：`MODEL_FINISH`
- 工具调用：3 次，全部成功
- 输出截断：无
- 越界访问：无
- Agent 返回的样例结论：折扣逻辑位于 `src/order_service.py`；standard、silver、gold 客户等级对应的折扣率分别为 0%、5% 和 10%，未知等级回退为 0%。

这里的“折扣摘要”指 Agent 针对此次示例代码阅读任务给出的最终答案。它证明 Agent 的输出与工具实际读取到的源码一致，不代表 Runtime 内置了折扣业务知识。

## 6. 当前边界与后续演进

当前里程碑刻意限定为只读、内存态和 Fake LLM 驱动，以优先验证协议、控制边界及执行循环。尚未包含真实模型接入、写操作审批、持久化恢复、重试策略、并发执行和离线评估指标；这些能力属于后续阶段。

## 7. 总体结论

Phase 0 和 Phase 1 已完成从结构化协议到只读 Agent 闭环的纵向切片。实现不仅能运行，还具备严格输入校验、工作区隔离、资源限制、结构化轨迹和自动化回归测试。

验收结果：**通过。当前版本满足“可控、可复现、可测试的只读单 Agent Runtime”阶段目标。**
