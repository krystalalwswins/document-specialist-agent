# P0-1：结构化计划模型与持久化

日期：2026-09-18。开发基线：`d2a8ba7f4c5638158ae3be5151a4b3ab034fcc21`。
功能分支：`feat/harness-p0-1`。本轮仅完成 task_points.md 的 P0-1。

## 解决的问题

原计划只是名称、描述和推荐工具的列表，未保存到任务中。执行日志中的步骤
其实是工具调用，无法回答“原计划是什么”。现在计划有独立数据模型和持久化字段，
为后续执行绑定提供基础，但不提前实现 P0-2 的调度和重规划。

## 调用链与职责

1. `Planner.plan` 给模型提供 `create_plan` Schema；模型只生成参数，不执行工具。
2. Planner 检查响应恰好包含一个 `create_plan`，解析 JSON 并校验 Schema。
3. `Plan.from_dict` 检查字段与依赖图，使用标准库 `TopologicalSorter` 检测环。
4. Orchestrator 调用 `TaskManager.set_plan`，在锁内将初始计划绑定到运行中的任务。
5. `FileTaskStore` 复用原子 JSON 写入；成功后才调用 Executor。
6. Executor 通过原有 `plan.summary()` 获取含依赖和完成条件的摘要。
7. 查询接口经 `Task.to_dict()` 返回完整 `plan`。

`task/plan_model.py` 仅依赖标准库，不反向依赖 Agent 或模型 SDK。
`agent.planner` 继续导出 Plan/PlanStep，保持现有导入路径兼容。
构造器可以组装草稿；从模型解析、绑定任务、序列化时必须通过完整校验。

## 保存结构示例

```json
{
  "plan": {
    "user_input": "分析销售数据并保存报告",
    "version": 1,
    "steps": [
      {
        "step_id": "read",
        "name": "读取数据",
        "description": "读取销售表并识别字段",
        "tool": "parse_document",
        "depends_on": [],
        "completion_criteria": ["确认区域和销售额字段存在"]
      },
      {
        "step_id": "report",
        "name": "生成报告",
        "description": "汇总并保存结果",
        "tool": null,
        "depends_on": ["read"],
        "completion_criteria": ["生成非空报告文件"]
      }
    ]
  },
  "steps": []
}
```

`plan.steps` 描述打算做什么；外层 `steps` 记录实际调用了什么。
版本由 Runtime 设置，模型不能修改任务目标或计划版本。
完成条件本轮是可观察证据的文字列表，尚没有自动判定执行器。

## 兼容与失败行为

- 旧 JSON 没有 `plan` 字段时读取为 `None`，API 输出 `null`，原调用记录保留。
- 保存前深拷贝计划，调用方修改原计划对象不会污染已保存快照。
- 初始计划只能在 RUNNING 且尚无计划时绑定，版本必须为 1、目标必须匹配。
- 计划缺字段、重复 ID、未知依赖、自环或多节点环均拒绝。
- 支持非拓扑排序的合法 DAG；这不表示 Executor 已按依赖调度。
- 模型输出无 Tool Call、错误函数名、多计划调用、非法 JSON 或空计划均报 PlannerError。
- 计划无效或持久化失败时，工具执行不会启动；Orchestrator 走原有失败处理。

## 实际验证

环境：Linux、Python 3.12.14、pytest 9.1.1。复用工作区已安装依赖，未修改依赖文件。

| 验证 | 结果 |
| --- | --- |
| 新增验收用例在旧实现上运行 | 21 failed，确认可捕获缺失能力 |
| 实现后的相关测试 | 125 passed |
| 补充边界后的完整 `python -m pytest -q` | 276 passed，1 warning |
| `git diff --check` | 通过 |

工作区依赖路径通过 PYTHONPATH 注入后的实际命令：

```bash
PYTHONPATH=/workspace/scratch/dcdbf9951bca/doc-agent-venv/lib/python3.12/site-packages python -m pytest -q
```

本地正常虚拟环境直接执行 `python -m pytest -q` 即可。
警告来自 Starlette 使用 AnyIO 的弃用别名，不影响通过结果。
Linux 可运行符号链接测试，因此本次没有 README 中 Windows 基线的那项 skip。

测试覆盖 DAG 往返序列化、所有依赖错误、旧 JSON、重建 Manager 后 API 查询、
持久化先于执行、计划对象隔离、禁止覆盖、错误任务/版本/状态、存储失败及模型非法响应。
历史安全测试只修订 FakePlanner 的有效计划输入，保留原超时终止断言。

本次全部为离线工程测试，没有真实模型效果、Docker 或 MinIO 验证。
未实现 P0-2 及后续编号，也未变更沙箱超时行为。
