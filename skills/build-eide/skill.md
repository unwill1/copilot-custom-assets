---
name: build-eide
description: 当需要通过 EIDE 的 unify_builder 构建工程，调用自带脚本定位 builder.params、执行构建并提取固件产物与构建日志时使用。
---

# 构建 EIDE 工程

## 适用场景

- 工作区中已有 EIDE 生成的 builder.params。
- 用户希望执行构建、重建或确认最新 AXF、ELF、HEX、BIN 产物。
- VS Code 的 `${command:eide.project.build}` 任务可以构建，但任务终端输出不稳定，不方便 AI 稳定追踪。
- 烧录或调试流程需要新的 AXF（ELF）、HEX 或 BIN。

## 必要输入

- 工作区路径，或明确指定的 builder.params 文件路径。
- 可选的 unify_builder 可执行文件路径。

## 自动探测

- 脚本自动扫描工作区中的 build/**/builder.params。
- 若只找到一个 builder.params，则直接使用。
- 若找到多个 builder.params，则列出候选，避免静默猜错当前工程。
- unify_builder 优先从 builder.params 中的 EIDE_BUILDER_DIR 推断，其次读取配置文件，再退回 PATH。
- 构建完成后自动扫描 outDir 中的 AXF、ELF、HEX、BIN，并按 ELF > HEX > BIN 排序。

## 执行步骤

1. 先阅读 [references/usage.md](references/usage.md)，确认本次是环境探测、工程扫描，还是直接执行构建。
2. 对于常见单工程场景，优先一次调用完成探测和构建：
   ```bash
   python scripts/eide_builder.py --detect --build --workspace <工程根目录>
   ```
3. 仅在工作区中可能存在多个 EIDE 工程时，先执行 `--scan` 列出 builder.params，再明确指定 `--builder-params`。
4. 读取脚本转发的原始构建日志和末尾汇总，重点关注首选产物、Program Size、Flash/RAM 汇总和失败分类。
5. 将构建目标、工具链、产物路径和大小信息交给下游 skill。

## 失败分流

- `environment-missing`：未找到 builder.params 或 unify_builder。
- `ambiguous-context`：存在多个 builder.params，无法安全判断当前工程。
- `project-config-error`：unify_builder 执行失败、工程配置损坏或参数无效。
- `artifact-missing`：构建看似成功，但未找到可用产物。

## 平台说明

- 该 skill 实际调用的是 EIDE 底层的 unify_builder，不依赖 VS Code 任务 API，因此更容易稳定拿到完整构建日志。
- Windows 上通常直接调用 builder.params 中 EIDE_BUILDER_DIR 下的 unify_builder.exe。
- 若 EIDE 升级后 unify_builder 路径变化，脚本会优先使用 builder.params 中记录的新路径。
- 构建日志中若包含中文字符，脚本会自动尝试 UTF-8、GBK 等常见编码。

## 输出约定

脚本执行完成后，必须将以下关键信息提取并呈现给用户：

- 构建状态（成功/失败）
- builder.params 路径、目标名和工具链
- 输出目录和首选产物
- 固件大小明细（若日志中存在 Program Size）
- Flash/RAM 汇总（若日志中存在 Total Memory Usage）
- 错误、警告统计和构建耗时
- 若失败：失败分类和日志证据

示例输出格式：

```text
构建成功 ?
  builder.params: build/GD32H73x_75x/builder.params
  目标: GD32H73x_75x | 工具链: AC6
  首选产物: build/GD32H73x_75x/Project.axf
  固件大小: Flash ≈ 12.3 KB  RAM ≈ 5.6 KB
  耗时: 00:00:03
```

- 原样转发 unify_builder 的主要日志，便于 AI 和用户一起看编译细节。
- 用 `artifact_path`、`artifact_kind`、`target_name`、`toolchain` 等字段交给后续 skill 或 workflow。

## 交接关系

- 当下一步需要烧录时，将成功构建结果交给 flash-eide、flash-jlink、flash-openocd 或 flash-keil。
- 当下一步需要 ELF 符号或调试时，将首选 AXF、ELF 交给调试相关 skill 或 MCP 调试脚本。