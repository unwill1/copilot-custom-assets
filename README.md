# 嵌入式AI工具-Agent在线调试编译烧录

这个仓库用于分享一套可复用的 GitHub Copilot 自定义资源，包含：

- prompts：专用于FAE的instruction 文件

- skills：编译、烧录、调试查bug等 skill，部分导入来自小智AI嵌入式AI工具

  包括Agent查用户手册的skill（需要自行转PDF为md并分类索引）

- mcp：MCP 在线调试工具，通过pylink接口实现

- mcp.template.json：脱敏后的 MCP 配置模板

## 一键安装
大模型对话中输入：
```
帮我安装 https://github.com/LeoKemp223/embed-ai-tool.git 的 skill和MCP
```
安装后还需要继续对话按需安装部分py依赖环境。

## 主要资源一览

### Instructions 和 Prompts

- prompts/FAE.instructions.md
	面向 MCU FAE 场景的主 instruction，约束代码风格、排查思路、review 方式和客户问题分析方法。
- prompts/demo_writing.prompt.md
	用于生成 demo、例程和交付型示例代码。
- prompts/explain.prompt.md
	用于讲解代码、外设流程和实现逻辑。

### Skills

当前仓库中的 skill 主要分成这几类：

- 构建类
	build-cmake、build-eide、build-keil、build-makefile
- 烧录类
	flash-eide、flash-jlink、flash-keil、flash-openocd
- 调试类
	debug-gdb-openocd、debug-jlink、rtos-debug、serial-monitor、memory-analysis、static-analysis
- 总线与协议类
	can-debug、i2c-debug、modbus-debug、visa-debug
- 知识库与支持类
	customer-debug、gd32-peripheral-lookup、peripheral-driver
- 流程编排类
	workflow

如果你是做嵌入式支持或 FAE，最常用的一般是：

- gd32-peripheral-lookup：查用户手册知识库，核对寄存器、bit 定义和配置流程
- customer-debug：按现场问题排查思路给出根因假设和验证路径
- build-keil / build-eide：编译工程并提取产物
- flash-jlink / flash-keil / flash-openocd：烧录固件
- debug-jlink / debug-gdb-openocd：在线调试、抓现场
- serial-monitor：抓串口日志
- memory-analysis：看 map / ELF 内存占用

### MCP 在线调试能力

仓库里当前自带的 MCP 是 gd32-debugger，核心用途是让 Agent 直接连上目标板做在线调试。

接好 J-Link、目标板和本机配置后，Agent 可以通过 MCP 工具完成这些动作：

- 获取目标连接状态、芯片 ID、当前 PC
- 停止 CPU、继续运行、单步、步过、复位
- 设置和清除硬件断点
- 读取全部核心寄存器或单个核心寄存器
- 读取和写入内存
- 查看外设寄存器、位域和 SVD 定义
- 配合 ELF 符号和寄存器信息定位问题代码路径

换句话说，Agent 不只是“看代码”，还可以在调试会话里直接做这些事情：

- 在关键地址下断点后等待命中
- 停住 CPU 看 PC、SP、LR、XPSR 和通用寄存器
- 读取 RCU、GPIO、USART、DMA 等外设寄存器状态
- 检查某段 RAM、外设寄存器地址或 DMA 缓冲区的内容
- 必要时写寄存器或内存做验证

这类能力很适合用来排查：

- 外设没开时钟
- 初始化顺序错误
- 中断没有进入或跑飞
- DMA 缓冲区异常
- 程序卡死、硬 fault、死循环、跑飞

如果你希望让 Agent 具备“在线调试”能力，这部分就是关键。只装 prompts 和 skills 只能增强分析和代码生成，接上 MCP 之后才真正具备断点、寄存器、内存级别的调试能力。

## 目录说明

- prompts/
	放 VS Code User prompts 目录下的 prompt 和 instruction 文件。
- skills/
	放 .copilot/skills 目录下的 skill。
- mcp/
	放 .copilot/mcp 目录下的 MCP 服务实现代码。
- mcp.template.json
	用于生成你自己本机的 MCP 配置，不要直接把模板里的占位符原样使用。

## Windows 安装路径

下面这些路径是当前这套资源对应的默认安装位置：

- prompts -> `%APPDATA%\Code\User\prompts\`
- skills -> `%USERPROFILE%\.copilot\skills\`
- mcp -> `%USERPROFILE%\.copilot\mcp\`
- mcp.template.json -> 复制后改成 `%APPDATA%\Code\User\mcp.json`

## 使用步骤

1. 下载本仓库，或者直接 Download ZIP。
2. 把 prompts、skills、mcp 分别复制到上面的本机目录。
3. 将 mcp.template.json 复制为本机使用的 mcp.json。
4. 根据你的环境修改 mcp.json 中的芯片型号、脚本路径、SVD 路径、ELF 路径等参数。
5. 重启 VS Code，或者重新加载窗口，让 Copilot 重新读取配置。

## MCP 配置说明

mcp.template.json 已经做过脱敏处理，公开仓库中不包含本机专用配置。

你至少需要根据自己的环境补这几类信息：

- MCP 启动脚本路径
- 目标芯片型号
- SVD 文件路径
- ELF 或 AXF 文件路径

如果你本机已经有自己的 mcp.json，不要直接覆盖，建议手动合并对应 server 配置。

## 注意事项

- mcp.local.json 是本机专用配置，不会提交到仓库。
- 仓库里的路径模板仅作为示例，不能保证在你的机器上直接可用。
- 如果后续本地 skill、prompt 或 MCP 有更新，执行 git pull 即可同步别人发布的新版本。
