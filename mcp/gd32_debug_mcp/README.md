# GD32 Debug MCP Server

让 AI 通过 J-Link 直接操作 GD32(或任意 Cortex-M)目标:读内存、读寄存器、下断点、单步、复位、运行。

## 它能做什么

让你跟 AI 这样对话:

> 我:USART0 乱码,帮我查
>
> AI:[调用 read_named_register("USART0_CTL0")]
>     UEN=0, TEN=0, REN=0 —— USART 没使能。我再看一下时钟...
>     [调用 read_named_register("RCU_APB2EN")]
>     USART0EN=0 —— 找到了,APB2 上 USART0 的时钟根本没开。
>     检查代码里是不是漏了 rcu_periph_clock_enable(RCU_USART0)?

## 启动前提

1. **J-Link 探针**通过 USB 连到电脑(原版/克隆都行)
2. **目标板**供电正常,SWD 4 线接好(VCC/GND/SWCLK/SWDIO)
3. **J-Link 软件**装好(SEGGER 官网下载,会带 `JLinkARM.dll`)
4. **Keil 调试会话关闭**(不能同时占用 J-Link)
5. **Python 3.9+**

## 安装

```bash
cd gd32_debug_mcp
pip install -r requirements.txt
```

## 第一步:验证 J-Link 链路

直接跑测试脚本,确认 Python 能控制 J-Link:

```bash
# 改成你的实际芯片型号
set GD32_TARGET=GD32F407VG
python test_jlink.py
```

期望输出:

```
[1/5] 打开 J-Link... OK
[2/5] 设置 SWD 接口... OK
[3/5] 连接目标 GD32F407VG... OK
[4/5] 读 Core ID... 0x2BA01477
[5/5] 读核心寄存器...
        R0  = 0x00000000
        ...

✓ 全部通过!可以接 MCP server 了
```

**只有这一步通了,MCP 才有可能通。** 如果失败,看脚本里的"常见原因"提示。

## 第二步:接到 Claude Desktop

找到 Claude Desktop 的配置文件:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

添加(或合并)以下内容,把路径改成你电脑上的:

```json
{
  "mcpServers": {
    "gd32-debugger": {
      "command": "python",
      "args": ["D:/your/path/gd32_debug_mcp.py"],
      "env": {
        "GD32_TARGET": "GD32F407VG",
        "GD32_SWD_SPEED_KHZ": "4000"
      }
    }
  }
}
```

**重启 Claude Desktop**。在对话框下方点工具图标,应该能看到 `gd32-debugger` 已连接,并列出所有可用工具。

## 第三步:开始让 AI 调试

直接对话试试:

- "用 `get_target_info` 看一下当前连接状态"
- "读一下 RCU_APB2EN 寄存器"
- "把 CPU 停下来,看一下所有核心寄存器"
- "在 0x08001234 下个断点,然后 resume,等命中"

## 工具一览

| 工具 | 作用 |
|---|---|
| `get_target_info` | 看连接状态、芯片 ID、PC |
| `read_memory` | 读 32-bit 内存(N 个字) |
| `read_memory_bytes` | 读字节(含 ASCII 视图) |
| `write_memory` | 写 32-bit 值并回读验证 |
| `read_core_register` | 读单个核心寄存器(R0-R15/XPSR 等) |
| `read_all_core_registers` | 读所有核心寄存器快照 |
| `read_named_register` | 按名字读 GD32F4 常用外设寄存器 |
| `list_named_registers` | 列出支持的外设寄存器名 |
| `halt` | 停止 CPU |
| `resume` | 让 CPU 继续跑 |
| `step` | 单步一条汇编指令 |
| `step_over` | 跨过函数调用 |
| `reset` | 复位芯片(可选复位后停下) |
| `set_breakpoint` | 在指定地址下硬件断点 |
| `clear_breakpoint` | 删除指定断点 |
| `clear_all_breakpoints` | 删除所有断点 |
| `wait_for_halt` | 等待 CPU 停止(配合 resume + 断点用) |

## 支持其他 GD32 系列

通过环境变量切换芯片:

```json
"env": {
  "GD32_TARGET": "GD32F407VG",     // 你的型号
  "GD32_SWD_SPEED_KHZ": "4000"
}
```

J-Link 支持的型号见 SEGGER 官网"Supported Devices"。GD32 系列大多数都直接支持。

如果换的是 GD32H7 系列,`read_named_register` 里的地址可能要改——那个表是 GD32F4 的。下个版本会接 SVD,直接按 SVD 解析任意系列。

## 常见问题

**Q: 跑 test_jlink.py 报 "Cannot connect to J-Link"**

关掉 Keil(尤其是 Debug 会话)和 Ozone,J-Link 同一时刻只能被一个进程占用。

**Q: 报 "Failed to identify target"**

`GD32_TARGET` 写错了。可以打开 `JLinkExe`,输入 `connect` 后再选 `?` 看支持列表。

**Q: 写寄存器没生效**

`write_memory` 返回 `match: false` 时,大概率是:
- 目标外设没开时钟(看 RCU_xxxEN)
- 写到了只读位
- 寄存器有写保护(如某些 RCU 寄存器需要先解锁)

**Q: 断点下不上**

Cortex-M3/M4 通常只有 6 个硬件断点。先 `clear_all_breakpoints` 再下。

**Q: AI 误用 write_memory 把板子写挂了**

Claude Desktop 默认对每次工具调用都会弹窗确认。**对 write_memory / reset 不要勾"始终允许"**,保留确认环节。

## 下一步可以加什么

这个 server 是最小起步版。后续可以扩展:

1. **接 SVD 文件**:任意 GD32 系列按外设名+寄存器名访问,自动位域解析
2. **接 ELF 符号表**:`find_symbol("rcu_periph_clock_enable")` 自动拿地址,AI 不用人肉传地址
3. **DWT 数据观察点**:监控某个地址被谁写,自动抓改寄存器的"凶手"
4. **变量读取**:基于 DWARF 调试信息按 C 变量名读值
5. **RTT 输出抓取**:把 SEGGER RTT 的实时打印接进 AI 上下文

按需要加,不要一上来铺太大。
