"""中心端一键自更新。

设计目标：**数据与程序分离** —— 更新只替换可执行文件，绝不动数据库、storage、
backups、.env 等数据文件；新程序启动后继续读取同一数据目录。

流程：
  1. 查询 GitHub 最新 Release；
  2. 下载当前平台对应的中心端二进制；
  3. 校验 SHA-256；
  4. 把新文件写入 ``<exe>.next``，生成一个脱离当前进程的交换脚本：
     等待数秒（保证 API 响应已返回）→ 停止旧进程 → 备份旧文件为 ``.prev`` →
     原子替换 → 用原参数重新启动。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

REPO = "CHENZlHAO/knowledge-flywheel"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

_update_state: dict = {"status": "idle", "message": "", "latest_version": ""}


def _request_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "knowledge-center-updater"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def current_version() -> str:
    from .config import settings

    return getattr(settings, "app_version", "dev") or "dev"


def platform_asset_name() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "knowledge-center.exe"
    if system == "darwin":
        return "knowledge-center-darwin-arm64" if machine in ("arm64", "aarch64") else "knowledge-center-darwin-amd64"
    return None  # Linux 中心端未发布二进制；Docker 部署请拉镜像


def check_update() -> dict:
    try:
        data = _request_json(LATEST_URL)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"无法连接 GitHub Release：{exc}"}

    latest = data.get("tag_name", "")
    assets = {a.get("name"): a for a in data.get("assets", [])}
    want = platform_asset_name()
    asset = assets.get(want) if want else None
    digest = (asset.get("digest") or "").replace("sha256:", "") if asset else None
    return {
        "ok": True,
        "repo": REPO,
        "current_version": current_version(),
        "latest_version": latest,
        "published_at": data.get("published_at"),
        "platform_asset": want,
        "asset_url": asset.get("browser_download_url") if asset else None,
        "sha256": digest or None,
        "update_available": bool(asset and latest and latest != current_version()),
    }


def _executable() -> Path:
    return Path(sys.executable).resolve()


def _spawn_detached(script: Path) -> None:
    if platform.system().lower() == "windows":
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000008,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            ["sh", str(script)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _write_swap_script(old: Path, new: Path, pid: int, restart_args: list[str]) -> Path:
    script = Path(tempfile.gettempdir()) / f"knowledge-center-update-{os.getpid()}.{'ps1' if platform.system().lower() == 'windows' else 'sh'}"
    if platform.system().lower() == "windows":
        args = " ".join(f"'{a}'" for a in restart_args)
        script.write_text(
            "\n".join(
                [
                    f"$pidToStop = {pid}",
                    f"$old = '{old}'",
                    f"$new = '{new}'",
                    f"$startArgs = \"{args}\"",
                    "Start-Sleep -Seconds 3",
                    "if (Get-Process -Id $pidToStop -ErrorAction SilentlyContinue) { Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue }",
                    "Start-Sleep -Seconds 1",
                    "if (Test-Path \"$old.prev\") { Remove-Item \"$old.prev\" -Force }",
                    "if (Test-Path $old) { Move-Item $old \"$old.prev\" -Force }",
                    "Move-Item $new $old -Force",
                    "if ($startArgs) { Start-Process -FilePath $old -ArgumentList $startArgs } else { Start-Process -FilePath $old }",
                ],
                encoding="utf-8",
            ),
        )
    else:
        args = " ".join(f"'{a}'" for a in restart_args)
        script.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    "set -eu",
                    f"OLD='{old}'",
                    f"NEW='{new}'",
                    f"PID={pid}",
                    f"ARGS={args}",
                    "sleep 2",
                    f"kill $PID 2>/dev/null || true",
                    "sleep 1",
                    'if [ -f "$OLD.prev" ]; then rm -f "$OLD.prev"; fi',
                    'if [ -f "$OLD" ]; then mv "$OLD" "$OLD.prev" 2>/dev/null || true; fi',
                    'mv "$NEW" "$OLD"',
                    'chmod +x "$OLD"',
                    'exec "$OLD" $ARGS',
                ],
                encoding="utf-8",
            ),
        )
        script.chmod(0o755)
    return script


def perform_update() -> dict:
    global _update_state
    _update_state = {"status": "running", "message": "检查最新版本…", "latest_version": ""}
    try:
        info = check_update()
        if not info.get("ok"):
            _update_state = {"status": "failed", "message": info.get("error", "检查失败"), "latest_version": ""}
            return {"ok": False, "error": _update_state["message"]}
        if not info.get("update_available"):
            _update_state = {"status": "up_to_date", "message": "已是最新版本", "latest_version": info["latest_version"]}
            return {"ok": True, "message": _update_state["message"]}
        if not info.get("asset_url"):
            _update_state = {"status": "failed", "message": "当前平台没有可下载的中心端二进制", "latest_version": info["latest_version"]}
            return {"ok": False, "error": _update_state["message"]}

        old = _executable()
        new = old.with_name(old.name + ".next")
        _update_state = {"status": "running", "message": f"下载 {info['latest_version']} 中…", "latest_version": info["latest_version"]}
        urllib.request.urlretrieve(info["asset_url"], new)  # noqa: S310

        if info.get("sha256"):
            digest = hashlib.sha256(new.read_bytes()).hexdigest()
            if digest != info["sha256"]:
                new.unlink(missing_ok=True)
                _update_state = {"status": "failed", "message": "SHA-256 校验失败，已中止", "latest_version": info["latest_version"]}
                return {"ok": False, "error": _update_state["message"]}

        restart_args = sys.argv[1:]
        script = _write_swap_script(old, new, os.getpid(), restart_args)
        _update_state = {"status": "restarting", "message": "已下载并校验，即将重启…", "latest_version": info["latest_version"]}
        _spawn_detached(script)
        return {"ok": True, "message": _update_state["message"], "latest_version": info["latest_version"]}
    except Exception as exc:  # noqa: BLE001
        _update_state = {"status": "failed", "message": f"更新失败：{exc}", "latest_version": _update_state.get("latest_version", "")}
        return {"ok": False, "error": _update_state["message"]}


def start_update_async() -> dict:
    thread = threading.Thread(target=perform_update, daemon=True)
    thread.start()
    return {"started": True, "message": "更新任务已在后台启动，请稍候刷新查看状态"}


def update_status() -> dict:
    return dict(_update_state)
