# Copilot 自定义资源

这个仓库用于分享一套可复用的 GitHub Copilot 自定义资源，包含：

- prompts：自定义 prompt 和 instruction 文件
- skills：自定义 skill
- mcp：MCP 服务代码
- copilot-instructions.md：补充指令文件
- mcp.template.json：脱敏后的 MCP 配置模板

## 目录说明

- prompts/
	放 VS Code User prompts 目录下的 prompt 和 instruction 文件。
- skills/
	放 .copilot/skills 目录下的 skill。
- mcp/
	放 .copilot/mcp 目录下的 MCP 服务实现代码。
- copilot-instructions.md
	可放到 .vscode 目录作为补充指令文件。
- mcp.template.json
	用于生成你自己本机的 MCP 配置，不要直接把模板里的占位符原样使用。

## Windows 安装路径

下面这些路径是当前这套资源对应的默认安装位置：

- prompts -> %APPDATA%\Code\User\prompts\
- skills -> %USERPROFILE%\.copilot\skills\
- mcp -> %USERPROFILE%\.copilot\mcp\
- copilot-instructions.md -> %USERPROFILE%\.vscode\copilot-instructions.md
- mcp.template.json -> 复制后改成 %APPDATA%\Code\User\mcp.json

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
