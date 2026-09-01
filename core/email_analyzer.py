"""
单封邮件分析器（AstrBot 插件版）
在拉取邮件时，对每封邮件单独调用 LLM 进行分析，结果持久化存储
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

    logger = logging.getLogger("email_analyzer")


class EmailAnalyzer:
    """单封邮件分析器"""

    SYSTEM_PROMPT = """你是一个企业邮件分析助手。对每封邮件进行独立分析，输出严格的 JSON 格式（不要输出其他内容）：

```json
{{
  "is_important": true/false,
  "priority": "high|medium|low",
  "category": "工作/会议/审批/通知/其他",
  "sub_category": "子分类",
  "title": "邮件标题",
  "sender": "发件人",
  "date": "日期",
  "body_summary": "内容摘要（30字以内）",
  "key_points": ["关键要点列表"],
  "action_needed": "需要采取的行动（如无则为空）",
  "action_deadline": "截止日期（如有）",
  "has_attachment": true/false,
  "has_links": true/false,
  "links": ["链接列表"],
  "amounts": ["涉及金额列表"],
  "sentiment": "正面/负面/中性",
  "tags": ["标签列表"]
}}
```

注意：
- 提取所有时间、数字、金额、截止日期等关键信息
- 注意邮件是否有附件、会议链接、签名档
- 判断发件人身份（领导、客户、同事、系统）
- 注意紧急程度
"""

    def __init__(self, api_key: str, api_base: str, model: str):
        self.api_key = api_key or ""
        self.api_base = api_base
        self.model = model
        self._client: Optional[OpenAI] = None

    def _get_client(self) -> OpenAI:
        """惰性创建 OpenAI 客户端。

        未配置 API Key 时不构造客户端（新版本 openai SDK 在构造时就会校验
        凭据并抛 Missing credentials），避免插件加载阶段直接报错。
        """
        if self._client is None:
            if not self.api_key:
                raise ValueError(
                    "未配置 LLM API Key（llm_api_key），请先在 AstrBot 插件配置中填写。"
                )
            # timeout：LLM 请求超时上限（秒），避免网络不通时扫描无限卡住
            self._client = OpenAI(
                api_key=self.api_key, base_url=self.api_base, timeout=60, max_retries=1
            )
        return self._client

    def analyze(self, subject: str, sender: str, date: str, body: str) -> dict:
        body_preview = body[:1500] if body else ""

        try:
            response = self._get_client().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"""请分析以下邮件：
标题: {subject}
发件人: {sender}
日期: {date}
正文: {body_preview}""",
                    },
                ],
                temperature=0.3,
                max_tokens=1500,
            )
            return self._parse_response(response.choices[0].message.content)

        except Exception as e:
            # 必须用 AstrBot 的 logger 记录，print() 只进 stdout，日志面板看不到
            logger.error(f"邮件分析失败: {e}")
            return self._fallback_result(subject, sender, date, error=str(e))

    def _parse_response(self, text: str) -> dict:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]

        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                # LLM 可能返回字符串/数组等非对象，直接走兜底
                logger.warning("LLM 返回内容不是 JSON 对象，已使用兜底结果")
                return self._fallback_result("", "", "", error="LLM 返回内容不是 JSON 对象")
            return {
                "is_important": data.get("is_important", False),
                "priority": data.get("priority", "low"),
                "category": data.get("category", "其他"),
                "sub_category": data.get("sub_category", ""),
                "title": data.get("title", ""),
                "sender": data.get("sender", ""),
                "date": data.get("date", ""),
                "body_summary": data.get("body_summary", ""),
                "key_points": data.get("key_points", []),
                "action_needed": data.get("action_needed", ""),
                "action_deadline": data.get("action_deadline", ""),
                "has_attachment": data.get("has_attachment", False),
                "has_links": data.get("has_links", False),
                "links": data.get("links", []),
                "amounts": data.get("amounts", []),
                "sentiment": data.get("sentiment", "中性"),
                "tags": data.get("tags", []),
            }
        except json.JSONDecodeError:
            logger.warning("LLM 返回内容无法解析为 JSON，已使用兜底结果")
            return self._fallback_result("", "", "", error="LLM 返回内容无法解析为 JSON")

    def _fallback_result(
        self, subject: str, sender: str, date: str, error: str = ""
    ) -> dict:
        return {
            "is_important": False,
            "priority": "low",
            "category": "其他",
            "sub_category": "",
            "title": subject or "(无标题)",
            "sender": sender,
            "date": date,
            "body_summary": "",
            "key_points": [],
            "action_needed": "",
            "action_deadline": "",
            "has_attachment": False,
            "has_links": False,
            "links": [],
            "amounts": [],
            "sentiment": "中性",
            "tags": [],
            "analysis_error": error,
        }


class AnalysisStore:
    """分析结果持久化存储"""

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir) / "analysis"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def save(self, uid: int, analysis: dict) -> None:
        file_path = self.data_dir / f"{uid}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, ensure_ascii=False, indent=2)

    def load(self, uid: int) -> Optional[dict]:
        file_path = self.data_dir / f"{uid}.json"
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def load_all(self, uids: list[int] = None) -> list[dict]:
        results = []
        for file_path in sorted(
            self.data_dir.glob("*.json"), key=lambda p: int(p.stem)
        ):
            uid = int(file_path.stem)
            if uids is None or uid in uids:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        data["uid"] = uid
                        results.append(data)
                except (json.JSONDecodeError, FileNotFoundError):
                    continue
        return results
