#!/bin/zsh

set -eu

launcher_dir="${0:A:h}"
cd "$launcher_dir"

if [[ -x "$launcher_dir/.venv/bin/python" ]] && \
    "$launcher_dir/.venv/bin/python" -c \
        'import tkinter, matplotlib, serial, pyvisa' >/dev/null 2>&1; then
    exec "$launcher_dir/.venv/bin/python" -m gan_fet --simulate
fi

if command -v gan-fet >/dev/null 2>&1; then
    exec gan-fet --simulate
fi

if command -v python3 >/dev/null 2>&1 && \
    python3 -c 'import tkinter, matplotlib, serial, pyvisa' >/dev/null 2>&1; then
    exec python3 -m gan_fet --simulate
fi

echo "GaN FET dependencies are not installed in .venv or Python 3."
echo 'Install them with: python3 -m pip install -e ".[macos]"'
echo "Then double-click this launcher again."
read -r "?Press Return to close..."
exit 1
