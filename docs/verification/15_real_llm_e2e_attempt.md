# 真实 LLM 端到端验收记录（进行中，未通过）

> 记录日期：2026-09-28
> 分支：`feat/one-shot-execution`
> 脚本：[`demo/real_llm_e2e.py`](../../demo/real_llm_e2e.py)
> 状态：**未通过**。链路本身已跑通，但任务未能以最终回答收口

---

## 1. 模型接口确认（通过）

```text
$ .venv\Scripts\python.exe -m demo.llm_interface_check
base_url    : https://api.deepseek.com
model       : deepseek-chat
api_key set : True (value never printed)
elapsed_s   : 2.52
reply       : 'PONG'
usage       : {'prompt': 10, 'completion': 2, 'total': 12}
attempts    : 1
```

密钥仅以布尔值出现，全文未输出任何密钥内容。

---

## 2. 端到端任务：三次运行

任务：上传 `sales.csv` → 解析 → 按 region 汇总 → 写 `reports/region_summary.csv` →
保存产物 → 给出总收入与小计。`require_artifact=true`。

| 运行 | 结果 | 耗时 | Token（prompt/completion/total） | 失败原因 |
| --- | --- | --- | --- | --- |
| #1 | FAILED | 21.5s | 25377 / 1903 / 27280 | `exceeded 8 tool-calling iterations` |
| #2 | FAILED | 26.5s | 27777 / 2175 / 29952 | `exceeded 8 tool-calling iterations` |
| #3 | FAILED | 37.0s | 44440 / 2282 / 46722 | `exceeded 12 tool-calling iterations` |

模型固定为 `deepseek-chat`；三次都是真实调用（无 fake LLM）。

---

## 3. 每次失败暴露的真实问题（均已在本分支修复）

### #1：`out/` 路径在调用之间漂移

模型写 `out/agg.json`，提交后文件落到**任务根目录**；下一次调用再读 `out/agg.json`
就 `FileNotFoundError`，模型反复重试直到耗尽迭代。

修复：`out/` 改为**任务内持久**的产物目录——每次调用把它预置进调用级私有目录，
成功后按"允许覆盖"提交回 `<task>/out`，失败则整批丢弃。路径从此稳定。

### #2：只读目录缺提示 + 迭代预算过紧

模型把文件写到任务根目录，得到裸的 `OSError: Read-only file system`，试错两轮；
8 轮预算不够。

修复：包装脚本在 `EROFS` 时追加明确提示（"产物写到 out/"）；迭代上限提升为
可配置的 `EXECUTOR_MAX_ITERATIONS`（默认 12）。

### #3：提交协议拒绝了合法的嵌套产物

用户要求 `reports/region_summary.csv`，模型写了 `out/reports/...`，被提交协议
（默认禁止嵌套）整批拒绝，且被判定为终止性错误。

修复：新增 `SANDBOX_COMMIT_ALLOW_NESTED`（默认 true，深度仍在 CallWorkspace 内受限）；
并把"提交前校验拒绝"从终止性错误改为**可重试的普通工具错误**（因为此时没有任何文件
落盘，模型换一个路径即可）。

---

## 4. 第 3 次运行的实际链路（关键证据）

第 3 次运行虽然整体 FAILED，但**业务链路已经完整走通**：

| 步骤 | 工具 | 结果 |
| --- | --- | --- |
| 0 | `read_file` | 成功读回 CSV 全部内容 |
| 1-2 | `run_python` | 成功解析并求和，得到 `north 1500 / south 950 / east 450 / total 2900` |
| 3 | `run_python` | 写根目录被拒（只读），包装脚本给出 out/ 提示 |
| 4 | `run_python` | 改为写 `out/`，成功生成 4 行 CSV |
| 5 | **`save_report`** | **成功上传产物**，返回真实预签名 URL：`http://localhost:9000/doc-agent-storage/reports/<task_id>/region_summary.csv?...` |
| 6 | `run_python` | 回读校验，`match True`，`total 2900` |

`plan_events` 显示 s1–s5 全部 `plan_step_completed`，s6 已绑定但**始终没有完成事件**，
随后模型在 12 轮迭代内未能产出最终回答。

---

## 5. 结论与待办

- 真实 LLM + 一次性容器 + 文档解析 + 代码执行 + 产物提交（含 OSS 上传）**已实际打通**；
- 未通过的部分是**收口**：模型跑完业务步骤后无法在迭代预算内完成计划并给出最终回答；
- 因此本次端到端验收记为 **未通过**，`require_artifact` 校验也未能执行（任务未到 SUCCESS）；
- 下一步需要定位第 6 步之后的循环（s6 未完成、无 binding/completion 拒绝事件），
  这属于 Executor/Planner 的收敛问题，与新后端无关但阻塞验收；
- P0-5 保持未完成，分支不合并。
