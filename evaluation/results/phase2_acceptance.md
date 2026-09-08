# Reliable Agent Runtime：Phase 2 工程验收报告

## 1. 验收范围

Phase 2 的目标是建立真实模型驱动的代码修改闭环：模型读取代码、应用补丁、运行受控命令，并由 Runtime 的确定性 Verifier 决定任务是否完成。

本阶段实现：

- OpenAI-compatible Client，记录 token usage、模型延迟和响应元数据；
- `apply_patch`、`git_diff`、`run_tests`、`run_linter`；
- 受保护路径策略，`tests/` 修改会被拒绝；
- 测试和 lint 仅接受配置中声明的命令名称；
- 命令超时、补丁失败和模型错误映射为结构化结果；
- `CommandExitCodeVerifier` 与 `ProtectedPathsUnchangedVerifier`；
- 模型返回 `finish` 后触发外部验证，失败证据重新进入 Agent 上下文；
- 独立的故障示例项目与可重复执行轨迹。

## 2. 确定性验收

环境：Conda `llm-inference-lab`，Python 3.11.13。

确定性 Fake LLM 轨迹：

1. 搜索并读取折扣实现；
2. 应用一个错误的首次补丁；
3. pytest 失败；
4. 模型请求 `finish`，Verifier 拒绝完成；
5. 失败证据返回 Agent；
6. 应用第二个补丁；
7. pytest、ruff 和受保护路径验证全部通过；
8. 任务以 `VERIFIED_COMPLETE` 结束。

该部分验收通过。

## 3. 真实模型实验

实验配置：

- 服务：vLLM 0.28.0，OpenAI-compatible Chat Completions；
- 模型：`Qwen/Qwen2.5-1.5B-Instruct`；
- GPU：AutoDL RTX 4090；
- 最大上下文：8192 tokens；
- 生成参数：temperature 0，最多 256 输出 tokens，禁止并行工具调用；
- 工具解析：Hermes parser。

2026-09-09 最终复测使用隔离工作区
`/private/tmp/rar-phase2-vllm.TTMoor`，任务 ID 为
`ac1bea7c-ccc2-4f89-8eaa-6f3c03f60e87`。基线提交在实验开始前创建，
因此模型产生的修改可以与原始 fixture 明确区分。

验证通过的能力：

- `/v1/models` 与 `/v1/chat/completions` 可访问；
- 模型能生成结构化 `tool_calls`；
- Runtime 能解析真实模型工具调用并回传工具结果；
- 非白名单命令被拒绝后，模型能够改用允许的 `ruff`；
- 模型尝试修改 `tests/test_discount.py` 时，`apply_patch` 返回 `protected_path`；
- 模型多次声明完成，但 pytest 仍失败，Verifier 始终没有将任务标记为 `COMPLETED`；
- 上下文超限错误被映射为 `FAILED / UNRECOVERABLE_ERROR`，此前轨迹得到保留。

最终复测的关键轨迹：

1. `git_diff` 成功，确认初始工作区无修改；
2. `list_files` 成功，模型获得 `src/discount.py` 和测试文件的位置；
3. `run_linter(ruff)` 退出码为 0；
4. `run_tests(pytest)` 退出码为 1，两个断言明确显示 gold 折扣实际值为
   `0.01`、期望值为 `0.10`；
5. 模型随后反复输出“将搜索并修复”的完成文本，甚至生成了未被 Runtime
   执行的伪工具调用；
6. 每次 `finish` 都被 Verifier 拒绝，`tests/` 始终未被修改；
7. 输入达到至少 7937 tokens，加上 256 个预留输出 tokens 后超过 8192，
   vLLM 返回 HTTP 400；Runtime 最终记录为
   `FAILED / UNRECOVERABLE_ERROR`。

未通过的能力：

- 最终复测中，1.5B 模型没有读取或修改 `src/discount.py`；
- 被拒绝修改 tests/ 后，模型重复请求完成，未能恢复到正确工具路径；
- 最终真实模型任务没有达到 `VERIFIED_COMPLETE`。

## 4. 验收判断

- Phase 2 工程实现与确定性回归：**通过**；
- vLLM/OpenAI-compatible 真实接入：**通过**；
- Qwen2.5-1.5B-Instruct 真实代码修复任务：**未通过**；
- Phase 2 按规划的最终验收：**待更强工具调用模型复测**。

这一结果不能被记录为真实模型修复成功。它提供了一个可复现的失败案例，并证明 Runtime 的安全边界和外部验证在弱模型产生错误动作及虚假完成时能够生效。
