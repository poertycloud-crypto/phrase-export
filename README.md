# Phrase Export

内部使用的 Phrase TMS 本地导出工具：Skill + Python 脚本，可选 Codex 插件注册。无需部署服务器，每位成员使用自己的 Phrase Platform API Token。

## 安装

1. 下载并完整解压本仓库，或通过 Git 克隆到本地。
2. 安装 Python 3.9+，运行 `安装-Mac.command` 或 `安装-Windows.cmd`。也可在仓库目录执行 `python3 install.py`（Windows 为 `py -3 install.py`）。
3. 在安装器提示的个人 `credentials.json` 中填写自己的 Token，然后在 AI 工具中新建对话使用 `phrase-export`。

默认安装为独立 Skill；需要在 Codex 插件面板注册时，参照[使用说明](使用说明.md)选择 `--mode codex`。其他 AI 工具的 Skill 发现目录可能不同。

## 内容

- 白名单项目查询、导出范围预览与确认。
- Job 清单，或单一合并 XLIFF 和三列 Excel/CSV。
- Platform Token Exchange、分页、重试、分段完成数独立核对。
- [详细安装与安全说明](使用说明.md) · [首版验证记录](验证记录.md)

## 凭证与权限

真实 Token 不属于仓库内容。只编辑个人配置文件，不要编辑 SKILL.md，不要把 Token 发到对话、提交 Git 或上传到 Issues。仓库只包含空白凭证示例，成员的实际导出数据也不得提交。

个人配置为本地明文文件；本地白名单和对话确认不是强制权限隔离，真正的项目权限由 Phrase 账号控制。此仓库包含内部项目名称和 UID，请保持私有并仅授权需要使用的成员。

## 测试

```text
python3 -m unittest discover -s tests -v
```

测试不需要真实 Token。Windows 将 `python3` 替换为 `py -3`。
