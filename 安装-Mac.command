#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  print "需要先安装 Python 3.9+，详见 使用说明.md。"
else
  python3 ./install.py "$@"
fi
read "?按回车关闭…"
