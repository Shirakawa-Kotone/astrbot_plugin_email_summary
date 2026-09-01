#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建/校验 AstrBot 插件发布压缩包。

背景
----
AstrBot（v3/v4）通过上传 zip 安装插件时，会先调用
`astrbot.core.star.updater.PluginUpdater.inspect_plugin_archive` 校验压缩包：
1. 通过 `_resolve_archive_root_dir` 求压缩包内所有条目的公共根目录；
2. 在公共根目录下查找 metadata.yaml / metadata.yml；
3. 读取并校验必需字段（name/desc/version/author），且 name 必须是合法 Python 标识符。

常见失败原因：
- 用 macOS 访达「压缩」生成的 zip 会带 __MACOSX/ 目录，导致公共根目录解析为空，
  从而报错「压缩包不是合法的 AstrBot 插件：未找到 metadata.yaml 或 metadata.yml」；
- metadata.yaml 的 name 含连字符（如 email-summary-assistant）不是合法 Python 标识符，
  安装后加载插件会报「name 不是合法的模块名称」。

本脚本生成满足要求的 zip（单一顶层目录、无 macOS 垃圾文件），并用与 AstrBot 相同的
算法自校验，确保打出来的包一定能被 AstrBot 识别。

用法
----
python build_release.py                 # 构建 dist/astrbot_plugin_email_summary.zip 并自校验
python build_release.py 任意.zip        # 只校验指定 zip（不重新构建）
"""

from __future__ import annotations

import keyword
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_DIR_NAME = "astrbot_plugin_email_summary"  # 压缩包内单一顶层目录名
OUT_DIR = ROOT / "dist"
OUT_ZIP = OUT_DIR / f"{PLUGIN_DIR_NAME}.zip"

# 与 AstrBot v4 astrbot/core/star/updater.py 保持一致
PLUGIN_METADATA_FILENAMES = ("metadata.yaml", "metadata.yml")
PLUGIN_METADATA_REQUIRED_FIELDS = ("name", "desc", "version", "author")

# 需要打包进插件目录的文件（相对仓库根目录）
INCLUDE_FILES = (
    "metadata.yaml",
    "main.py",
    "_conf_schema.json",
    "README.md",
    "requirements.txt",
    "core/__init__.py",
    "core/imap_fetcher.py",
    "core/email_analyzer.py",
    "core/summary_reporter.py",
    "pages/dashboard/index.html",
    "pages/dashboard/app.js",
    "pages/dashboard/style.css",
    ".astrbot-plugin/i18n/zh-CN.json",
)

# 打包时排除（防御性，防止 __pycache__/.DS_Store/.git 等混入）
EXCLUDE_PATTERNS = (
    "__pycache__",
    "*.pyc",
    ".DS_Store",
    "__MACOSX",
    ".git",
    ".github",
    "dist",
    "data",
    "*.zip",
    "build_release.py",
)


# ---------- 以下为 AstrBot 校验逻辑的忠实复刻（stdlib 实现，不依赖 yaml） ----------

def _resolve_archive_root_dir(entries: list[str]) -> str:
    """复刻 AstrBot v4 astrbot/core/zip_updater.py::_resolve_archive_root_dir"""
    normalized_entries = [os.path.normpath(e) for e in entries]
    portable_entries = [e.replace("\\", "/") for e in normalized_entries]
    root_candidates: list[str] = []
    for raw_entry, normalized_entry, portable_entry in zip(
        entries, normalized_entries, portable_entries
    ):
        if normalized_entry == ".":
            continue
        has_children = any(
            other != portable_entry and other.startswith(f"{portable_entry}/")
            for other in portable_entries
        )
        if raw_entry.endswith(("/", "\\")) or has_children:
            root_candidates.append(normalized_entry)
            continue
        parent_portable, _, _ = portable_entry.rpartition("/")
        if not parent_portable:
            return ""
        root_candidates.append(parent_portable.replace("/", os.sep))
    if not root_candidates:
        return ""
    return os.path.commonpath(root_candidates)


def _find_plugin_metadata_entry(entries: list[str]) -> str | None:
    """复刻 AstrBot v4 astrbot/core/star/updater.py::find_plugin_metadata_entry"""
    update_dir = _resolve_archive_root_dir(entries)
    portable_update_dir = os.path.normpath(update_dir).replace("\\", "/")
    if portable_update_dir == ".":
        portable_update_dir = ""
    entries_by_portable_path: dict[str, str] = {}
    for entry in entries:
        portable_entry = os.path.normpath(entry).replace("\\", "/")
        if portable_entry in ("", "."):
            continue
        entries_by_portable_path[portable_entry] = entry
    candidates = (
        [f"{portable_update_dir}/{fname}" for fname in PLUGIN_METADATA_FILENAMES]
        if portable_update_dir
        else list(PLUGIN_METADATA_FILENAMES)
    )
    for candidate in candidates:
        if candidate in entries_by_portable_path:
            return entries_by_portable_path[candidate]
    return None


def _read_top_level_keys(text: str) -> dict[str, str]:
    """极简 YAML 标量解析：仅读取顶层 `key: value`（不带缩进的行），用于自校验。"""
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            continue  # 缩进行（嵌套内容）跳过
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            # 去掉行尾注释
            value = re.sub(r"\s+#.*$", "", value).strip()
            if value:
                result[key] = value
    return result


def validate_plugin_archive(zip_path: Path) -> None:
    """按 AstrBot 的规则校验压缩包，失败时抛出 ValueError（信息与 AstrBot 一致）。"""
    if not zip_path.exists():
        raise ValueError(f"压缩包不存在: {zip_path}")
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            entries = z.namelist()
            metadata_entry = _find_plugin_metadata_entry(entries)
            if metadata_entry is None:
                raise ValueError(
                    "压缩包不是合法的 AstrBot 插件：未找到 metadata.yaml 或 metadata.yml。"
                )
            try:
                metadata_text = z.read(metadata_entry).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{metadata_entry} 必须使用 UTF-8 编码。") from exc

            metadata = _read_top_level_keys(metadata_text)
            missing = [
                f for f in PLUGIN_METADATA_REQUIRED_FIELDS if f not in metadata
            ]
            if missing:
                raise ValueError(
                    f"{metadata_entry} 中缺少必需字段: {', '.join(missing)}。"
                )
            invalid = [
                f for f in PLUGIN_METADATA_REQUIRED_FIELDS
                if not isinstance(metadata[f], str) or not metadata[f].strip()
            ]
            if invalid:
                raise ValueError(
                    f"{metadata_entry} 中字段 {', '.join(invalid)} 必须是非空字符串。"
                )

            plugin_name = metadata["name"]
            if not plugin_name.isidentifier() or keyword.iskeyword(plugin_name):
                raise ValueError(
                    "metadata 文件中 name 不是合法的模块名称"
                    "（应为合法 Python 标识符且非关键字）。"
                )

            # 额外：单一顶层目录（v3/v4 均兼容的打包方式）
            top_level = {
                os.path.normpath(e).split(os.sep)[0]
                for e in entries
                if os.path.normpath(e) not in ("", ".")
            }
            if len(top_level) != 1:
                raise ValueError(
                    f"压缩包应只包含一个顶层目录（当前为: {sorted(top_level)}）。"
                    "请使用 build_release.py 重新打包，或避免使用访达「压缩」。"
                )
    except zipfile.BadZipFile as exc:
        raise ValueError("插件压缩包格式错误。") from exc


def build_release() -> Path:
    """构建发布压缩包并返回其路径。"""
    missing = [f for f in INCLUDE_FILES if not (ROOT / f).exists()]
    if missing:
        raise SystemExit(f"缺少文件，无法打包: {', '.join(missing)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_ZIP.exists():
        OUT_ZIP.unlink()

    with zipfile.ZipFile(OUT_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # 先写目录条目，保证 namelist()[0] 是插件目录（兼容 v3 解压逻辑）
        z.writestr(f"{PLUGIN_DIR_NAME}/", "")
        for rel in INCLUDE_FILES:
            arcname = f"{PLUGIN_DIR_NAME}/{rel}"
            z.write(ROOT / rel, arcname)

    return OUT_ZIP


def _iter_zip_entries(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path, "r") as z:
        return z.namelist()


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg:
        target = Path(arg)
        validate_plugin_archive(target)
        entries = _iter_zip_entries(target)
        metadata_entry = _find_plugin_metadata_entry(entries)
        print(f"[OK] 校验通过: {target}")
        print(f"     公共根目录: {_resolve_archive_root_dir(entries)!r}")
        print(f"     metadata 条目: {metadata_entry}")
        return

    zip_path = build_release()
    validate_plugin_archive(zip_path)
    entries = _iter_zip_entries(zip_path)
    print(f"[OK] 构建并校验通过: {zip_path} ({zip_path.stat().st_size} bytes)")
    print(f"     公共根目录: {_resolve_archive_root_dir(entries)!r}")
    print(f"     metadata 条目: {_find_plugin_metadata_entry(entries)}")
    print("     可直接在 AstrBot「插件管理 → 安装插件 → 上传压缩包」中安装。")


if __name__ == "__main__":
    main()
