# flash-eide 用法

## 基础用法

```bash
# 探测 builder.params、上传器和默认产物
python scripts/eide_flasher.py --detect --workspace /repo/fw

# 扫描 builder.params 候选
python scripts/eide_flasher.py --scan --workspace /repo/fw

# 自动使用 .eide/eide.yml 的 J-Link 配置烧录
python scripts/eide_flasher.py --flash --workspace /repo/fw

# 显式指定 builder.params
python scripts/eide_flasher.py --flash --builder-params /repo/fw/build/board/builder.params

# 显式指定产物
python scripts/eide_flasher.py --flash --workspace /repo/fw --artifact /repo/fw/build/board/Project.axf

# 覆盖 J-Link 设备名和速度
python scripts/eide_flasher.py --flash --workspace /repo/fw --device GD32H759IM --speed 8000
```

## 返回码

- `0`：烧录成功
- `1`：环境缺失、上下文不明确或烧录失败