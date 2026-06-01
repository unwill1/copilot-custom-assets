---
name: flash-eide
description: 当需要在 EIDE 工程中根据 .eide/eide.yml 的上传配置执行烧录，调用自带脚本解析上传器配置、选择产物并保留原始终端输出时使用。
---

# EIDE 烧录

## 适用场景

- 需要在 EIDE 工程中执行下载，并希望稳定拿到原始终端输出。
- 工作区中已经存在 builder.params 和 EIDE 上传配置文件。
- 当前工程通过 EIDE 配置了 uploader，并希望沿用同一套下载参数。
- 需要从当前 out_dir 自动选择 AXF、ELF、HEX、BIN 主产物后执行烧录。

## 必要输入

- 优先提供工作区路径或 builder.params。
- 可选提供 artifact；未提供时脚本会自动扫描当前 out_dir。
- 可选覆盖 device、interface、speed、base-address。

## 自动探测

- 默认从工作区扫描 builder.params；找到多个候选时阻塞并要求用户指定。
- 默认读取 .eide/eide.yml 中的 uploader 和下载配置。
- 默认从 out_dir 中按 ELF > HEX > BIN 优先级选择主产物。
- 当前版本聚焦 J-Link uploader，并将实际烧录委托给下层脚本完成。

## 执行步骤

1. 先阅读 [references/usage.md](references/usage.md)，确认本次是环境探测、候选扫描，还是直接烧录。
2. 不确定上下文时，先执行一次探测：
   ```bash
   python scripts/eide_flasher.py --detect --workspace <工程根目录>
   ```
3. 对于常见单工程场景，直接执行烧录：
   ```bash
   python scripts/eide_flasher.py --flash --workspace <工程根目录>
   ```
4. 读取脚本输出的 uploader、artifact 和底层烧录日志，重点关注连接状态、擦写过程和失败分类。
5. 将烧录结果交给串口监视或调试相关 skill。

## 失败分流

- `environment-missing`：缺少 builder.params、.eide/eide.yml 或 J-Link 工具。
- `ambiguous-context`：扫描到多个 builder.params，无法自动判定当前工程。
- `artifact-missing`：构建产物不存在，或 BIN 缺少基地址信息。
- `project-config-error`：当前 uploader 不属于已支持类型，或配置字段缺失。
- `connection-failure`：J-Link 无法连接目标板。

## 平台说明

- 该 skill 复用 EIDE 的工程配置，不要求用户手工重复填写下载参数。
- 当前版本按 EIDE 的 J-Link 配置执行烧录；若 uploader 类型不受支持，会显式报错而不是静默退化。
- 烧录日志中的中文字符由下层脚本负责兼容常见编码。

## 输出约定

脚本执行完成后，必须将以下关键信息提取并呈现给用户：

- 烧录状态（成功/失败）
- builder.params 路径和 uploader 类型
- 选中的 artifact 路径、类型和大小
- 设备名、接口类型、速度等关键下载参数（若配置中存在）
- 下层脚本的原始输出摘要
- 若失败：失败分类和日志证据

示例输出格式：

```text
烧录成功 ?
  builder.params: build/GD32H73x_75x/builder.params
  uploader: J-Link
  固件: build/GD32H73x_75x/Project.axf
  状态: Erase Done -> Programming Done -> Verify OK
```

- 输出 builder.params、uploader、选中的 artifact，以及下层脚本原始输出。
- 成功后推荐串联 serial-monitor、debug-jlink 或 workflow。

## 交接关系

- 可与 [build-eide](../build-eide/SKILL.md) 串联，形成 build + flash。
- 当前版本委托 [flash-jlink](../flash-jlink/SKILL.md) 完成实际烧录。
