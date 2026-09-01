"""
汇总报告生成器（AstrBot 插件版）
基于已存储的每封邮件分析结果，生成汇总报告
配置通过参数传入，不依赖全局 config
"""

import json
from pathlib import Path
from typing import Optional
from openai import OpenAI

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 独立运行兜底
    import logging

    logger = logging.getLogger("summary_reporter")


class SummaryReporter:
    """汇总报告生成器"""

    SYSTEM_PROMPTS = {
        "brief": """你是一个企业邮件汇报助手。根据已分析的数据，生成一份极简的汇报摘要。

输出 JSON：
```json
{{
  "title": "邮件汇报",
  "summary": "一句话总览（20字以内）",
  "total_count": 总数,
  "important_count": 重要数,
  "action_required_count": 需行动数,
  "important_emails": [
    {{
      "title": "标题",
      "sender": "发件人",
      "priority": "high|medium|low",
      "body_summary": "摘要（15字以内）",
      "action_needed": "行动（如有）"
    }}
  ],
  "action_items": ["行动事项列表"]
}}
```
""",
        "balanced": """你是一个专业的企业邮件汇报助手。根据已分析的数据，生成一份均衡的报告。

输出 JSON：
```json
{{
  "title": "邮件汇报",
  "summary": "简洁总览（50字以内）",
  "total_count": 总数,
  "important_count": 重要数,
  "action_required_count": 需行动数,
  "important_emails": [
    {{
      "title": "标题",
      "sender": "发件人",
      "date": "日期",
      "priority": "high|medium|low",
      "category": "分类",
      "body_summary": "摘要（30字以内）",
      "action_needed": "行动（如有）",
      "action_deadline": "截止日期（如有）"
    }}
  ],
  "action_items": [{{"task": "", "priority": "", "deadline": ""}}],
  "trends": ["趋势分析列表"]
}}
```
""",
        "detailed": """你是一个资深的企业邮件分析师。根据已分析的数据，生成一份详细报告。

输出 JSON（结构见 balanced，额外增加）：
- summary 包含关键数据和数字（100字以内）
- 保留所有有 action 的邮件完整信息
- body_summary 延长到 50 字
- 增加 amounts 汇总（如涉及金额）
- 增加截止日期提醒（临近的标红）
""",
        "ultra_detailed": """你是一个资深的企业邮件分析师。根据已分析的数据，生成一份超详细报告。

输出 JSON（结构见 detailed，额外增加）：
- summary 包含所有关键数据和趋势分析（150字以内）
- 所有邮件的完整 key_points 和 amounts
- 增加 risk_warnings 列表（风险预警）
- 增加 recommendations 列表（建议）
- 增加类别分布统计
""",
    }

    def __init__(self, api_key: str, api_base: str, model: str, mode: str = "balanced"):
        self.api_key = api_key or ""
        self.api_base = api_base
        self.model = model
        self.mode = mode
        self._client: Optional[OpenAI] = None

    def _get_client(self) -> OpenAI:
        """惰性创建 OpenAI 客户端，未配置 API Key 时不构造（避免加载即报错）。"""
        if self._client is None:
            if not self.api_key:
                raise ValueError(
                    "未配置 LLM API Key（llm_api_key），请先在 AstrBot 插件配置中填写。"
                )
            # timeout：LLM 请求超时上限（秒），避免网络不通时报告生成无限卡住
            self._client = OpenAI(
                api_key=self.api_key, base_url=self.api_base, timeout=60, max_retries=1
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

        email_text = self._build_email_text(all_analyses)
        system_prompt = self.SYSTEM_PROMPTS.get(self.mode, self.SYSTEM_PROMPTS["balanced"])

        logger.info(f"正在生成汇总报告（{len(all_analyses)} 封已分析邮件）...")

        try:
            response = self._get_client().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"请根据以下邮件分析结果生成汇报：\n{email_text}"},
                ],
                temperature=0.3,
                max_tokens=4000,
            )
            report = self._parse_report(response.choices[0].message.content)
            if not report:
                logger.warning("LLM 汇总报告返回内容无法解析，已使用本地兜底报告")
                return self._fallback_report(all_analyses)
            return report

        except Exception as e:
            logger.error(f"汇总报告生成失败: {e}")
            return self._fallback_report(all_analyses)

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
            if a.get("tags"):
                lines.append(f"标签: {'; '.join(a['tags'])}")
            lines.append("")
        return "\n".join(lines)

    def _parse_report(self, text: str) -> dict:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _fallback_report(self, all_analyses: list[dict]) -> dict:
        important = [a for a in all_analyses if a.get("is_important")]
        action = [a for a in all_analyses if a.get("action_needed")]

        return {
            "title": "邮件汇报",
            "summary": f"共 {len(all_analyses)} 封，其中 {len(important)} 封重要，{len(action)} 封需行动",
            "total_count": len(all_analyses),
            "important_count": len(important),
            "action_required_count": len(action),
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
                for a in action
                if a.get("action_needed")
            ],
        }

    def _load_all_analyses(self) -> list[dict]:
        from .email_analyzer import AnalysisStore

        store = AnalysisStore()
        return store.load_all()
