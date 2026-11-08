# 结果校验与必要行为检查

## 判定语义

用例通过 = 所有配置的必需检查项都通过。Token 和耗时独立统计，不隐式作为门槛。

- `tool_match: "required"`：`tools` 中的工具必须至少成功一次，允许额外调用、重复调用和不同顺序。失败调用本身不满足必要行为。
- `tool_match: "exact"`：严格比较全部工具调用序列，用于参数纠错、重试、回读等机制回归。旧数据集省略该字段仍采用 exact。
- `forbidden_tools`：任何一次调用命中即失败，即使该调用被拒绝或失败。
- `completed_steps`：仅检查配置的步骤 ID 是否完成，不要求所有运行步骤与预设一致。真实模型用例不应绑定自由生成的步骤 ID；可改用产物和必要工具验收。
- `replan`：true 要求发生，false 禁止发生，null 不限制。required 模式省略时不限制；exact 模式省略时保留旧版禁止重规划语义。
- `required_events`、失败恢复和已有产物存在性校验继续保留。

`tool_selection_accuracy` 为兼容旧报告保留字段名：exact 模式表示按位置匹配率，required 模式表示必要工具成功覆盖率。比较版本时必须同时看数据集版本和 tool_match，不能混用两个定义。

## CSV 内容验收

`csv_result` 配置交付文件名、分组键、独立标准答案和绝对误差。评分器只读取 task.result.artifacts 中唯一匹配的交付对象，不采信模型回答中的成功声明或数值。

CSV 必须是 UTF-8（允许 BOM），包含必需列、完整且唯一的分组。按分组键匹配而非行顺序，允许额外列。使用 Decimal 比较数值，拒绝 NaN/Infinity、缺列、缺组、多组、重复分组和错误数值。读取失败、没有 reader、缺失/歧义产物均失败，不跳过检查。内容检查限定 2 MiB（下载完成后检查，不是流式下载内存限额）。

失败报告在 JSON 的 check_details 和 Markdown 中写出预期值、实际值或读取/解析原因。

独立输入：`evaluation/fixtures/sales.csv`：East 为 10、20，West 为 30。
人工可直接复核的标准答案：East 合计 30、平均 15；West 合计 30、平均 30；ALL 合计 60、平均 20。答案保存在 expectation 中，不暴露给被测模型。固定测试的输出脚本和标准答案分别维护，不从 expectation 生成实际输出。

## 运行

```bash
python -m pytest tests/test_evaluation.py tests/test_evaluation_results.py -q
python -m evaluation --mode fake
```

默认核心集升级为 1.1.0，仍是九类场景。fake 模式使用模拟工具写入的实际 CSV 字节验证评分链路，不代表真实模型计算能力。

真实模型统计用例：先配置 LLM、Docker 沙箱和对象存储，然后将输入上传到指定 key：

```bash
python -c 'from pathlib import Path; from storage.storage_manager import StorageManager; StorageManager().upload_file_content("raw/evaluation/sales.csv", Path("evaluation/fixtures/sales.csv").read_bytes())'
python -m evaluation --mode real --allow-external --dataset evaluation/datasets/statistics_result.json
```

real 模式读取对象存储里 Agent 实际交付的 CSV；不使用 fake 响应。此用例不限制计算工具或计划步骤 ID，只要求 save_report 成功、交付产物通过已有校验且 CSV 内容正确。真实运行需外部服务和凭据，并产生模型费用。

## 范围

当前验收覆盖结构化 CSV 统计，不评价自由文本总结，也不检查最终回答与 CSV 是否一致；不包含 LLM Judge。禁止工具检查针对记录中配置的工具名，不等同于完整安全审计。
