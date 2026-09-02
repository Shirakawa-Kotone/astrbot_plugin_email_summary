"""
汇总报告生成器（AstrBot 插件版）
基于已存储的每封邮件分析结果，生成汇总报告
配置通过参数传入，不依赖全局 config

v1.6 起：过滤已过截止日期的邮件/行动项，总结报告只显示未来和当天的待办。
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from openai import OpenAI

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 独立运行兜底
    import logging

    logger = logging.getLogger("summary_reporter")


def _parse_deadline_date(deadline_str: str) -> Optional[datetime]:
    """尝试解析 action_deadline 字符串为 datetime，失败返回 None。

    支持格式：
    - 2024-01-20
    - 2024/01/20
    - 2024年1月20日
    - 1月20日（当前年份）
    - 今天/明天/后天（相对于今天）
    """
    deadline_str = (deadline_str or "").strip()
    if not deadline_str:
        return None

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        # 显式带年份日期：2024-01-20 / 2024/01/20
        m = re.match(
            r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", deadline_str
        )
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))

        # 不带年份：1月20日（用当年）
        m = re.match(r"(\d{1,2})月(\d{1,2})日", deadline_str)
        if m:
            d = datetime(today.year, int(m.group(1)), int(m.group(2)))
            if d.date() < today.date():
                d = d.replace(year=today.year + 1)
            return d

        # 今天/明天/后天（中文）
        for word, delta in [("今天", 0), ("明天", 1), ("后天", 2)]:
            if deadline_str == word:
                return (today + timedelta(days=delta))

        # 本周/下周 周X（可带"前"后缀）
        week_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        m = re.match(
            r"(?:本周|下周)?([一二三四五六日天])(?:前|之前)?", deadline_str
        )
        if m:
            target = week_map.get(m.group(1))
            if target is not None:
                delta_days = (target - today.weekday()) % 7
                if delta_days == 0:
                    delta_days = 7
                return (today + timedelta(days=delta_days))

    except Exception:
        pass

    return None


def _is_deadline_past(deadline_str: str) -> bool:
    """判断截止日期是否已过（< 今天）。返回 True 表示已过期。"""
    dt = _parse_deadline_date(deadline_str)
    if dt is None:
        return False  # 无法解析，保留（不确定就不删）
    return dt.date() < datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).date()


def _is_402_error(analysis: dict) -> bool:
    """判断分析结果是否因 LLM 账户余额不足（402）导致失败。

    余额不足是用户账户层面的问题，修复后下一次扫描即可恢复，
    与真正的网络不可达不同，不应当作"无需处理的邮件"过滤掉。
    """
    err = str(analysis.get("analysis_error", "")).strip().lower()
    return bool(err and ("余额不足" in err or "balance" in err or "insufficient" in err))


def _is_actionable(analysis: dict) -> bool:
    """判断一封邮件是否有「值得在总结中展示」的未竟事项。

    规则：
    1. 402 余额不足 → 视为有效（用户充值后可用，不应被跳过）
    2. 用户标签 "已完成" → 已否定的，排除
    3. 有 action_deadline 且已过截止日 → 已否定的，排除（不再提）
    4. 有 action_needed 但无截止日期 → 保留（不确定是否已过）
    5. 无行动项且无截止日 → 保留（LLM 可能判断为重要邮件）
    """
    uid_str = str(analysis.get("uid", ""))
    user_tags = []
    try:
        from .tag_store import TagStore

        ts = TagStore()
        user_tags = ts.get_tags(int(uid_str)) if uid_str.isdigit() else []
    except Exception:
        pass

    # 402 余额不足 → 视为有效（用户充值后可恢复扫描，不应跳过）
    if _is_402_error(analysis):
        return True

    # "已完成"标签 → 已处理，排除
    if "已完成" in user_tags:
        return False

    deadline = str(analysis.get("action_deadline", "")).strip()
    action = str(analysis.get("action_needed", "")).strip()

    # 有截止日期且已过期 → 排除（不再提）
    if deadline and _is_deadline_past(deadline):
        return False

    return True


def _extract_json(text: str) -> str:
    """从 LLM 输出中提取最外层 JSON 对象。

    支持的情况：
    - 纯 JSON：{"...": ...}
    - 包裹在代码块中：```json {...} ``` 或 ``` {...} ```
    - 前后有说明文字

    通过递归匹配最外层成对的大括号来定位 JSON 范围，从而正确处理
    内部嵌套数组/对象中出现的 {}，避免因 rfind("}") 截断导致的不合法 JSON。
    """
    if not text:
        return ""

    # 先尝试去掉代码块标记
    code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if code_block:
        text = code_block.group(1)

    # 递归找到最外层 { } 对
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : i + 1]
    # 如果括号不匹配（LLM 截断），尽力截取
    if start is not None:
        return text[start:]
    return ""


class SummaryReporter:
    """汇总报告生成器"""

    SYSTEM_PROMPTS = {
        "brief": (
            "你是一个企业邮件汇报助手。根据已分析的数据，生成一份极简的汇报摘要。"
            "你的回复必须且只能是合法的 JSON 对象，不要包含任何解释文字、Markdown "
            "代码块标记或其他额外内容。\n\n"
            "格式如下（字段名称完全一致）：\n"
            '{\n'
            '  "title": "string — 报告标题",\n'
            '  "summary": "string — 一句话总览，20字以内",\n'
            '  "total_count": number — 邮件总数,\n'
            '  "important_count": number — 重要邮件数,\n'
            '  "action_required_count": number — 需行动邮件数,\n'
            '  "important_emails": [\n'
            '    {\n'
            '      "title": "string — 标题",\n'
            '      "sender": "string — 发件人",\n'
            '      "priority": "high" 或 "medium" 或 "low",\n'
            '      "body_summary": "string — 摘要，15字以内",\n'
            '      "action_needed": "string — 行动，如无则为空字符串"\n'
            '    }\n'
            '  ],\n'
            '  "action_items": ["string — 行动事项列表"]\n'
            "}\n"
            "\n"
            "重要提醒：\n"
            "- 不要加 markdown 代码块（```）\n"
            "- 所有字段都必须存在，不要省略\n"
            "- 数字用纯数字，不要用字符串\n"
        ),
        "balanced": (
            "你是一个专业的企业邮件汇报助手。根据已分析的数据，生成一份均衡的报告。"
            "你的回复必须且只能是合法的 JSON 对象，不要包含任何解释文字、Markdown "
            "代码块标记或其他额外内容。\n\n"
            "格式如下（字段名称完全一致）：\n"
            '{\n'
            '  "title": "string — 报告标题",\n'
            '  "summary": "string — 简洁总览，50字以内",\n'
            '  "total_count": number,\n'
            '  "important_count": number,\n'
            '  "action_required_count": number,\n'
            '  "important_emails": [\n'
            '    {\n'
            '      "title": "string",\n'
            '      "sender": "string",\n'
            '      "date": "string",\n'
            '      "priority": "high" 或 "medium" 或 "low",\n'
            '      "category": "string — 分类",\n'
            '      "body_summary": "string — 摘要，30字以内",\n'
            '      "action_needed": "string — 行动，如无则为空字符串",\n'
            '      "action_deadline": "string — 截止日期，如无则为空字符串"\n'
            '    }\n'
            '  ],\n'
            '  "action_items": [\n'
            '    {"task": "string", "priority": "string", "deadline": "string"}\n'
            '  ],\n'
            '  "trends": ["string — 趋势分析列表"]\n'
            "}\n"
            "\n"
            "重要提醒：\n"
            "- 不要加 markdown 代码块（```）\n"
            "- 所有字段都必须存在\n"
            "- 空字符串用 \"\"，空数组用 []\n"
        ),
        "detailed": (
            "你是一个资深的企业邮件分析师。根据已分析的数据，生成一份详细报告。"
            "你的回复必须且只能是合法的 JSON 对象，不要包含任何解释文字、Markdown "
            "代码块标记或其他额外内容。\n\n"
            "格式基于 balanced 模式，额外增加以下字段：\n"
            '- summary 包含关键数据和数字（100字以内）\n'
            "- important_emails 保留所有有 action 的邮件完整信息（加 amounts 字段）\n"
            '- body_summary 延长到50字\n'
            "- 增加 amounts 汇总（如涉及金额）\n"
            "- 增加截止日期提醒（临近的标红）\n"
            "\n"
            "格式如下：\n"
            '{\n'
            '  "title": "string",\n'
            '  "summary": "string — 100字以内",\n'
            '  "total_count": number,\n'
            '  "important_count": number,\n'
            '  "action_required_count": number,\n'
            '  "important_emails": [\n'
            '    {\n'
            '      "title": "string",\n'
            '      "sender": "string",\n'
            '      "date": "string",\n'
            '      "priority": "high" 或 "medium" 或 "low",\n'
            '      "category": "string",\n'
            '      "body_summary": "string — 50字以内",\n'
            '      "action_needed": "string",\n'
            '      "action_deadline": "string",\n'
            '      "amounts": ["string"]\n'
            '    }\n'
            '  ],\n'
            '  "action_items": [{"task": "string", "priority": "string", "deadline": "string"}],\n'
            '  "trends": ["string"],\n'
            '  "amount_summary": "string — 金额汇总",\n'
            '  "deadline_alerts": ["string — 截止日期提醒"]\n'
            "}\n"
            "\n"
            "重要提醒：\n"
            "- 不要加 markdown 代码块（```）\n"
            "- 所有字段都必须存在\n"
            "- 空字符串用 \"\"，空数组用 []\n"
        ),
        "ultra_detailed": (
            "你是一个资深的企业邮件分析师。根据已分析的数据，生成一份超详细报告。"
            "你的回复必须且只能是合法的 JSON 对象，不要包含任何解释文字、Markdown "
            "代码块标记或其他额外内容。\n\n"
            "格式基于 detailed 模式，额外增加以下字段：\n"
            '- summary 包含所有关键数据和趋势分析（150字以内）\n'
            "- 所有邮件的完整 key_points 和 amounts\n"
            "- 增加 risk_warnings 列表（风险预警）\n"
            "- 增加 recommendations 列表（建议）\n"
            "- 增加类别分布统计\n"
            "\n"
            "格式如下：\n"
            '{\n'
            '  "title": "string",\n'
            '  "summary": "string — 150字以内",\n'
            '  "total_count": number,\n'
            '  "important_count": number,\n'
            '  "action_required_count": number,\n'
            '  "category_distribution": {"string": number},\n'
            '  "important_emails": [\n'
            '    {\n'
            '      "title": "string",\n'
            '      "sender": "string",\n'
            '      "date": "string",\n'
            '      "priority": "high" 或 "medium" 或 "low",\n'
            '      "category": "string",\n'
            '      "body_summary": "string — 50字以内",\n'
            '      "key_points": ["string"],\n'
            '      "action_needed": "string",\n'
            '      "action_deadline": "string",\n'
            '      "amounts": ["string"],\n'
            '      "sentiment": "string"\n'
            '    }\n'
            '  ],\n'
            '  "action_items": [{"task": "string", "priority": "string", "deadline": "string"}],\n'
            '  "trends": ["string"],\n'
            '  "amount_summary": "string",\n'
            '  "deadline_alerts": ["string"],\n'
            '  "risk_warnings": ["string"],\n'
            '  "recommendations": ["string"]\n'
            "}\n"
            "\n"
            "重要提醒：\n"
            "- 不要加 markdown 代码块（```）\n"
            "- 所有字段都必须存在\n"
            "- 空字符串用 \"\"，空数组用 []\n"
        ),
    }

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model: str,
        mode: str = "balanced",
        timeout: int = 60,
    ):
        self.api_key = api_key or ""
        self.api_base = api_base
        self.model = model
        self.mode = mode
        self.timeout = timeout
        self._client: Optional[OpenAI] = None

    def _get_client(self) -> OpenAI:
        """惰性创建 OpenAI 客户端，未配置 API Key 时不构造（避免加载即报错）。"""
        if self._client is None:
            if not self.api_key:
                raise ValueError(
                    "未配置 LLM API Key（llm_api_key），请先在 AstrBot 插件配置中填写。"
                )
            # timeout：单个 LLM 请求超时上限（秒），避免网络不通时报告生成无限卡住。
            # max_retries=0：不重试，端点不可达时快速失败（配合批量熔断使用）。
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.api_base,
                timeout=self.timeout,
                max_retries=0,
            )
        return self._client

    def generate_report(
        self, all_analyses: list[dict] = None, recent_n: int = None
    ) -> dict:
        if all_analyses is None:
            all_analyses = self._load_all_analyses()

        if recent_n:
            all_analyses = all_analyses[-recent_n:]

        if not all_analyses:
            return {
                "title": "邮件汇报",
                "summary": "本次无邮件记录",
                "total_count": 0,
                "important_count": 0,
                "action_required_count": 0,
                "important_emails": [],
                "action_items": [],
            }

        # 过滤：排除已过截止日期的邮件和用户标记"已完成"的邮件
        actionable_analyses = [a for a in all_analyses if _is_actionable(a)]
        skipped_count = len(all_analyses) - len(actionable_analyses)

        email_text = self._build_email_text(actionable_analyses)
        system_prompt = self.SYSTEM_PROMPTS.get(self.mode, self.SYSTEM_PROMPTS["balanced"])

        # 在 LLM 的 system prompt 中加入过期过滤指示
        system_prompt += (
            "\n\n注意：以下邮件已经过预处理，仅包含未来或当天的待办事项。"
            "已过期截止日期的邮件不会被包含在此列表中。"
            "如果报告中所有邮件都无具体行动项，请如实生成一份简洁报告说明当前无紧急事项。"
        )

        logger.info(
            f"正在生成汇总报告（{len(all_analyses)} 封已分析邮件，"
            f"{len(actionable_analyses)} 封有效/{skipped_count} 封已过滤）..."
        )

        try:
            response = self._get_client().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"请根据以下邮件分析结果生成汇报：\n{email_text}"},
                ],
                temperature=0.2,
                max_tokens=8192,
            )
            report = self._parse_report(response.choices[0].message.content)
            if not report:
                logger.warning("LLM 汇总报告返回内容无法解析，已使用本地兜底报告")
                return self._fallback_report(actionable_analyses)
            # LLM 可能忽略没有行动项的 402 邮件，手动补入
            self._inject_402_errors(report, actionable_analyses)
            return report

        except Exception as e:
            from .email_analyzer import friendly_error

            logger.error(f"汇总报告生成失败: {friendly_error(str(e))}")
            return self._fallback_report(actionable_analyses)

    def _build_email_text(self, analyses: list[dict]) -> str:
        lines = []
        for i, a in enumerate(analyses, 1):
            if not isinstance(a, dict):
                continue
            lines.append(
                f"\n--- 邮件 {i} ---"
                f"\n标题: {a.get('title', '')}"
                f"\n发件人: {a.get('sender', '')}"
                f"\n日期: {a.get('date', '')}"
                f"\n优先级: {a.get('priority', '')}"
                f"\n重要: {'是' if a.get('is_important') else '否'}"
                f"\n分类: {a.get('category', '')}"
            )
            if a.get("body_summary"):
                lines.append(f"摘要: {a['body_summary']}")
            if a.get("key_points"):
                lines.append(f"要点: {'; '.join(a['key_points'])}")
            if a.get("action_needed"):
                lines.append(f"行动: {a['action_needed']}")
            if a.get("action_deadline"):
                lines.append(f"截止: {a['action_deadline']}")
            if a.get("amounts"):
                lines.append(f"金额: {'; '.join(a['amounts'])}")
            if a.get("attachment_names"):
                lines.append(f"附件: {'; '.join(a['attachment_names'])}")
            if a.get("tags"):
                lines.append(f"标签: {'; '.join(a['tags'])}")
            lines.append("")
        return "\n".join(lines)

    def _parse_report(self, text: str) -> dict:
        json_str = _extract_json(text)
        if not json_str:
            logger.warning("LLM 返回内容中没有找到 JSON，已使用兜底报告")
            return {}

        try:
            data = json.loads(json_str)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as e:
            logger.warning(f"LLM 返回内容无法解析为 JSON: {e}")
            logger.debug(f"原始输出: {text[:500]}")
            return {}

    def _fallback_report(self, all_analyses: list[dict]) -> dict:
        important = [a for a in all_analyses if a.get("is_important")]
        action = [a for a in all_analyses if a.get("action_needed")]

        # 402 余额不足邮件：无论有没有 action_needed 都纳入展示
        error_402 = [a for a in all_analyses if _is_402_error(a)]
        action_normal = [a for a in action if not _is_402_error(a)]

        # 过滤已过期截止日期的邮件
        actionable_action = [
            a for a in action_normal
            if not a.get("action_deadline") or not _is_deadline_past(a.get("action_deadline", ""))
        ]
        important = [
            a for a in important
            if _is_actionable(a)
        ]

        # 分类分布统计
        cat_count: dict[str, int] = {}
        for a in all_analyses:
            c = str(a.get("category") or "其他")
            cat_count[c] = cat_count.get(c, 0) + 1
        cat_desc = "，".join(
            f"{k}×{v}" for k, v in sorted(cat_count.items(), key=lambda x: -x[1])[:5]
        )

        # 截止日期提醒（只保留未来或当天的）
        deadline_alerts = [
            f"{a.get('title', '')[:20]}（{a.get('action_deadline')}）"
            for a in actionable_action
            if a.get("action_deadline")
        ][:5]

        report = {
            "title": "邮件汇报",
            "summary": f"共 {len(all_analyses)} 封，其中 {len(important)} 封重要，{len(actionable_action)} 封需行动",
            "total_count": len(all_analyses),
            "important_count": len(important),
            "action_required_count": len(actionable_action),
            "important_emails": [
                {
                    "title": a.get("title", ""),
                    "sender": a.get("sender", ""),
                    "priority": a.get("priority", "low"),
                    "body_summary": a.get("body_summary", ""),
                    "action_needed": a.get("action_needed", ""),
                }
                for a in important
            ],
            "action_items": [
                {
                    "task": a.get("action_needed", ""),
                    "priority": a.get("priority", "low"),
                    "deadline": a.get("action_deadline", ""),
                }
                for a in actionable_action
                if a.get("action_needed")
            ],
        }
        # 补充 402 余额不足邮件到报告（LLM 不可用时仍需展示邮件信息）
        existing_uids = {a.get("uid") for a in report["important_emails"]}
        for a in error_402:
            if a.get("uid") not in existing_uids:
                report["important_emails"].append(
                    {
                        "title": a.get("title", ""),
                        "sender": a.get("sender", ""),
                        "priority": a.get("priority", "low"),
                        "body_summary": a.get("body_summary", ""),
                        "action_needed": a.get("action_needed", ""),
                        "analysis_error": a.get("analysis_error", ""),
                    }
                )
                existing_uids.add(a.get("uid"))
        if cat_desc:
            report["trends"] = [f"分类分布: {cat_desc}"]
        if deadline_alerts:
            report["deadline_alerts"] = deadline_alerts
        return report

    def _inject_402_errors(self, report: dict, analyses: list[dict]) -> None:
        """把 LLM 报告遗漏的 402 余额不足邮件补入 important_emails。"""
        existing_uids = {e.get("uid") for e in report.get("important_emails", [])}
        for a in analyses:
            if _is_402_error(a) and a.get("uid") not in existing_uids:
                report.setdefault("important_emails", []).append(
                    {
                        "title": a.get("title", ""),
                        "sender": a.get("sender", ""),
                        "priority": a.get("priority", "low"),
                        "body_summary": a.get("body_summary", ""),
                        "action_needed": a.get("action_needed", ""),
                        "analysis_error": a.get("analysis_error", ""),
                    }
                )
                existing_uids.add(a.get("uid"))

    def _load_all_analyses(self) -> list[dict]:
        from .email_analyzer import AnalysisStore

        store = AnalysisStore()
        return store.load_all()
