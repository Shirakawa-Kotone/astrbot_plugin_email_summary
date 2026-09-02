"""
单封邮件分析器（AstrBot 插件版）
在拉取邮件时，对每封邮件单独调用 LLM 进行分析，结果持久化存储
配置通过参数传入，不依赖全局 config

兜底策略：LLM 失败（网络异常 / 返回内容不是合法 JSON）时，
使用本地规则分析（关键词 + 正则）从邮件原文提取关键信息，
保证即使没有 LLM 也能得到可用的摘要、优先级、分类、行动项等。
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

    logger = logging.getLogger("email_analyzer")


NETWORK_ERROR_HINTS = (
    "timeout",
    "timed out",
    "connection",
    "connect error",
    "cannot connect",
    "refused",
    "unreachable",
    "network",
    "ssl",
    "dns",
    "max retries",
    "apiconnectionerror",
    "apitimeouterror",
)


def is_network_error(message: str) -> bool:
    """判断错误信息是否属于网络/连接/超时类问题。

    用于批量熔断：只有端点不可达（超时、连接失败等）才中止整批，
    LLM 正常工作但返回内容不合法（如 JSON 解析失败）不算网络错误，
    不会触发熔断。
    """
    if not message:
        return False
    lower = str(message).lower()
    return any(hint in lower for hint in NETWORK_ERROR_HINTS)


def friendly_error(message: str) -> str:
    """把 LLM API 返回的原始错误翻译成可读的修复提示。

    常见问题：账户余额不足、模型名不存在（404）、API Key 无效（401）、
    Base URL 少了 /v1（404）。这样日志/列表里直接能看到「该改哪个配置」。
    """
    if not message:
        return ""
    msg = str(message)
    lower = msg.lower()

    # 余额不足
    if "balance" in lower or "insufficient" in lower:
        return (
            f"{msg}（账户余额不足：请前往 SiliconFlow 控制台充值，"
            "或更换其他有余额/免费的 OpenAI 兼容服务）"
        )
    # 401/403：鉴权失败
    if (
        "token is invalid" in lower
        or "unauthorized" in lower
        or "invalid api key" in lower
        or "authentication" in lower
        or "401" in lower
        or "403" in lower
    ):
        return f"{msg}（API Key 无效：请检查 llm_api_key 是否填写正确）"
    # 404：模型不存在 / 路径错误
    if "404" in lower or "not found" in lower or "does not exist" in lower:
        if "model" in lower or "模型" in msg:
            return (
                f"{msg}（模型名不存在：llm_model 需填服务商的完整模型 ID，"
                "如 deepseek-ai/DeepSeek-V3.2，不能填 gpt-4o-mini）"
            )
        if "url" in lower or "path" in lower or "base" in lower:
            return f"{msg}（Base URL 路径错误：llm_api_base 需以 /v1 结尾，如 https://api.siliconflow.cn/v1）"
        return f"{msg}（请求失败：请检查 llm_api_base / llm_model 配置）"
    # 限流
    if "429" in lower or "rate limit" in lower or "too many requests" in lower:
        return f"{msg}（请求过于频繁被限流，请稍后再试或降低扫描频率）"
    # 模型侧错误
    if "context length" in lower or "maximum context" in lower or "input length" in lower:
        return f"{msg}（邮件内容超过模型上下文上限，建议换更大上下文的模型）"
    return msg


def _extract_json(text: str) -> str:
    """从 LLM 输出中提取最外层 JSON 对象。

    支持的情况：
    - 纯 JSON：{"...": ...}
    - 包裹在代码块中：```json {...} ``` 或 ``` {...} ```
    - 前后有说明文字：先 ... ```json {...} ``` 后 ...

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


class EmailAnalyzer:
    """单封邮件分析器"""

    # ==================== 规则兜底分析的关键词表 ====================

    PRIORITY_HIGH_KEYWORDS = (
        "紧急", "加急", "特急", "尽快", "立即", "马上", "务必", "非常重要",
        "urgent", "asap", "immediately", "critical",
    )
    PRIORITY_MEDIUM_KEYWORDS = (
        "提醒", "注意", "请回复", "请确认", "请查收", "请审批", "请反馈",
        "截止", "今天", "明天", "本周", "下周", "deadline", "due",
        "reminder", "notice", "please",
    )
    IMPORTANT_KEYWORDS = (
        "重要", "紧急", "务必", "尽快", "立即", "非常重要", "关键",
        "important", "urgent", "asap", "critical", "attention", "priority",
    )
    CATEGORY_RULES = (
        ("会议", (
            "会议", "meeting", "参会", "议程", "agenda", "周会", "例会", "研讨会",
            "视频会议", "腾讯会议", "zoom", "邀请", "会议室", "讨论会",
        )),
        ("审批", (
            "审批", "approval", "approve", "报销", "请假", "申请", "合同", "盖章",
            "签字", "流程", "oa",
        )),
        ("财务", (
            "发票", "账单", "invoice", "付款", "收款", "到账", "转账", "财务",
            "工资", "报销", "金额", "费用", "预算", "报价",
        )),
        ("通知", (
            "通知", "公告", "notice", "announcement", "提醒", "须知", "政策",
            "制度", "规定", "变更", "调整", "升级", "停用",
        )),
        ("汇报", (
            "汇报", "周报", "月报", "日报", "报告", "report", "summary",
            "总结", "进度",
        )),
        ("招聘", (
            "招聘", "面试", "简历", "入职", "offer", "职位", "候选人", "hr",
        )),
    )
    GREETING_PREFIXES = (
        "你好", "您好", "尊敬的", "亲爱的", "各位", "大家好", "hi", "hello", "hey",
    )
    ACTION_VERBS = ("请回复", "请确认", "请审批", "请查收", "请处理", "请尽快",
                    "请反馈", "请安排", "请审阅", "请知悉")
    POSITIVE_KEYWORDS = (
        "成功", "感谢", "恭喜", "通过", "完成", "达成", "顺利", "欢迎", "批准",
        "good", "great", "thanks", "thank", "approved", "congrats", "welcome",
        "passed", "nice",
    )
    NEGATIVE_KEYWORDS = (
        "失败", "错误", "警告", "异常", "问题", "风险", "取消", "拒绝", "延迟",
        "故障", "停止", "无法", "抱歉", "遗憾", "驳回", "投诉", "缺",
        "error", "failed", "warning", "issue", "risk", "problem", "cancel",
        "reject", "delay", "sorry", "denied",
    )

    SYSTEM_PROMPT = (
        "你是一个企业邮件分析助手。对每封邮件进行独立分析，"
        "你的回复必须且只能是合法的 JSON 对象，"
        "不要包含任何解释文字、Markdown 代码块标记或其他额外内容。"
        "\n\n"
        "你必须使用以下精确格式输出（字段名称和类型完全一致）：\n"
        '{\n'
        '  "is_important": boolean,\n'
        '  "priority": "high" 或 "medium" 或 "low",\n'
        '  "category": "工作" 或 "会议" 或 "审批" 或 "通知" 或 "其他",\n'
        '  "sub_category": "string — 子分类名称",\n'
        '  "title": "string — 邮件标题原文",\n'
        '  "sender": "string — 发件人名称或邮箱",\n'
        '  "date": "string — 日期",\n'
        '  "body_summary": "string — 内容摘要，不超过30个字",\n'
        '  "key_points": ["string — 关键要点列表，每项不超过20字"],\n'
        '  "action_needed": "string — 需要采取的行动，如无则为空字符串",\n'
        '  "action_deadline": "string — 截止日期，如无则为空字符串",\n'
        '  "has_attachment": boolean,\n'
        '  "has_links": boolean,\n'
        '  "links": ["string — 链接列表，如无则为空数组"],\n'
        '  "amounts": ["string — 涉及的金额，如无则为空数组"],\n'
        '  "sentiment": "正面" 或 "负面" 或 "中性",\n'
        '  "tags": ["string — 标签列表"]\n'
        "}\n"
        "\n"
        "示例输出（仅作格式参考，不要照抄内容）：\n"
        '{\n'
        '  "is_important": true,\n'
        '  "priority": "high",\n'
        '  "category": "会议",\n'
        '  "sub_category": "团队周会",\n'
        '  "title": "本周项目进度汇报",\n'
        '  "sender": "张三",\n'
        '  "date": "2024-01-15",\n'
        '  "body_summary": "张三面邀1/20讨论Q1项目计划",\n'
        '  "key_points": ["1/20下午3点开会", "地点：会议室A", "需要准备Q1数据"],\n'
        '  "action_needed": "回复确认参会",\n'
        '  "action_deadline": "2024-01-19",\n'
        '  "has_attachment": false,\n'
        '  "has_links": true,\n'
        '  "links": ["https://meeting.example.com/abc123"],\n'
        '  "amounts": [],\n'
        '  "sentiment": "中性",\n'
        '  "tags": ["会议", "内部"]\n'
        "}"
        "\n\n"
        "重要提醒：\n"
        "- 必须输出合法 JSON，不要加 markdown 代码块（```）\n"
        "- 不要输出任何解释文字\n"
        "- 布尔值用 true/false（小写），不要用中文\n"
        "- 空字符串用 \"\"，空数组用 []\n"
        "- 所有字段都必须存在，不要省略任何字段\n"
    )

    def __init__(self, api_key: str, api_base: str, model: str, timeout: int = 60):
        self.api_key = api_key or ""
        self.api_base = api_base
        self.model = model
        self.timeout = timeout
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
            # timeout：单个 LLM 请求超时上限（秒），避免网络不通时扫描无限卡住。
            # max_retries=0：不重试。端点不可达时，重试只会让每封邮件多等一倍时间；
            # 批量级的连续失败熔断（main 的 _run_scan / _run_resummarize）会在
            # 连续失败超过阈值后中止整批，而不是逐封干等。
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.api_base,
                timeout=self.timeout,
                max_retries=0,
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
                temperature=0.2,
                max_tokens=2048,
            )
            return self._parse_response(
                response.choices[0].message.content,
                subject=subject,
                sender=sender,
                date=date,
                body=body,
            )

        except Exception as e:
            # 必须用 AstrBot 的 logger 记录，print() 只进 stdout，日志面板看不到
            err_text = friendly_error(str(e))
            logger.error(f"邮件分析失败: {err_text}")
            return self._fallback_result(subject, sender, date, body, error=err_text)

    def _parse_response(
        self,
        text: str,
        subject: str = "",
        sender: str = "",
        date: str = "",
        body: str = "",
    ) -> dict:
        json_str = _extract_json(text)
        if not json_str:
            logger.warning("LLM 返回内容中没有找到 JSON，已使用规则兜底分析")
            return self._fallback_result(
                subject, sender, date, body, error="LLM 返回内容中没有找到 JSON"
            )

        try:
            data = json.loads(json_str)
            if not isinstance(data, dict):
                # LLM 可能返回字符串/数组等非对象，直接走规则兜底
                logger.warning("LLM 返回内容不是 JSON 对象，已使用规则兜底分析")
                return self._fallback_result(
                    subject, sender, date, body, error="LLM 返回内容不是 JSON 对象"
                )
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
        except json.JSONDecodeError as e:
            logger.warning(f"LLM 返回内容无法解析为 JSON: {e}")
            logger.debug(f"原始输出: {text[:500]}")
            return self._fallback_result(
                subject, sender, date, body, error="LLM 返回内容无法解析为 JSON"
            )

    # ==================== 规则兜底分析（不依赖 LLM） ====================

    def _fallback_result(
        self, subject: str, sender: str, date: str, body: str, error: str = ""
    ) -> dict:
        """本地规则分析：LLM 不可用时，从邮件原文提取关键信息。

        与空壳兜底的区别：摘要/要点/行动项/截止日期/金额/链接等
        都由关键词 + 正则从真实邮件内容中提取，保证可用。
        """
        subject = (subject or "").strip()
        sender = (sender or "").strip()
        body = body or ""
        text = f"{subject} {body}"
        text_lower = text.lower()

        # ---- 优先级 / 是否重要 ----
        priority = self._rule_priority(text_lower)
        is_important = priority == "high" or self._has_any(
            text_lower, self.IMPORTANT_KEYWORDS
        )

        # ---- 分类 ----
        category, sub_category = self._rule_category(text_lower)

        # ---- 摘要：正文第一句（跳过问候语），取 30 字内 ----
        body_summary = self._first_sentence(body) or subject[:30] or "(无标题)"

        # ---- 关键要点 ----
        key_points = self._rule_key_points(body, subject)

        # ---- 行动项 / 截止日期（行动项只从正文提取，避免标题污染） ----
        action_needed = self._rule_action(body) or self._rule_action(subject)
        action_deadline = self._rule_deadline(text)

        # ---- 链接 / 附件 ----
        links = self._rule_links(body)
        has_links = bool(links)
        has_attachment = self._rule_attachment(text)

        # ---- 金额 ----
        amounts = self._rule_amounts(text)

        # ---- 情感 ----
        sentiment = self._rule_sentiment(text_lower)

        # ---- 标签 ----
        tags = []
        if is_important:
            tags.append("重要")
        if priority == "high":
            tags.append("紧急")
        if category and category != "其他":
            tags.append(category)
        if has_attachment:
            tags.append("含附件")
        if has_links:
            tags.append("含链接")
        if action_needed:
            tags.append("需行动")
        tags = tags[:6]

        return {
            "is_important": is_important,
            "priority": priority,
            "category": category,
            "sub_category": sub_category,
            "title": subject or "(无标题)",
            "sender": sender,
            "date": date,
            "body_summary": body_summary,
            "key_points": key_points,
            "action_needed": action_needed,
            "action_deadline": action_deadline,
            "has_attachment": has_attachment,
            "has_links": has_links,
            "links": links,
            "amounts": amounts,
            "sentiment": sentiment,
            "tags": tags,
            "analysis_error": error,
        }

    # ---------- 规则实现 ----------

    @staticmethod
    def _has_any(text_lower: str, keywords: tuple[str, ...]) -> bool:
        return any(k in text_lower for k in keywords)

    def _rule_priority(self, text_lower: str) -> str:
        if self._has_any(text_lower, self.PRIORITY_HIGH_KEYWORDS):
            return "high"
        if self._has_any(text_lower, self.PRIORITY_MEDIUM_KEYWORDS):
            return "medium"
        return "low"

    def _rule_category(self, text_lower: str) -> tuple[str, str]:
        """返回 (分类, 子分类)。子分类取第一个命中的具体关键词，剔除泛化词。"""
        generic = {
            "会议": {"会议", "参会", "meeting", "agenda", "zoom", "邀请", "会议室"},
            "审批": {"审批", "approval", "approve", "流程", "oa"},
            "财务": {"财务", "金额", "费用", "invoice", "预算"},
            "通知": {"通知", "notice", "announcement", "变更", "调整"},
            "汇报": {"汇报", "report", "summary", "总结"},
            "招聘": {"招聘", "hr"},
        }
        best_cat = "其他"
        best_sub = ""
        best_score = 0
        for cat, keywords in self.CATEGORY_RULES:
            score = 0
            first_kw = ""
            for kw in keywords:
                if kw in text_lower:
                    score += 1
                    if not first_kw:
                        first_kw = kw
            if score > best_score:
                best_cat, best_sub, best_score = cat, first_kw, score
        if (
            not best_sub
            or best_sub in generic.get(best_cat, set())
            or not re.search(r"[\u4e00-\u9fff]", best_sub)
        ):
            best_sub = ""
        return best_cat, best_sub

    def _first_sentence(self, body: str) -> str:
        """取正文第一个有意义句（跳过问候语/空话），截断 30 字。"""
        body = (body or "").strip()
        if not body:
            return ""
        # 先按段落，再按句号拆分
        for para in re.split(r"[\n\r]+", body):
            para = para.strip()
            if len(para) < 4:
                continue
            for sent in re.split(r"[。！？!?；;]+", para):
                sent = sent.strip("，。！？!?；;：:、 \t\"'“”‘’")
                if len(sent) < 4:
                    continue
                # 去掉问候语前缀（如「大家好，」「您好，」）
                candidate = sent
                for g in self.GREETING_PREFIXES:
                    if sent.lower().startswith(g):
                        rest = sent[len(g):].lstrip("，,：:、 \t")
                        candidate = rest if len(rest) >= 4 else ""
                        break
                if candidate:
                    return candidate[:30]
        # 兜底：返回第一段第一句
        for para in re.split(r"[\n\r]+", body):
            sent = re.split(r"[。！？!?；;]+", para.strip())[0].strip()[:30]
            if sent:
                return sent
        return ""

    def _rule_key_points(self, body: str, subject: str) -> list[str]:
        """从正文中提取含时间/金额/链接/行动等关键信息的句子。"""
        points: list[str] = []
        seen: set[str] = set()

        if subject and len(subject) <= 30 and subject not in seen:
            seen.add(subject)
            points.append(subject[:30])

        key_patterns = (
            r"\d{1,2}[:：]\d{2}",                    # 14:30
            r"\d{1,2}月\d{1,2}日",                  # 1月20日
            r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?",  # 2024-01-20 / 2024年1月20日
            r"今天|明天|后天|本周|下周",
            r"[¥￥]\s?\d|[\d,]+\.?\d*\s?(?:万元|元|美元|欧元|港币|rmb|usd)",
            r"https?://",
            r"截止|deadline|due",
            r"请|务必|尽快|紧急|需",
        )
        for sent in re.split(r"[。！？!?；;\n]+", body):
            sent = sent.strip().strip("，。！？!?；;、 \t")
            if len(sent) < 4 or len(sent) > 60:
                continue
            if any(re.search(pat, sent, re.IGNORECASE) for pat in key_patterns):
                key = sent[:20]
                if key not in seen:
                    seen.add(key)
                    points.append(key)
            if len(points) >= 5:
                break
        return points[:5]

    def _rule_action(self, text: str) -> str:
        """提取行动项：优先找包含动作动词的句子，取动词后的短语（≤30字）。"""
        action_verbs = (
            "请回复", "请确认", "请审批", "请查收", "请处理", "请尽快",
            "请反馈", "请安排", "请审阅", "请知悉", "尽快提交",
            "回复", "确认", "审批", "查收", "提交", "反馈", "处理", "参会", "填写",
        )
        for sent in re.split(r"[。！？!?；;\n]+", text):
            sent = sent.strip()
            if len(sent) < 4:
                continue
            for verb in action_verbs:
                idx = sent.find(verb)
                if idx >= 0:
                    action = sent[idx : idx + 30].strip("，。！？；、 \t")
                    return action[:30]
        # 兜底：匹配「请/烦请/麻烦/务必 + 动作短语」
        m = re.search(r"(?:请|烦请|麻烦|务必)[^，。；;,\n]{2,25}", text)
        if m:
            action = m.group(0).strip()
            if len(action) <= 30:
                return action
        return ""

    def _rule_deadline(self, text: str) -> str:
        """提取截止日期。

        支持：2024-01-20 / 2024年1月20日 / 1月20日（截止语境）、
        「今天/明天/后天 + 前/截止」、周X（本周五/周五 + 前/截止）。
        仅取有截止语义（前/之前/截止/deadline/due/by/于/在/到）的日期。
        """
        today = datetime.now()
        try:
            # 1. 显式带年份日期
            m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", text)
            if m:
                return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            # 2. 截止词在前 + 月日（如「于2月15日前」「截止1月20日」）
            m = re.search(
                r"(?:截止|于|在|到|之前|deadline|due|by|前)[^。；;，,\n]{0,10}?(\d{1,2})月(\d{1,2})日",
                text,
                re.IGNORECASE,
            )
            if m:
                d = datetime(today.year, int(m.group(1)), int(m.group(2)))
                if d.date() < today.date():  # 已过则顺延到下一年
                    d = d.replace(year=today.year + 1)
                return d.strftime("%Y-%m-%d")
            # 3. 今天/明天/后天 + 截止语义（如「明天前」「今天下班前」）
            m = re.search(
                r"(今天|明天|后天)(?=[^。；;，,\n]{0,8}(?:前|之前|截止|deadline|due|by))",
                text,
                re.IGNORECASE,
            )
            if m:
                delta = {"今天": 0, "明天": 1, "后天": 2}[m.group(1)]
                return (today + timedelta(days=delta)).strftime("%Y-%m-%d")
            # 4. （本周/下周）周X + 截止语义（如「本周五前」「下周一之前」）
            m = re.search(
                r"((?:本周|下周)?[一二三四五六日天])(?=[^。；;，,\n]{0,8}(?:前|之前|截止|deadline|due|by))",
                text,
                re.IGNORECASE,
            )
            if m:
                week_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
                expr = m.group(1)
                target = week_map[expr[-1]]
                delta_days = (target - today.weekday()) % 7
                if "下周" in expr:
                    delta_days += 7
                if delta_days == 0:
                    delta_days = 7
                return (today + timedelta(days=delta_days)).strftime("%Y-%m-%d")
        except Exception:
            pass
        return ""

    def _rule_links(self, body: str) -> list[str]:
        return re.findall(r"https?://[^\s，。；、（）()<>\"'“”‘’]+", body)[:5]

    def _rule_attachment(self, text: str) -> bool:
        if re.search(r"(?:附件|见附件|如附件|详见附件|enclosed|attachment)", text, re.IGNORECASE):
            return True
        if re.search(
            r"[\u4e00-\u9fff\w\-]+\.(?:docx?|xlsx?|pptx?|pdf|zip|rar|7z|txt)",
            text,
            re.IGNORECASE,
        ):
            return True
        return False

    def _rule_amounts(self, text: str) -> list[str]:
        amounts: list[str] = []
        for m in re.finditer(r"[¥￥]\s?[\d,]+\.?\d*", text):
            amounts.append(m.group(0).strip())
        for m in re.finditer(
            r"[\d,]+\.?\d*\s*(?:万元|元|美元|欧元|港币|rmb|usd)", text, re.IGNORECASE
        ):
            amounts.append(m.group(0).strip())
        # 去重保持顺序
        seen: set[str] = set()
        unique = [a for a in amounts if not (a in seen or seen.add(a))]
        return unique[:5]

    def _rule_sentiment(self, text_lower: str) -> str:
        pos = sum(text_lower.count(k) for k in self.POSITIVE_KEYWORDS)
        neg = sum(text_lower.count(k) for k in self.NEGATIVE_KEYWORDS)
        if neg > pos:
            return "负面"
        if pos > neg:
            return "正面"
        return "中性"


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
