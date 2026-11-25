"""Loopback-only, read-only evidence dashboard. No execution or publication API."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit
import uuid


def validate_roots(root: Path, data_dirs: list[Path] | None = None) -> tuple[Path, list[Path] | None]:
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("项目根目录必须是已存在的目录")
    if data_dirs is None:
        return root, None
    checked = []
    for item in data_dirs:
        original = Path(item) if Path(item).is_absolute() else root / item
        resolved = original.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_dir():
            raise ValueError("数据目录必须位于项目根目录内")
        relative = original.relative_to(root)
        if any((root.joinpath(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
            raise ValueError("不接受符号链接数据目录")
        checked.append(resolved)
    return root, checked


def make_server(root: Path, port: int = 8765, data_dirs: list[Path] | None = None) -> ThreadingHTTPServer:
    root, data_dirs = validate_roots(root, data_dirs)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("端口必须是 0–65535 的整数")
    page = files("skillforge").joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "SkillForgeDashboard/1"

        def log_message(self, format: str, *args: object) -> None:
            # A local read-only viewer does not need to log task text or URL parameters.
            return

        def reply(self, code: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def local_request(self) -> bool:
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host", "").lower() not in hosts:
                self.reply(403, b"Local Host required")
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in hosts}:
                self.reply(403, b"Same-origin requests only")
                return False
            return True

        def do_GET(self) -> None:
            if not self.local_request():
                return
            path = urlsplit(self.path).path
            if path == "/":
                self.reply(200, page, "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                self.reply(204)
            elif path == "/api/snapshot":
                try:
                    from .dashboard_data import build_snapshot

                    snapshot = build_snapshot(root, data_dirs=data_dirs)
                    body = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except Exception:
                    self.reply(500, b"Unable to read evidence snapshot")
                    return
                self.reply(200, body, "application/json; charset=utf-8")
            else:
                self.reply(404, b"Not found")

        def reject_write(self) -> None:
            if self.local_request():
                self.reply(405, b"Read-only dashboard")

        do_POST = reject_write
        do_PUT = reject_write
        do_PATCH = reject_write
        do_DELETE = reject_write
        do_OPTIONS = reject_write
        do_HEAD = reject_write

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


OFFLINE_CSP_META = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'none\'; img-src data:; base-uri \'none\'; form-action \'none\'">'


def escape_json_for_html_script(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False)
    # Prevent HTML script breakout: </script>, <!--, etc.
    # In standard JSON string literals, \u003c is <, \u003e is >, \u0026 is &, \u2028 and \u2029 are line separators.
    return (
        raw.replace("&", r"\u0026")
        .replace("<", r"\u003c")
        .replace(">", r"\u003e")
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )


def render_offline_html(snapshot: dict) -> str:
    template = files("skillforge").joinpath("dashboard.html").read_text(encoding="utf-8")
    escaped_json = escape_json_for_html_script(snapshot)

    head_tag = "<head>\n"
    if head_tag in template:
        rendered = template.replace(head_tag, f"{head_tag}{OFFLINE_CSP_META}\n", 1)
    else:
        rendered = template.replace("<head>", f"<head>\n{OFFLINE_CSP_META}\n", 1)

    placeholder = '<script id="skillforge-snapshot" type="application/json"></script>'
    if placeholder not in rendered:
        raise ValueError("dashboard.html 缺少快照挂载点")

    embedded_tag = f'<script id="skillforge-snapshot" type="application/json">{escaped_json}</script>'
    return rendered.replace(placeholder, embedded_tag, 1)


def validate_export_path(root: Path, output_path: Path | str) -> Path:
    if not output_path:
        raise ValueError("必须指定导出 HTML 路径")
    raw = Path(output_path)
    if raw.suffix.lower() != ".html":
        raise ValueError("导出路径必须是 .html 结尾的文件")

    home = Path.home().resolve()
    target = raw if raw.is_absolute() else (root / raw)

    if target.is_symlink():
        raise ValueError("不接受目标为符号链接")
    if target.exists():
        raise ValueError("目标文件已存在，拒绝覆盖")

    try:
        resolved = target.resolve()
    except Exception as exc:
        raise ValueError(f"无法解析导出路径: {exc}")

    if resolved == home or resolved.parent == home:
        raise ValueError("拒绝在用户 HOME 根目录下输出 HTML 文件")
    if root.resolve() == home:
        raise ValueError("项目根目录不能是用户 HOME 根目录")

    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("导出路径必须位于项目根目录内")

    # Inspect lexical ancestors before resolution hides aliases in existing subdirectories.
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise ValueError("导出路径必须位于项目根目录内") from None
    if ".." in relative.parts:
        raise ValueError("导出路径不能包含父目录跳转")
    for i in range(1, len(relative.parts) + 1):
        if root.joinpath(*relative.parts[:i]).is_symlink():
            raise ValueError("导出路径中的目录不能是符号链接")
    return resolved


def export_html(root: Path, output_path: Path | str, data_dirs: list[Path] | None = None) -> Path:
    root, data_dirs = validate_roots(root, data_dirs)
    target = validate_export_path(root, output_path)

    from .dashboard_data import build_snapshot

    snapshot = build_snapshot(root, data_dirs=data_dirs)
    html_content = render_offline_html(snapshot)

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        raise ValueError(f"目标文件已存在，拒绝覆盖: {target}")

    temp_file = target.parent / f".tmp_{target.stem}_{os.getpid()}_{uuid.uuid4().hex}.html.tmp"
    try:
        temp_file.write_text(html_content, encoding="utf-8")
        try:
            os.link(temp_file, target)
        except FileExistsError:
            raise ValueError(f"目标文件已存在，拒绝覆盖: {target}")
    finally:
        if temp_file.exists() or temp_file.is_symlink():
            try:
                temp_file.unlink()
            except OSError:
                pass

    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SkillForge 本地只读证据看板")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    parser.add_argument("--port", type=int, default=8765, help="本地端口，默认 8765")
    parser.add_argument("--data-dir", type=Path, action="append", help="项目内数据目录，可重复指定")
    parser.add_argument("--export-html", type=Path, default=None, help="导出离线单 HTML 快照到指定项目内路径")
    args = parser.parse_args(argv)

    if args.export_html is not None:
        try:
            out_file = export_html(args.root, args.export_html, args.data_dir)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f"SkillForge 离线证据看板已导出: {out_file}", flush=True)
        print("提示：已完成只读投影与已知敏感字段脱敏；分享或归档前请人工审阅业务内容。", flush=True)
        return 0

    try:
        server = make_server(args.root, args.port, args.data_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"SkillForge 证据看板: http://127.0.0.1:{server.server_port}", flush=True)
    print("本地只读 · 点击刷新读取新记录 · 不触发模型调用或技能发布 · Ctrl+C 退出", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
