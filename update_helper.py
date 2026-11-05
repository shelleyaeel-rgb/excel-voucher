"""Standalone onefile updater; its trust configuration is embedded at build time."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import sys

from xiaoyu.update_installer import (
    AppLock, UpdateError, _read_json, _state_dir, _write_json, install_candidate,
    recover_pending, restart_application, wait_for_parent,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="给小雨的软件：更新和恢复")
    parser.add_argument("--app-dir", type=Path)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--data-path", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args(argv)
    try:
        # Inherited cwd can keep the old directory busy on Windows even after
        # the parent app exits. The copied helper always works from outside it.
        runner = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        os.chdir(runner)
        if args.app_dir is None:
            request = _read_json(runner / "request.json", 65536)
            if request.get("schema") != 1 or not isinstance(request.get("app_dir"), str):
                raise UpdateError("更新恢复入口缺少程序目录信息。")
            args.app_dir = Path(request["app_dir"])
            args.restart = request.get("restart") is True
            args.recover = True
        if args.parent_pid is not None:
            wait_for_parent(args.parent_pid)
        with AppLock(args.app_dir):
            recover_pending(args.app_dir)
            if not args.recover:
                if args.candidate_dir is None or args.data_path is None:
                    raise UpdateError("缺少更新包或资料目录。")
                result = install_candidate(args.candidate_dir, args.app_dir, args.data_path)
                _write_json(_state_dir(args.app_dir) / "last-result.json", result)
        if args.restart:
            restart_application(args.app_dir)
        return 0
    except Exception as exc:
        message = f"更新未完成：{exc}"
        try:
            if args.app_dir is not None:
                state = _state_dir(args.app_dir)
                message += f"\n\n备份和恢复程序位于：\n{state}"
                _write_json(state / "last-error.json", {"error": str(exc)})
        except (OSError, ValueError):
            pass
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, "给小雨的软件 · 更新", 0x10)
        else:
            print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
