"""
邮件标签持久化管理（AstrBot 插件版）

支持用户通过 QQ 命令或网页为已分析的邮件添加自定义标签，
并提供优先级特殊处理：
  - 用户标签 "已完成"：该邮件不再出现在高优/待办列表中
  - 用户标签 "代办"：即使 LLM 判定为 Low 也会按中优先级展示
  - 其他自定义标签：仅用于筛选展示，不改变优先级

数据格式：
  data/tags.json  →  { "uid": [ "tag1", "tag2", ... ], ... }
"""

import json
from pathlib import Path


class TagStore:
    """标签持久化存储"""

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.tags_file = self.data_dir / "tags.json"
        self.tags_file.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        try:
            with open(self.tags_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        with open(self.tags_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ==================== 查询接口 ====================

    def get_tags(self, uid: int) -> list[str]:
        """获取指定 uid 的标签列表（用户标签，不含系统自动标签）"""
        return self._load().get(str(uid), [])

    def get_all_tags(self) -> dict[str, list[str]]:
        """获取全部标签数据（uid → 标签列表）"""
        return self._load()

    def has_tag(self, uid: int, tag: str) -> bool:
        """检查指定 uid 是否有指定标签"""
        return tag in self.get_tags(uid)

    def get_uids_by_tag(self, tag: str) -> list[str]:
        """获取带有指定标签的所有 uid 列表"""
        all_tags = self._load()
        return [uid for uid, tags in all_tags.items() if tag in tags]

    def get_done_uids(self) -> list[str]:
        """获取所有被标记为"已完成"的 uid 列表"""
        return self.get_uids_by_tag("已完成")

    def get_todo_uids(self) -> list[str]:
        """获取所有被标记为"代办"的 uid 列表"""
        return self.get_uids_by_tag("代办")

    def get_tagged_uids(self) -> list[str]:
        """获取所有有用户标签的 uid 列表（去重）"""
        all_tags = self._load()
        uids = set()
        for uid, tags in all_tags.items():
            if tags:  # 只要有任意标签就算
                uids.add(uid)
        return sorted(uids, key=lambda x: int(x) if x.isdigit() else 0)

    # ==================== 修改接口 ====================

    def add_tag(self, uid: int, tag: str) -> None:
        """为指定邮件添加标签"""
        data = self._load()
        uid_str = str(uid)
        if uid_str not in data:
            data[uid_str] = []
        if tag not in data[uid_str]:
            data[uid_str].append(tag)
            self._save(data)

    def remove_tag(self, uid: int, tag: str) -> None:
        """移除指定邮件的指定标签"""
        data = self._load()
        uid_str = str(uid)
        if uid_str in data and tag in data[uid_str]:
            data[uid_str].remove(tag)
            if not data[uid_str]:  # 没有标签了则删除整条记录
                del data[uid_str]
            self._save(data)

    def clear_tags(self, uid: int) -> None:
        """清除指定邮件的所有用户标签"""
        data = self._load()
        uid_str = str(uid)
        if uid_str in data:
            del data[uid_str]
            self._save(data)

    def clear_all(self) -> None:
        """清除所有标签"""
        self._save({})
