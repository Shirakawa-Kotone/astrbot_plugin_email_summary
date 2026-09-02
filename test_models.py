#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量测试 OpenAI 兼容 LLM 端点上的模型可用性（针对 SiliconFlow 等平台）。

流程：
1. GET /v1/models 拉取全部模型
2. 按模型 ID 特征分类（文本聊天 / 多模态VL / 嵌入 / 重排 / 语音 / OCR / 图像 / 视频）
3. 对「可聊天」的模型逐个调用 chat/completions 做 2+2 探针测试（并发加速）
4. 输出分类结果表：✅ 可用 / ❌ 失败原因（余额不足、限流、非聊天模型等）

用法：
  python3 test_models.py --key sk-xxx                     # 默认 SiliconFlow
  python3 test_models.py --key sk-xxx --model-filter deepseek   # 只测名字含 deepseek 的
  python3 test_models.py --key sk-xxx --concurrency 6 --timeout 60
  python3 test_models.py --key sk-xxx --json-out out.json  # 保存机器可读结果
  python3 test_models.py                                  # 无 key 时自动尝试常见配置文件

注意：测试会消耗账户 token 额度；探针请求已尽量小（max_tokens=20）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------- 模型分类规则（按顺序匹配，先命中先得） ----------
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("嵌入(Embedding)", ("embedding", "embed", "bge-m3", "bce-embed", "bge-")),
    ("重排(Reranker)", ("rerank",)),
    ("语音识别(ASR)", ("asr", "sensevoice", "audio", "gsr")),
    ("语音合成(TTS)", ("tts", "cosyvoice")),
    ("OCR/文档解析", ("ocr", "paddleocr", "diagram")),
    ("视频生成", ("t2v", "i2v", "wan2", "t2i")),
    ("图像编辑", ("image-edit",)),
    ("图像生成", ("image", "flux", "stable-diffusion", "kolors", "sdxl", "dit")),
    ("多模态聊天(VL)", ("vl", "omni", "captioner")),
]
DEFAULT_CATEGORY = "文本聊天"

# 可以参与 2+2 聊天测试的分类
CHAT_TESTABLE = {"文本聊天", "多模态聊天(VL)"}

PROBE_SYSTEM = "你是测试助手，只输出最简答案。"
PROBE_USER = "2+2等于几？只回答数字"

# 常见错误 -> 简短标签
ERROR_LABELS = [
    (("balance", "insufficient", "余额"), "❌ 余额不足"),
    (("429", "rate limit", "too many requests"), "❌ 被限流(429)"),
    (("timeout", "timed out"), "❌ 超时"),
    (("connection", "connect", "refused", "unreachable"), "❌ 连接失败"),
    (("not found", "does not exist", "不存在"), "❌ 模型不存在"),
    (("invalid api key", "unauthorized", "token is invalid", "401"), "❌ Key无效"),
    (("not a chat model", "unsupported", "not support", "405", "400"), "❌ 非聊天模型"),
]


def classify(model_id: str) -> str:
    mid = model_id.lower()
    for cat, keywords in CATEGORY_RULES:
        if any(k in mid for k in keywords):
            return cat
    return DEFAULT_CATEGORY


def short_error(error: str) -> str:
    err = (error or "").lower()
    for keywords, label in ERROR_LABELS:
        if any(k in err for k in keywords):
            return label
    return f"❌ 其他:{(error or '')[:60]}"


def api_request(base_url: str, key: str, path: str, payload: dict, timeout: int):
    """发起一次 JSON POST 请求，返回 (status_code, json_dict_or_rawtext)。"""
    url = f"{base_url.rstrip('/')}/{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body
    except Exception as e:
        return 0, str(e)


def fetch_models(base_url: str, key: str, timeout: int) -> list[str]:
    url = f"{base_url.rstrip('/')}/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [m["id"] for m in data.get("data", [])]


def probe_model(model_id: str, base_url: str, key: str, timeout: int, max_tokens: int) -> str:
    """2+2 探针测试单个模型，返回可读结果。"""
    code, data = api_request(
        base_url,
        key,
        "chat/completions",
        {
            "model": model_id,
            "messages": [
                {"role": "system", "content": PROBE_SYSTEM},
                {"role": "user", "content": PROBE_USER},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        },
        timeout,
    )
    if code == 0:
        return short_error(str(data))
    if isinstance(data, dict) and "choices" in data:
        try:
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r"\s+", " ", content)[:40]
            return f"✅ {content}"
        except Exception:
            return "✅ (返回了响应但结构异常)"
    if isinstance(data, dict) and data.get("error"):
        return short_error(str(data["error"]))
    if isinstance(data, dict) and data.get("message"):
        return short_error(str(data["message"]))
    return short_error(str(data))


def try_load_key_from_configs() -> str:
    """无 --key 时尝试从常见 AstrBot 插件配置文件读取 llm_api_key。"""
    candidates = [
        Path.home() / "dev/snowluma/data/config/email_summary_assistant_config.json",
        Path.home() / "snowluma/data/config/email_summary_assistant_config.json",
        Path.home() / ".config/astrbot/config/email_summary_assistant_config.json",
    ]
    for p in candidates:
        try:
            cfg = json.loads(p.read_text(encoding="utf-8-sig"))
            if cfg.get("llm_api_key"):
                print(f"[info] 从 {p} 读取到 llm_api_key")
                return cfg["llm_api_key"]
        except Exception:
            continue
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="批量测试 OpenAI 兼容 LLM 模型可用性")
    parser.add_argument("--base-url", default="https://api.siliconflow.cn/v1",
                        help="OpenAI 兼容 Base URL（默认 SiliconFlow）")
    parser.add_argument("--key", default="", help="API Key（缺省时读环境变量 SILICONFLOW_KEY 或常见配置文件）")
    parser.add_argument("--model-filter", default="", help="只测试模型名包含该关键字的模型")
    parser.add_argument("--concurrency", type=int, default=4, help="并发测试数（默认4）")
    parser.add_argument("--timeout", type=int, default=45, help="单请求超时秒数（默认45）")
    parser.add_argument("--max-tokens", type=int, default=20, help="探针 max_tokens（默认20）")
    parser.add_argument("--json-out", default="", help="保存机器可读结果到 JSON 文件")
    args = parser.parse_args()

    key = args.key or __import__("os").environ.get("SILICONFLOW_KEY", "")
    if not key:
        key = try_load_key_from_configs()
    if not key:
        print("错误：未提供 --key，且环境变量/配置文件中也没有。", file=sys.stderr)
        return 1

    print(f"[1/3] 拉取模型列表: {args.base_url}")
    try:
        all_models = fetch_models(args.base_url, key, args.timeout)
    except Exception as e:
        print(f"错误：拉取模型列表失败: {e}", file=sys.stderr)
        return 1
    print(f"      共 {len(all_models)} 个模型")

    if args.model_filter:
        all_models = [m for m in all_models if args.model_filter.lower() in m.lower()]
        print(f"      按过滤条件 '{args.model_filter}' 剩 {len(all_models)} 个")

    # 分类
    print("[2/3] 模型分类")
    by_category: dict[str, list[str]] = {}
    for m in sorted(all_models):
        by_category.setdefault(classify(m), []).append(m)
    for cat, models in sorted(by_category.items(), key=lambda x: -len(x[1])):
        print(f"      {cat}: {len(models)} 个")

    # 逐个测试可聊天的模型
    print("[3/3] 2+2 探针测试（并发 %d）" % args.concurrency)
    testable = [m for cat in CHAT_TESTABLE for m in by_category.get(cat, [])]
    skipped = [m for cat, ms in by_category.items() if cat not in CHAT_TESTABLE for m in ms]

    results: list[dict] = []
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(probe_model, m, args.base_url, key, args.timeout, args.max_tokens): m
            for m in testable
        }
        for fut in as_completed(futures):
            model_id = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                result = short_error(str(e))
            results.append({"model": model_id, "category": classify(model_id), "result": result})
            done += 1
            print(f"  [{done}/{len(testable)}] {model_id} → {result}")
            sys.stdout.flush()

    # 汇总输出
    print("\n" + "=" * 60)
    print(f"共测试 {len(testable)} 个聊天模型，耗时 {time.time() - t0:.0f}s")
    ok = [r for r in results if r["result"].startswith("✅")]
    print(f"可用(✅): {len(ok)} 个")
    if ok:
        for r in ok:
            print(f"   {r['model']} → {r['result']}")
    else:
        print("   （一个都不可用）")
    print(f"不可用(❌): {len(results) - len(ok)} 个")
    if skipped:
        print(f"未测试(非聊天模型): {len(skipped)} 个 -> {', '.join(skipped[:10])}{'...' if len(skipped) > 10 else ''}")

    if args.json_out:
        out = {
            "base_url": args.base_url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "categories": {k: v for k, v in by_category.items()},
            "results": sorted(results, key=lambda r: (r["category"], r["model"])),
            "skipped_non_chat": skipped,
        }
        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已保存: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
