#!/usr/bin/env python3
"""Install the portable skill, or explicitly register the Codex plugin."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "plugins/phrase-export/skills/phrase-export"


def default_config():
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "PhraseExport"
    return Path.home() / ".config/phrase-export"


def initialize_config(folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / "credentials.json"
    if target.is_symlink():
        raise RuntimeError("凭证路径为符号链接，请先检查；安装器不会写入。")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return target, False
    with os.fdopen(fd, "wb") as f:
        f.write((ROOT / "credentials.example.json").read_bytes())
    return target, True


def install_skill(destination, replace=False, backup_root=None):
    if destination.is_symlink():
        raise RuntimeError("Skill 目标为符号链接；未覆盖。")
    backup = None
    if destination.exists():
        if not replace:
            raise RuntimeError("Skill 已存在。更新请加 --replace；旧版本会备份，个人凭证不受影响。")
        backup_root = Path(backup_root) if backup_root else default_config() / "skill-backups"
        if backup_root == destination or destination in backup_root.parents:
            raise RuntimeError("备份目录不能在 Skill 内。")
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / (destination.name + "-" + str(time.time_ns()))
        destination.rename(backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(SKILL, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    except Exception:
        # Preserve backup even if copying fails; do not delete broad user paths.
        raise RuntimeError("安装复制失败；旧版本备份已保留在个人配置目录的 skill-backups 中。") from None
    return backup


def find_codex():
    found = shutil.which("codex")
    if found:
        return found
    for name in ("Codex", "ChatGPT"):
        candidate = Path("/Applications") / (name + ".app/Contents/Resources/codex")
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("找不到 Codex CLI。可先使用默认的 Skill 安装模式。")


def codex_run(executable, *args):
    result = subprocess.run([executable, "plugin", *args, "--json"], capture_output=True, text=True, encoding="utf-8", timeout=90)
    if result.returncode:
        raise RuntimeError("Codex 插件命令失败。配置和已复制文件保留；请核对 CLI 版本和权限。")
    return json.loads(result.stdout)


def install_codex():
    executable = find_codex()
    manifest = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    name = manifest["name"]
    # Never replace an unrelated marketplace that happens to have the same name.
    local = Path.home() / ".agents/plugins/marketplace.json"
    if local.exists() and json.loads(local.read_text(encoding="utf-8")).get("name") == name:
        raise RuntimeError("已有同名个人 marketplace；未修改。请使用默认 Skill 安装模式。")
    entries = codex_run(executable, "marketplace", "list").get("marketplaces", [])
    if any(entry.get("name") == name and Path(entry.get("root", "")).resolve() != ROOT for entry in entries):
        raise RuntimeError("另一个目录已注册同名 marketplace；未覆盖。请使用默认 Skill 安装模式。")
    codex_run(executable, "marketplace", "add", str(ROOT))
    codex_run(executable, "add", "phrase-export@" + name)
    return ROOT


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Phrase Export 安装器；不获取或输出 Token。")
    parser.add_argument("--mode", choices=("skill", "codex"), default="skill")
    parser.add_argument("--skill-dir", type=Path, default=Path.home() / ".agents/skills/phrase-export")
    parser.add_argument("--config-dir", type=Path, default=default_config())
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 9):
            raise RuntimeError("需要 Python 3.9 或更新版本。")
        if args.mode == "codex":
            destination = install_codex()
            backup = None
        else:
            if args.skill_dir.is_symlink():
                raise RuntimeError("Skill 目标为符号链接；未覆盖。")
            destination = args.skill_dir.resolve()
            # Credentials stay outside the code tree, including custom-path testing.
            if args.config_dir.resolve() == destination or destination in args.config_dir.resolve().parents:
                raise RuntimeError("个人配置目录不能放在 Skill 内。")
            if destination == Path.home() or destination == ROOT or destination == Path(destination.anchor):
                raise RuntimeError("Skill 目标目录过宽，已拒绝。")
            backup = install_skill(destination, args.replace, args.config_dir.resolve() / "skill-backups")
        credential, created = initialize_config(args.config_dir)
        print(json.dumps({"installed": True, "mode": args.mode, "location": str(destination), "backup": str(backup) if backup else None,
            "credentials_file": str(credential.resolve()), "credentials_created": created,
            "next": "用本地编辑器填写个人凭证（已有文件不覆盖），然后在 AI 工具新建对话使用 phrase-export。"}, ensure_ascii=False, indent=2))
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(json.dumps({"installed": False, "error": str(e)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
