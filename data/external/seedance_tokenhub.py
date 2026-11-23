#!/usr/bin/env python3
"""通过智云 TokenHub 调用 Seedance 2.0，轮询任务并下载 MP4。

仅使用 Python 标准库。接口路径来自智云 TokenHub 模型详情页；
请求参数采用 Seedance 2.0 常用的 content[] 格式。

用法：
    python seedance_tokenhub.py
    python seedance_tokenhub.py --prompt "一只小猫在窗台上打哈欠"
    python seedance_tokenhub.py --task-id <之前获得的任务ID>
    python seedance_tokenhub.py --image-url https://example.com/photo.png
"""

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


MODEL = "doubao-seedance-2-0-260128"
CREATE_URL = "https://aigw.telecomjs.com/v1/videos/generations"
TASK_URL = "https://aigw.telecomjs.com/v1/videos/generations/task/{}"
DEFAULT_PROMPT = (
    "电影感画面，一辆未来科技跑车缓缓驶过雨夜街道，"
    "霓虹灯在车身反射，镜头平稳跟随，真实的光影细节。"
)
FINISHED = {"succeeded", "success", "completed", "complete"}
FAILED = {"failed", "error", "cancelled", "canceled", "expired"}


class APIError(RuntimeError):
    """请求失败；不自动重复提交可能计费的 POST。"""


def request_json(method, url, api_key, payload=None):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = exc.read(3000).decode("utf-8", errors="replace")
        raise APIError(f"HTTP {exc.code}：{body}") from exc
    except (URLError, TimeoutError) as exc:
        raise APIError(f"网络错误或超时：{exc}") from exc

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise APIError(f"服务器返回的不是 JSON：{raw[:400]}") from exc
    if not isinstance(result, dict):
        raise APIError(f"服务器返回的数据结构异常：{str(result)[:400]}")
    return result


def nested_dicts(data):
    """兼容供应商将结果放入 data/result/output 中的情况。"""
    if not isinstance(data, dict):
        return
    yield data
    for key in ("data", "result", "output", "task"):
        value = data.get(key)
        if isinstance(value, dict):
            yield from nested_dicts(value)


def find_task_id(data):
    for item in nested_dicts(data):
        for key in ("taskId", "task_id", "id"):
            value = item.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value)
    return None


def find_status(data):
    for item in nested_dicts(data):
        for key in ("status", "state", "task_status"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return "unknown"


def find_video_url(data):
    """兼容 content.video_url、content[0].video_url、顶层 video_url。"""
    if isinstance(data, dict):
        for key in ("video_url", "videoUrl"):
            candidate = data.get(key)
            if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
                return candidate
            if isinstance(candidate, dict):
                url = candidate.get("url")
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    return url
        for value in data.values():
            found = find_video_url(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_video_url(value)
            if found:
                return found
    return None


def download_video(url, destination):
    """视频地址通常是限时链接；不向该地址发送 TokenHub API Key。"""
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = path.with_name(path.name + ".part")
    req = Request(url, headers={"User-Agent": "Seedance-TokenHub-Python/1.0"})
    try:
        with urlopen(req, timeout=120) as response, open(partial_path, "wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        partial_path.replace(path)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise APIError(f"视频已生成，但下载失败：{exc}") from exc
    return path


def parse_args():
    parser = argparse.ArgumentParser(description="智云 TokenHub / Seedance 2.0 视频生成")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="文生视频提示词")
    parser.add_argument("--image-url", help="可公开访问的图片直链，用作首帧（可选）")
    parser.add_argument("--duration", type=int, default=5, help="视频秒数，4~15，默认 5")
    parser.add_argument("--resolution", choices=("480p", "720p", "1080p", "4k"), default="480p")
    parser.add_argument("--ratio", choices=("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"), default="16:9")
    parser.add_argument("--output", default="seedance_output.mp4", help="保存的 MP4 路径")
    parser.add_argument("--task-id", help="只查询已有任务，不重新发起生成/扣费")
    parser.add_argument("--poll-interval", type=int, default=10, help="轮询间隔秒数，默认 10")
    parser.add_argument("--max-wait", type=int, default=900, help="最长等待秒数，默认 900")
    parser.add_argument("--dry-run", action="store_true", help="仅查看请求 JSON，不发送、不扣费")
    return parser.parse_args()


def main():
    args = parse_args()
    if not 4 <= args.duration <= 15:
        raise ValueError("--duration 必须是 4~15 秒")
    if args.poll_interval < 1 or args.max_wait < 1:
        raise ValueError("轮询间隔和最长等待时间必须大于 0")

    content = [{"type": "text", "text": args.prompt}]
    if args.image_url:
        if not args.image_url.startswith(("https://", "http://")):
            raise ValueError("--image-url 必须是模型服务器可访问的 HTTP(S) 图片直链")
        content.append({
            "type": "image_url",
            "image_url": {"url": args.image_url},
            "role": "first_frame",
        })

    payload = {
        "model": MODEL,
        "content": content,
        "duration": args.duration,
        "resolution": args.resolution,
        "ratio": args.ratio,
        "generate_audio": False,
    }

    if args.dry_run:
        print(f"POST {CREATE_URL}")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print("\n仅预览，未请求 API，也不会扣费。")
        return 0

    api_key = os.environ.get("TOKENHUB_API_KEY", "").strip()
    if not api_key:
        api_key = getpass.getpass("请输入智云 TokenHub API Key（输入时不显示）：").strip()
    if not api_key:
        raise ValueError("未输入 API Key")

    task_id = args.task_id
    if not task_id:
        print(f"即将生成 {args.duration} 秒、{args.resolution} 的 Seedance 2.0 视频。")
        consent = input("这可能消耗比赛额度。确认提交请输入 y：").strip().lower()
        if consent != "y":
            print("已取消，未发送请求。")
            return 0

        print("提交视频生成任务中……")
        try:
            created = request_json("POST", CREATE_URL, api_key, payload)
        except APIError:
            print("注意：若发生超时，任务可能已创建。请先检查平台调用记录，切勿直接重复提交。")
            raise

        task_id = find_task_id(created)
        if not task_id:
            print("服务器已回应，但未识别到任务 ID；不要直接重试：")
            print(json.dumps(created, indent=2, ensure_ascii=False)[:4000])
            return 1

        task_record = Path(args.output).with_suffix(".task.txt")
        task_record.write_text(task_id + "\n", encoding="utf-8")
        print(f"提交成功！任务 ID：{task_id}")
        print(f"任务 ID 已保存：{task_record.resolve()}")
    else:
        print(f"继续查询已有任务：{task_id}（不会创建新任务）")

    url = TASK_URL.format(quote(task_id, safe=""))
    deadline = time.monotonic() + args.max_wait
    while True:
        task = request_json("GET", url, api_key)
        status = find_status(task)
        print(f"当前状态：{status}")

        if status in FINISHED:
            video_url = find_video_url(task)
            if not video_url:
                print("任务显示成功，但没有找到 video_url，完整响应如下：")
                print(json.dumps(task, indent=2, ensure_ascii=False)[:5000])
                return 1
            print("视频生成成功，正在下载……")
            try:
                path = download_video(video_url, args.output)
            except APIError:
                print(f"生成的视频链接：{video_url}")
                raise
            print(f"MP4 已保存：{path}")
            return 0

        if status in FAILED:
            print("生成任务失败，服务器信息：")
            print(json.dumps(task, indent=2, ensure_ascii=False)[:5000])
            return 1

        if time.monotonic() >= deadline:
            print("等待超时，但任务可能仍在运行。")
            print(f"稍后执行：python seedance_tokenhub.py --task-id {task_id}")
            return 2
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (APIError, ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        sys.exit(1)
