# Phrase Export Skill

只做三件事：拉取 Phrase 数据、使用成员自己的本地 Token、需求不清楚时反问。

## 使用

1. 将本仓库文件放入所用 AI 工具的 Skill 目录，文件夹命名为 `phrase-export`，按该工具的方式加载 Skill。
2. 在本地复制 `credentials.example.json` 为 `credentials.local.json`，用文本编辑器填写自己的 Phrase Platform API Token。已有个人凭证文件也可以直接向 AI 指定其路径。
3. 对 AI 说你要拉取哪个项目、什么语言和时间范围。缺少必要条件时，它会先问清楚再执行。

不需要安装器、插件注册、MCP 或固定 Python 脚本。AI 工具本身必须能访问本地文件并执行 HTTPS 请求；只有聊天、没有这些工具的环境不能实际拉取。生成 Excel 同样使用宿主已有能力，不自动安装运行环境。

## 文件

- `SKILL.md`：需求澄清、认证步骤和拉取规则。
- `credentials.example.json`：空白配置示例。
- `.gitignore`：排除个人凭证和导出内容。
- 本说明文件。

`credentials.local.json` 由每位成员在本机创建，不属于仓库。Token 不写进 SKILL.md、不发到聊天、不提交 Git。更新时保留个人配置；分享原仓库或干净的发布包，不要将带个人凭证的目录重新压缩发送。Git 忽略规则不会替 ZIP 自动排除秘密。

本地配置是明文，不是加密保险箱；本地项目范围约束也不能替代 Phrase 账号权限。仓库含内部项目名称和 UID，请保持私有。

旧版安装器和 Python 程序可从 Git 历史提交 `707a656` 恢复；旧版验证结果不代表本版在所有 AI 宿主均已实测。
