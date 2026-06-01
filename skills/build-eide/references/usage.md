# EIDE 构建 Skill 用法

## 基础用法

```bash
# 探测环境、builder.params 和 unify_builder
python scripts/eide_builder.py --detect --workspace /path/to/workspace

# 列出工作区中的 builder.params 候选
python scripts/eide_builder.py --scan --workspace /path/to/workspace

# 直接构建（单工程场景）
python scripts/eide_builder.py --build --workspace /path/to/workspace

# 指定 builder.params 构建
python scripts/eide_builder.py --build --builder-params /path/to/build/<target>/builder.params

# 仅扫描产物
python scripts/eide_builder.py --scan-artifacts --builder-params /path/to/build/<target>/builder.params

# 保存探测到的 unify_builder 路径到工作区配置
python scripts/eide_builder.py --detect --save-config --workspace /path/to/workspace
```

## 参数说明

| 参数 | 说明 |
| --- | --- |
| --detect | 探测 builder.params 和 unify_builder 环境 |
| --scan | 扫描工作区中的 builder.params |
| --build | 执行构建 |
| --scan-artifacts | 仅扫描产物 |
| --workspace | 工作区目录 |
| --builder-params | 指定 builder.params 路径 |
| --builder | 手动指定 unify_builder 可执行文件路径 |
| --save-config | 将探测到的 unify_builder 路径保存到 .em_skill.json |
| -v, --verbose | 输出更多细节 |

## 输出说明

- 构建命令和 builder.params 路径
- EIDE 目标名、工具链和输出目录
- 原始 unify_builder 日志
- Program Size 和 Total Memory Usage 摘要（若日志中存在）
- 产物列表与首选产物（ELF > HEX > BIN）

## 返回码

- 0：执行成功
- 1：环境缺失、构建失败、参数错误或产物缺失