# 系统架构

Reliable Agent Runtime 将模型提出的动作与可信控制决策分离：

```text
CLI ----------------> OpenAI-compatible LLM --+
  \-----------------> Fake LLM ----------------+--> Agent Loop
FastAPI 任务管理 ----> Fake LLM 确定性回放 ----+
                                                    |
                                             结构化动作
                                                    |
                                             Policy Engine
                                            /      |       \
                                         允许     拒绝     等待审批
                                          |
                                      Tool Registry
                                          |
                                工作区工具 / Docker 命令
                                          |
                            Verifier + SQLite + Prometheus
                                          |
                                 继续 / 完成 / 终止

Tool Registry -- 工具风险与副作用元数据 --> Policy Engine
SQLite 完整轨迹 --> Context Builder --> 有界模型上下文
```

模型可以提出动作或申请结束任务，但不能直接修改任务状态、绕过工具参数 Schema、批准高风险动作，也不能将外部验证结果标记为成功。

当前 CLI 可以选择 OpenAI-compatible Client 或 Fake LLM。FastAPI 主要提供任务生命周期管理、持久化、审批和确定性动作回放，执行任务时使用 Fake LLM，因此不能将 API 层描述为已经直接接入真实模型。两种入口最终复用同一个 Agent Loop 和工具、策略、验证契约。

## 核心组件

### Agent Loop

Agent Loop 负责在模型、工具和 Verifier 之间传递结构化消息，并根据任务状态决定继续执行、等待审批或终止。模型输出必须解析为明确的工具调用或 `finish` 动作，无法解析的响应会转化为结构化错误。

### Tool Registry

Tool Registry 维护可用工具及其 Pydantic 参数 Schema。文件工具将所有路径解析到指定工作区，写入工具额外检查受保护路径；测试和 Lint 只能执行预先配置的命令，不能接收任意 Shell 字符串。

### Policy Engine 与人工审批

Agent Loop 收到模型的结构化动作后，先从 Tool Registry 获取目标工具的风险和副作用元数据，再由 Policy Engine 在副作用发生前返回 `ALLOW`、`DENY` 或 `REQUIRE_APPROVAL`。动作获准后，Tool Registry 才会校验参数并执行对应工具。需要人工确认的动作会进入 `WAITING_APPROVAL`，审批结果与任务、步骤及动作指纹绑定，防止批准被其他动作复用。

### Verifier

模型的 `finish` 只表示申请结束。测试、Lint、受保护路径检查等 Verifier 独立判断验收条件是否成立；验证失败时，证据会返回 Agent Loop，任务不会被误标为成功。

### Persistence 与 Recovery

SQLite 保存任务、消息、步骤、工具执行、产物、审批和验证结果。修改动作记录执行前工作区哈希和幂等键；服务重启后通过事务状态与实际工作区变化判断动作是否已经生效，避免盲目重放补丁。

### Context Builder

数据库保留完整执行历史，Context Builder 只为下一次模型调用生成有界视图。目标、约束和验收条件固定保留，近期步骤保留完整结构，较早历史压缩为摘要。大型工具输出使用内容哈希引用，文件变化后旧读取结果会被标记为失效。

### Observability

模型调用、工具执行、重试、策略判定、上下文压缩和任务结束都会生成结构化事件。Prometheus指标覆盖任务结果、延迟、工具调用、Token、预算终止、验证失败和上下文压缩。

## 信任边界

- API 与 Runtime 管理任务生命周期和持久化状态；
- Policy Engine 在工具产生副作用前判断动作；
- 文件工具只能访问指定工作区，受保护路径不可修改；
- 配置好的测试与 Lint 命令在资源受限的 Docker 容器中执行；
- 写入任务是否完成由 Verifier 判断，而不是由模型文本决定；
- 完整轨迹保存在 SQLite 中，模型只接收 Context Builder 生成的有界上下文。

Docker 隔离用于降低命令意外影响主机的风险，不等同于面向主动恶意代码的生产级安全沙箱。
生产环境仍需配合独立执行节点或 microVM、Docker daemon 权限隔离、可信镜像来源及宿主机加固。
