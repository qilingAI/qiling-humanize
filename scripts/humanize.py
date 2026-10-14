#!/usr/bin/env python3
"""器灵去 AI 接口客户端；只使用 Python 标准库。"""

import argparse
from contextlib import contextmanager
import getpass
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE_URL = "https://toolsoul.daniu7.cn"
MAX_CHARS = 2000
KEY_FILE = Path.home() / ".config/qiling-humanize/credentials.json"


class ClientError(Exception):
    pass


class RemoteError(ClientError):
    def __init__(self, message, status=0, retry_after=3):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RemoteError("接口发生重定向，已停止以避免将密钥发送到其他地址。", code)


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"qk_[A-Za-z0-9_-]{16,1024}", key):
        raise ClientError("请输入完整的 qk_ API Key。密钥内容不会显示。")
    return key


def load_key():
    if os.environ.get("QILING_API_KEY"):
        return validate_key(os.environ["QILING_API_KEY"]), "environment"
    if KEY_FILE.exists():
        if os.name == "posix" and KEY_FILE.stat().st_mode & 0o077:
            raise ClientError("本地凭据文件权限过宽，请先设置为 600。")
        return validate_key(read_json(KEY_FILE).get("api_key")), "local"
    raise ClientError("尚未配置器灵 API Key。一键授权待接入；当前可在本地终端执行 configure。")


def write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".qiling-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, data):
    write_private(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def task_lock(state_path):
    path = Path(str(state_path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ClientError("另一个进程正在处理此任务，请等待它结束后继续。") from None
        else:
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ClientError("另一个进程正在处理此任务，请等待它结束后继续。") from None
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def read_json(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ClientError("配置或任务状态文件格式不正确。")
    return data


def save_key(key):
    key = validate_key(key)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(KEY_FILE, {"api_key": key})


def split_text(text, limit=MAX_CHARS):
    text = text.strip()
    if not text:
        raise ClientError("正文不能为空。")
    parts = []
    while len(text) > limit:
        cut = limit
        for separator in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", "; "):
            position = text.rfind(separator, 0, limit)
            if position >= limit // 2:
                cut = position + len(separator)
                break
        part, text = text[:cut], text[cut:]
        if not part.strip():
            raise ClientError("正文含有超过字符上限的连续空白，请先整理该空白段。")
        parts.append(part)
    parts.append(text)
    return parts


def request(method, path, key, payload=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = Request(BASE_URL + path, data=body, method=method, headers={
        "x-api-key": key, "Content-Type": "application/json",
    })
    try:
        with build_opener(NoRedirect).open(req, timeout=60) as response:
            data = json.load(response)
    except HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8"))
            detail = detail.get("detail", detail) if isinstance(detail, dict) else detail
            message = str(detail.get("code", "")) + ": " + str(detail.get("message", "")) if isinstance(detail, dict) else str(detail)
        except (ValueError, UnicodeError):
            message = "服务返回 HTTP " + str(error.code)
        try:
            retry_after = max(3, int(error.headers.get("Retry-After", "3")))
        except (ValueError, TypeError):
            retry_after = 3
        raise RemoteError(message.replace(key, "[密钥已隐藏]")[:800], error.code, retry_after) from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise RemoteError("网络中断或请求超时。") from None
    except (ValueError, UnicodeError):
        raise RemoteError("服务返回了无法读取的响应。") from None
    if not isinstance(data, dict):
        raise RemoteError("服务响应应为 JSON 对象。")
    return data


def wait_for_part(part, state, state_path, key, deadline):
    while time.monotonic() < deadline:
        try:
            result = request("GET", "/api/v1/open/tasks/" + quote(part["task_id"], safe=""), key)
        except RemoteError as error:
            if error.status not in (0, 429, 500, 502, 503, 504):
                raise
            time.sleep(min(error.retry_after, max(0, deadline - time.monotonic())))
            continue
        status = result.get("status")
        if status == "completed":
            if not isinstance(result.get("text"), str) or not result["text"].strip():
                raise ClientError("任务已完成，但响应未包含有效成稿。保留原任务 ID，核对服务结果。")
            part.update(status="completed", text=result["text"])
            write_json(state_path, state)
            return
        if status == "failed":
            code = str(result.get("error_code", "")).replace(key, "[密钥已隐藏]")
            message = str(result.get("message", "")).replace(key, "[密钥已隐藏]")
            part.update(status="failed", error_code=code, message=message)
            write_json(state_path, state)
            raise ClientError("任务失败：" + code + " " + message + "。按平台规则退费，不自动重新提交。")
        if status not in ("queued", "processing"):
            raise ClientError("收到未知任务状态。保留原任务 ID，核对服务结果。")
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    raise ClientError("等待超时。任务 ID 已保存，重新运行同一命令即可继续查询。")


def rewrite(text, output, state_path, key, wait_seconds=600):
    with task_lock(state_path):
        return rewrite_locked(text, output, state_path, key, wait_seconds)


def rewrite_locked(text, output, state_path, key, wait_seconds):
    chunks = split_text(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    output, state_path = Path(output), Path(state_path)
    if output.resolve() == state_path.resolve():
        raise ClientError("成稿路径和任务状态路径必须不同。")
    if state_path.exists():
        state = read_json(state_path)
        if state.get("input_sha256") != digest or state.get("output") != str(output.resolve()) or state.get("base_url") != BASE_URL:
            raise ClientError("任务状态不属于当前原文或输出路径，请使用原任务的输入和输出。")
        if not isinstance(state.get("parts"), list) or len(state["parts"]) != len(chunks):
            raise ClientError("任务状态文件格式不正确。")
    else:
        if output.exists():
            raise ClientError("成稿路径已有文件，请选择新的输出路径。")
        state = {"base_url": BASE_URL, "input_sha256": digest, "output": str(output.resolve()),
                 "parts": [{"status": "ready"} for _ in chunks]}
        write_json(state_path, state)
    deadline = time.monotonic() + wait_seconds
    for index, (chunk, part) in enumerate(zip(chunks, state["parts"]), 1):
        if not isinstance(part, dict):
            raise ClientError("任务分段格式不正确。")
        if part.get("status") == "completed":
            if not isinstance(part.get("text"), str) or not part["text"].strip():
                raise ClientError("已完成分段缺少成稿，请核对任务状态。")
            continue
        if part.get("status") == "failed":
            raise ClientError("分段 " + str(index) + " 的原任务失败，不自动重新提交。")
        if not part.get("task_id"):
            if part.get("status") != "ready":
                raise ClientError("分段 " + str(index) + " 的提交结果尚未明确。先在平台用量页核对，再用 attach-task 关联原任务，或在确认未创建任务后用 reset-part 恢复。")
            if time.monotonic() >= deadline:
                raise ClientError("等待超时。已完成的分段保存在状态文件里，重新运行同一命令继续。")
            # 提交前写入标记；网络超时或进程退出后不能自动再付费提交。
            part["status"] = "submitting"
            write_json(state_path, state)
            try:
                receipt = request("POST", "/api/v1/open/remove-ai", key, {"text": chunk})
            except RemoteError as error:
                if error.status in (401, 402, 403, 404, 422, 429):
                    part["status"] = "ready"
                    write_json(state_path, state)
                    raise
                raise ClientError("提交结果尚未明确。请先在平台用量页核对，不要重复提交。") from None
            task_id = receipt.get("task_id")
            if not isinstance(task_id, str) or not task_id or len(task_id) > 256:
                raise ClientError("提交响应没有有效任务 ID。请先在平台用量页核对，不要重复提交。")
            part.update(status="queued", task_id=task_id)
            for field in ("call_id", "charged", "balance"):
                if field in receipt:
                    part[field] = receipt[field]
            write_json(state_path, state)
        if not isinstance(part["task_id"], str):
            raise ClientError("任务 ID 格式不正确。")
        wait_for_part(part, state, state_path, key, deadline)
    finished = "\n\n".join(part["text"].strip() for part in state["parts"]) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != finished:
        raise ClientError("成稿路径已有不同内容，请保留该文件并从任务状态另存结果。")
    write_private(output, finished)
    return {"status": "completed", "output": str(output.resolve()), "state": str(state_path.resolve()),
            "calls": len(chunks), "receipts": [{field: part[field] for field in ("task_id", "charged", "balance") if field in part} for part in state["parts"]]}


def repair_part(state_path, number, task_id=None, confirmed=False):
    with task_lock(state_path):
        return repair_part_locked(state_path, number, task_id, confirmed)


def repair_part_locked(state_path, number, task_id, confirmed):
    state = read_json(state_path)
    parts = state.get("parts", [])
    if not isinstance(parts, list) or number < 1 or number > len(parts):
        raise ClientError("分段序号不正确。")
    part = parts[number - 1]
    if not isinstance(part, dict) or part.get("status") != "submitting" or part.get("task_id"):
        raise ClientError("只能处理尚未获得任务 ID 的待核对提交。")
    if task_id:
        if len(task_id) > 256:
            raise ClientError("任务 ID 格式不正确。")
        part.update(status="queued", task_id=task_id)
    elif confirmed:
        part["status"] = "ready"
    else:
        raise ClientError("必须先核对平台记录，确认没有创建任务。")
    write_json(state_path, state)


def main():
    parser = argparse.ArgumentParser(description="器灵文章去 AI 味；一键授权待接入，当前支持本地密钥配置。")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("key-status", help="检查是否配置，不输出密钥")
    commands.add_parser("configure", help="在本地交互终端私下输入 API Key")
    for name in ("plan", "rewrite"):
        sub = commands.add_parser(name)
        sub.add_argument("--input", required=True, help="UTF-8 文本文件；- 表示标准输入")
        if name == "rewrite":
            sub.add_argument("--output", required=True)
            sub.add_argument("--state", help="默认在成稿旁保存 .qiling-state.json")
            sub.add_argument("--wait-seconds", type=int, default=600)
    for name in ("attach-task", "reset-part"):
        sub = commands.add_parser(name)
        sub.add_argument("--state", required=True)
        sub.add_argument("--part", type=int, required=True, help="从 1 开始")
        if name == "attach-task":
            sub.add_argument("--task-id", required=True)
        else:
            sub.add_argument("--confirmed-not-created", action="store_true", help="已核对平台记录且确认没有创建任务")
    args = parser.parse_args()
    active_key = None
    try:
        if args.command == "key-status":
            if not os.environ.get("QILING_API_KEY") and not KEY_FILE.exists():
                result = {"configured": False, "one_click_authorization": "pending"}
            else:
                _, source = load_key()
                result = {"configured": True, "source": source, "one_click_authorization": "pending"}
        elif args.command == "configure":
            if not sys.stdin.isatty():
                raise ClientError("请在本地交互终端运行 configure；不要通过聊天或命令参数传递密钥。")
            save_key(getpass.getpass("器灵 API Key（输入隐藏）：").strip())
            result = {"configured": True, "source": "local"}
        elif args.command in ("attach-task", "reset-part"):
            repair_part(args.state, args.part, getattr(args, "task_id", None), getattr(args, "confirmed_not_created", False))
            result = {"status": "ready_to_resume", "state": args.state, "part": args.part}
        else:
            text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8-sig")
            parts = split_text(text)
            if args.command == "plan":
                result = {"characters": len(text.strip()), "calls": len(parts), "part_characters": [len(part) for part in parts], "max_input_characters": MAX_CHARS}
            else:
                if args.wait_seconds <= 0:
                    raise ClientError("等待时间必须大于 0。")
                output = Path(args.output)
                state_path = Path(args.state) if args.state else Path(str(output) + ".qiling-state.json")
                if args.input != "-" and Path(args.input).resolve() in (output.resolve(), state_path.resolve()):
                    raise ClientError("原文、成稿、状态文件的路径必须分开。")
                active_key, _ = load_key()
                result = rewrite(text, output, state_path, active_key, args.wait_seconds)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ClientError, OSError, ValueError) as error:
        message = str(error)
        key = active_key or os.environ.get("QILING_API_KEY")
        if key:
            message = message.replace(key, "[密钥已隐藏]")
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
