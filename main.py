"""
企业微信邮箱智能整理 AstrBot 插件

工作流：
1. 定时（间隔/每日固定时间）通过 IMAP 拉取企业微信邮箱新邮件
2. 对每封新邮件调用 LLM 独立分析（优先级/分类/摘要/行动项），结果持久化
3. 拉取完成后自动生成汇总报告，主动推送到目标 QQ
4. 支持 QQ 命令触发：
   - /总结 → 基于已存储的分析结果生成汇总报告
   - /邮件列表 → 列出已分析邮件
   - /邮件 <编号> → 查看指定邮件分析详情
   - /扫描 → 立即手动触发一次拉取+逐封分析
   - /重新总结 → 把未总结/上次失败的邮件重新分析一遍
   - /重新总结 全部 → 强制把范围内所有邮件重新分析并覆盖旧结果

并发控制：同一时间只允许运行一个「扫描/重新总结」任务；
「汇总报告生成」独立加锁，避免同时多次生成。
所有耗时任务（扫描/重新总结/生成报告）都会实时汇报进度与 ETA，
QQ 端通过 event.send 推送，网页端通过 /status 轮询展示。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain, File
from astrbot.core.message.message_event_result import MessageChain

from .core.imap_fetcher import IMAPFetcher, StateManager, MAX_ATTACHMENT_SIZE
from .core.email_analyzer import EmailAnalyzer, AnalysisStore, is_network_error
from .core.summary_reporter import SummaryReporter
from .core.tag_store import TagStore

PLUGIN_DIR_NAME = "email_summary_assistant"

# ---------- 进度展示工具 ----------


def _progress_bar(pct: int, width: int = 15) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def _format_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        m = int(seconds / 60)
        s = seconds % 60
        return f"{m}分{s:.0f}秒"
    else:
        h = int(seconds / 3600)
        m = int((seconds % 3600) / 60)
        return f"{h}小时{m}分"


# ---------- 插件 Web API 兼容层（AstrBot v4.24.1 ~ v4.27.x） ----------
# v4.27.x：建议用 astrbot.api.web 的 request / json_response；
# v4.24.x：挂在 /api/plug/<subpath>，handler 由 Quart 调用，用 quart.request。
# 这里统一封装：读取 query 参数 + 构造 JSON 响应，两个版本都能跑。
try:
    from astrbot.api.web import request as _web_request  # type: ignore
except ImportError:  # AstrBot < 4.27
    from quart import request as _web_request  # type: ignore


def _web_query(name: str, default=None):
    """读取 query 参数（兼容 astrbot.api.web.request.query 与 quart request.args）"""
    try:
        return _web_request.query.get(name, default)
    except AttributeError:
        return _web_request.args.get(name, default)


async def _web_body_json(default=None):
    """读取 POST JSON body（兼容 v4.24.x quart 与 v4.27 astrbot.api.web）。"""
    import inspect as _inspect

    payload = None
    try:
        from quart import request as _q_request

        fn = getattr(_q_request, "get_json", None)
        if callable(fn):
            if _inspect.iscoroutinefunction(fn):
                payload = await fn(silent=True)
            else:
                payload = fn(silent=True)
    except Exception:
        pass
    if not isinstance(payload, dict):
        try:
            from astrbot.api.web import request as _a_request

            fn = getattr(_a_request, "json", None)
            if callable(fn):
                res = await fn() if _inspect.iscoroutinefunction(fn) else fn()
                if isinstance(res, dict):
                    payload = res
        except Exception:
            pass
    if not isinstance(payload, dict):
        return default if default is not None else {}
    return payload


def _web_ok(data):
    """标准成功 envelope：bridge 会 resolve 为 data"""
    return {"status": "ok", "data": data}


def _web_err(message: str, status_code: int = 400):
    """标准错误 envelope：bridge 会 reject（tuple 形式兼容 Quart/FastAPI 视图）"""
    return {"status": "error", "message": message, "data": None}, status_code


@star.register(
    "企业微信邮箱智能整理",
    "chen4",
    "定时拉取企业微信邮箱，LLM 逐封分析，汇总报告推送 QQ，支持命令查询",
    "1.6.0",
)
class EmailSummaryPlugin(star.Star):
    def __init__(
        self, context: star.Context, config: Optional[AstrBotConfig] = None
    ):
        super().__init__(context)
        # AstrBot 仅当插件目录存在 _conf_schema.json 时才传入 config；
        # 否则回退为只传 context，这里兜底为空配置，所有取值走默认值。
        self.config: AstrBotConfig = config if config is not None else {}

        # 插件专属数据目录
        self.data_dir: Path = star.StarTools.get_data_dir(PLUGIN_DIR_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 邮件附件保存目录（<data_dir>/attachments/<uid>/文件名）
        self.attachment_dir: Path = self.data_dir / "attachments"
        self.attachment_dir.mkdir(parents=True, exist_ok=True)

        # 核心组件（在 initialize 中构建）
        self.fetcher: Optional[IMAPFetcher] = None
        self.analyzer: Optional[EmailAnalyzer] = None
        self.reporter: Optional[SummaryReporter] = None
        self.store = AnalysisStore(str(self.data_dir))
        self.state_manager = StateManager(str(self.data_dir))
        self.tag_store = TagStore(str(self.data_dir))

        # 定时调度器（initialize 中创建并启动）
        self.scheduler: Optional[AsyncIOScheduler] = None

        # ============ 进程锁（同一时间只允许一个任务） ============
        self._scan_lock = asyncio.Lock()    # 扫描 / 重新总结（互斥）
        self._report_lock = asyncio.Lock()  # 汇总报告生成（互斥）
        # 后台任务引用（防止被 GC）
        self._background_tasks: set[asyncio.Task] = set()

        # ============ 共享进度状态（供网页 /status 轮询 + 重启恢复） ============
        self._progress_state: dict[str, Any] = {
            "running": False,
            "operation": "",        # "scan" | "resummarize" | "report"
            "label": "",            # 用户可见任务名
            "phase": "空闲",        # 准备/拉取/分析/报告/完成
            "message": "暂无任务",
            "current": 0,
            "total": 0,
            "failed": 0,
            "skipped": 0,
            "percent": 0,
            "eta_seconds": 0,
            "elapsed_seconds": 0,
            "started_at": 0.0,
            "finished_at": 0.0,
            "total_time": 0.0,
            "completed": False,
            "report": None,         # 最近一次任务生成的报告
        }

        # 注册插件 Web API（供插件 Pages 网页调用）
        self._register_web_apis()

    # ==================== 插件 Web API（供 Pages 网页） ====================

    def _register_web_apis(self):
        """注册 Web API 路由。

        注意：路由必须以插件名前缀开头（bridge 转发时去掉前缀），且不使用
        动态路径段（v4.24.x 挂载在 /api/plug/<subpath> 做精确匹配），
        参数一律走 query / JSON body。
        """
        prefix = f"/{PLUGIN_DIR_NAME}"
        self.context.register_web_api(
            f"{prefix}/list",
            self._web_api_list,
            ["GET"],
            "获取邮件分析列表",
        )
        self.context.register_web_api(
            f"{prefix}/detail",
            self._web_api_detail,
            ["GET"],
            "获取单封邮件分析详情",
        )
        self.context.register_web_api(
            f"{prefix}/report",
            self._web_api_report,
            ["GET"],
            "生成汇总报告",
        )
        self.context.register_web_api(
            f"{prefix}/scan",
            self._web_api_scan,
            ["POST"],
            "触发一次邮件扫描",
        )
        self.context.register_web_api(
            f"{prefix}/resummarize",
            self._web_api_resummarize,
            ["POST"],
            "重新总结邮件分析（body.force=true 强制全部重新分析）",
        )
        self.context.register_web_api(
            f"{prefix}/status",
            self._web_api_status,
            ["GET"],
            "获取任务进度状态（供网页轮询）",
        )
        self.context.register_web_api(
            f"{prefix}/tags",
            self._web_api_tags,
            ["GET"],
            "获取所有邮件的标签",
        )
        self.context.register_web_api(
            f"{prefix}/tag",
            self._web_api_tag,
            ["POST"],
            "为邮件添加/移除标签（body: uid, tag, action=add|remove）",
        )

    async def _web_api_list(self):
        """GET ?limit=30 → 邮件分析列表（最新在前）"""
        try:
            limit = int(_web_query("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        all_analyses = self.store.load_all()
        records = []
        for a in reversed(all_analyses[-limit:]):
            if not isinstance(a, dict):
                continue
            records.append(
                {
                    "uid": a.get("uid", ""),
                    "title": a.get("title", ""),
                    "sender": a.get("sender", ""),
                    "date": a.get("date", ""),
                    "priority": a.get("priority", "low"),
                    "is_important": bool(a.get("is_important")),
                    "category": a.get("category", ""),
                    "body_summary": a.get("body_summary", ""),
                    "analysis_error": a.get("analysis_error", ""),
                    "has_attachment": bool(a.get("has_attachment")),
                    "attachment_names": a.get("attachment_names", []),
                    "user_tags": self.tag_store.get_tags(
                        int(a.get("uid", 0))
                    ) if str(a.get("uid", "")).isdigit() else [],
                }
            )
        return _web_ok({"records": records, "total": len(records)})

    async def _web_api_detail(self):
        """GET ?uid=<文件名前缀> → 单封邮件完整分析"""
        uid = str(_web_query("uid", "") or "").strip()
        if not uid:
            return _web_err("缺少 uid 参数")
        if not uid.replace("-", "").isdigit():
            return _web_err("uid 必须为数字")
        try:
            analysis = self.store.load(int(uid))
        except (ValueError, TypeError):
            return _web_err("uid 必须为数字")
        if not isinstance(analysis, dict):
            return _web_err(f"未找到 uid={uid} 的分析记录", 404)
        return _web_ok(analysis)

    async def _web_api_report(self):
        """GET → 触发汇总报告生成（后台执行），进度通过 /status 轮询。"""
        if not self.config.get("llm_api_key"):
            return _web_err("未配置 LLM API Key（llm_api_key），请先在插件配置中填写。")
        busy = self._busy_hint()
        if busy:
            return _web_err(busy, 409)
        tracker = self._make_tracker("report", "正在生成汇总报告")
        self._set_running_state(tracker)
        task = asyncio.create_task(self._run_report_task(tracker))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return _web_ok({"message": "汇总报告生成中，进度请查看页面顶部。"})

    async def _web_api_scan(self):
        """POST → 触发一次扫描（后台执行），进度通过 /status 轮询。"""
        missing = self._missing_configs()
        if missing:
            return _web_err(f"尚未配置: {', '.join(missing)}。请先在插件配置中填写。")
        if self._scan_lock.locked():
            return _web_err("已有扫描/重新总结正在运行，请等待完成后再试。", 409)
        tracker = self._make_tracker("scan", "正在扫描邮箱并逐封分析")
        self._set_running_state(tracker)
        task = asyncio.create_task(
            self._run_scan(push=False, tracker=tracker, with_report=True)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return _web_ok({"message": "扫描已开始，进度请查看页面顶部。"})

    async def _web_api_resummarize(self):
        """POST {force: bool} → 触发重新总结（后台执行），进度通过 /status 轮询。"""
        missing = self._missing_configs()
        if missing:
            return _web_err(f"尚未配置: {', '.join(missing)}。请先在插件配置中填写。")
        if self._scan_lock.locked():
            return _web_err("已有扫描/重新总结正在运行，请等待完成后再试。", 409)
        try:
            body = await _web_body_json({})
        except Exception:
            body = {}
        force = bool(body.get("force", False))
        mode = "全部邮件（强制覆盖）" if force else "仅未总结/上次失败的邮件"
        tracker = self._make_tracker("resummarize", f"正在重新总结（{mode}）")
        self._set_running_state(tracker)
        task = asyncio.create_task(self._run_resummarize(force=force, tracker=tracker))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return _web_ok({"message": f"重新总结已开始（{mode}），进度请查看页面顶部。"})

    async def _web_api_status(self):
        """GET → 返回任务进度状态（供网页轮询）。"""
        st = dict(self._progress_state)
        st["scan_busy"] = self._scan_lock.locked()
        st["report_busy"] = self._report_lock.locked()
        st["busy"] = st["scan_busy"] or st["report_busy"]
        if st["running"] and st["started_at"]:
            st["elapsed_seconds"] = round(time.time() - st["started_at"], 1)
        return _web_ok(st)

    # ==================== 标签 Web API ====================

    async def _web_api_tags(self):
        """GET → 获取所有邮件的标签（含分析结果中的系统标签）"""
        all_tags = self.tag_store.get_all_tags()
        all_analyses = self.store.load_all()
        # 构建 uid → 分析结果 的映射
        analysis_map = {str(a.get("uid", "")): a for a in all_analyses if isinstance(a, dict)}
        result = {}
        for uid, user_tags in all_tags.items():
            analysis = analysis_map.get(uid)
            system_tags = []
            if analysis and not analysis.get("analysis_error"):
                system_tags = analysis.get("tags", [])
            result[uid] = {
                "user_tags": user_tags,
                "system_tags": system_tags,
                "title": (analysis.get("title", "")) if analysis else "未知",
            }
        return _web_ok({"tags": result, "total": len(result)})

    async def _web_api_tag(self):
        """POST {uid, tag, action: "add"|"remove"} → 添加/移除标签"""
        try:
            body = await _web_body_json({})
        except Exception:
            body = {}
        uid_str = str(body.get("uid", "")).strip()
        tag = str(body.get("tag", "")).strip()
        action = str(body.get("action", "add")).strip().lower()
        if not uid_str or not uid_str.isdigit():
            return _web_err("uid 必须为数字")
        if not tag:
            return _web_err("标签不能为空")
        uid = int(uid_str)
        if action == "remove":
            self.tag_store.remove_tag(uid, tag)
            return _web_ok({"message": f"已移除标签「{tag}」"})
        elif action == "clear":
            self.tag_store.clear_tags(uid)
            return _web_ok({"message": "已清除所有标签"})
        else:
            self.tag_store.add_tag(uid, tag)
            return _web_ok({"message": f"已添加标签「{tag}」"})

    # ==================== 生命周期 ====================

    async def initialize(self):
        """插件加载完成后的异步初始化"""
        self._init_components()

        # 创建并启动调度器
        self.scheduler = AsyncIOScheduler()
        self._register_scheduler()
        if self.scheduler.get_jobs():
            self.scheduler.start()
            logger.info("邮箱智能整理: 定时任务已启动")

        logger.info("邮箱智能整理插件已加载")

    async def terminate(self):
        """插件卸载时清理"""
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
        logger.info("邮箱智能整理插件已卸载")

    def _init_components(self):
        """从配置构建核心组件"""
        self.fetcher = IMAPFetcher(
            host=self.config.get("imap_host", "imap.exmail.qq.com"),
            port=int(self.config.get("imap_port", 993)),
            address=self.config.get("email_address", ""),
            auth_code=self.config.get("email_auth_code", ""),
            attachment_dir=str(self.attachment_dir),
        )
        api_key = self.config.get("llm_api_key", "")
        api_base = self.config.get("llm_api_base", "https://api.openai.com/v1")
        model = self.config.get("llm_model", "gpt-4o-mini")
        try:
            llm_timeout = int(self.config.get("llm_timeout", 60))
        except (TypeError, ValueError):
            llm_timeout = 60
        try:
            llm_max_tokens = int(self.config.get("llm_max_tokens", 16384))
        except (TypeError, ValueError):
            llm_max_tokens = 16384
        try:
            self.analysis_concurrency = max(
                1, int(self.config.get("analysis_concurrency", 5))
            )
        except (TypeError, ValueError):
            self.analysis_concurrency = 5

        self.analyzer = EmailAnalyzer(
            api_key=api_key,
            api_base=api_base,
            model=model,
            timeout=llm_timeout,
            max_tokens=llm_max_tokens,
        )
        self.reporter = SummaryReporter(
            api_key=api_key,
            api_base=api_base,
            model=model,
            mode=self.config.get("summary_mode", "balanced"),
            timeout=llm_timeout,
            max_tokens=llm_max_tokens,
        )

    def _register_scheduler(self):
        """注册定时任务"""
        try:
            schedule_mode = self.config.get("schedule_mode", "interval")
            if schedule_mode == "fixed":
                fixed_time = str(self.config.get("fixed_time", "09:00"))
                hour, minute = map(int, fixed_time.split(":"))
                self.scheduler.add_job(
                    self._run_scan,
                    "cron",
                    hour=hour,
                    minute=minute,
                    id="email_daily_scan",
                    replace_existing=True,
                )
                logger.info(f"邮箱智能整理: 已注册每日任务 {fixed_time}")
            else:
                interval_hours = float(self.config.get("interval_hours", 2))
                self.scheduler.add_job(
                    self._run_scan,
                    "interval",
                    hours=interval_hours,
                    id="email_interval_scan",
                    replace_existing=True,
                )
                logger.info(f"邮箱智能整理: 已注册间隔任务 每{interval_hours}小时")
        except Exception as e:
            logger.error(f"邮箱智能整理: 定时任务注册失败 {e}")

    # ==================== 配置与锁辅助 ====================

    def _missing_configs(self) -> list[str]:
        return [
            name
            for name, key in (
                ("email_address", "email_address"),
                ("email_auth_code", "email_auth_code"),
                ("llm_api_key", "llm_api_key"),
            )
            if not self.config.get(key)
        ]

    def _busy_hint(self) -> str:
        """返回当前占用任务的提示；无占用返回空串。"""
        hints = []
        if self._scan_lock.locked():
            hints.append("正在执行扫描/重新总结（完成后会自动生成汇总报告）")
        if self._report_lock.locked():
            hints.append("已有汇总报告正在生成")
        return "；".join(hints)

    # ==================== 进度追踪器 ====================

    class _ProgressTracker:
        """耗时任务进度追踪 + ETA 估算。

        - QQ 命令：通过 set_callback 注入回调（event.send 发送实时消息）
        - 网页任务：无回调，只同步 self._state（网页通过 /status 轮询读取）
        """

        def __init__(
            self,
            operation: str,
            label: str,
            initial_message: str = "",
            state: Optional[dict] = None,
        ):
            self.operation = operation
            self.label = label
            self.initial_message = initial_message or label
            self.total = 0
            self.current = 0
            self.failed = 0
            self.skipped = 0
            self.reanalyzed = 0
            self._start_time = 0.0
            self._finished = False
            self._callback: Optional[Callable[[str], Any]] = None
            self._state: dict = state if state is not None else {}
            self.stats: dict[str, Any] = {
                "total": 0,
                "current": 0,
                "failed": 0,
                "skipped": 0,
                "reanalyzed": 0,
                "total_time": 0.0,
                "message": "",
            }

        def set_callback(self, callback: Callable[[str], Any]) -> None:
            self._callback = callback

        async def _send(self, msg: str) -> None:
            if not self._callback:
                return
            try:
                ret = self._callback(msg)
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:
                logger.warning(f"邮箱智能整理: 发送进度消息失败: {e}")

        async def start(self) -> None:
            if self._start_time:
                return
            self._start_time = time.time()
            self._state.update(
                running=True,
                operation=self.operation,
                label=self.label,
                phase="准备",
                message=self.initial_message,
                current=0,
                total=0,
                failed=0,
                skipped=0,
                percent=0,
                eta_seconds=0,
                started_at=self._start_time,
                finished_at=0,
                total_time=0,
                completed=False,
                report=None,
            )
            await self._send(self.initial_message)

        def set_total(self, total: int) -> None:
            self.total = total
            self._sync_state()

        def set_phase(self, phase: str, message: str = "") -> None:
            self._state["phase"] = phase
            if message:
                self._state["message"] = message

        def mark_done(
            self, count: int = 1, failed: int = 0, skipped: int = 0, reanalyzed: int = 0
        ) -> None:
            self.current += count
            self.failed += failed
            self.skipped += skipped
            self.reanalyzed += reanalyzed
            self._sync_state()

        def mark_failed(self, count: int = 1) -> None:
            self.current += count
            self.failed += count
            self._sync_state()

        def _sync_state(self) -> None:
            st = self._state
            st.update(
                current=self.current,
                total=self.total,
                failed=self.failed,
                skipped=self.skipped,
            )
            if self.total > 0:
                st["percent"] = int(self.current / self.total * 100)
            if self._start_time:
                elapsed = time.time() - self._start_time
                if self.current > 0 and elapsed > 0 and self.total > 0:
                    st["eta_seconds"] = max(
                        0, (self.total - self.current) * (elapsed / self.current)
                    )
                else:
                    st["eta_seconds"] = 0

        async def send_progress(self) -> None:
            if self.current <= 0:
                return
            elapsed = time.time() - self._start_time if self._start_time else 0
            eta = 0.0
            if self.current > 0 and elapsed > 0 and self.total > 0:
                eta = max(0, (self.total - self.current) * (elapsed / self.current))
            pct = int(self.current / self.total * 100) if self.total > 0 else 0
            bar = _progress_bar(pct)
            eta_txt = f" (ETA: {_format_time(eta)})" if eta > 0 else ""
            msg = f"⏳ [{bar}] {self.current}/{self.total} ({pct}%){eta_txt}"
            self._state.update(message=msg, eta_seconds=round(eta, 1), percent=pct)
            await self._send(msg)

        async def finish(self, message: str = "") -> None:
            if self._finished:
                return
            self._finished = True
            total_time = time.time() - self._start_time if self._start_time else 0
            self.stats = {
                "total": self.total,
                "current": self.current,
                "failed": self.failed,
                "skipped": self.skipped,
                "reanalyzed": self.reanalyzed,
                "total_time": round(total_time, 1),
                "message": message,
            }
            self._state.update(
                running=False,
                completed=True,
                finished_at=time.time(),
                total_time=round(total_time, 1),
                phase="完成",
                message=message or "完成",
                percent=100 if self.total > 0 else 0,
                eta_seconds=0,
            )
            await self._send(f"✅ 处理完成（总耗时: {_format_time(total_time)}）{message}")

    def _make_tracker(self, operation: str, label: str) -> "_ProgressTracker":
        return self._ProgressTracker(
            operation=operation,
            label=label,
            state=self._progress_state,
        )

    @staticmethod
    def _merge_attachment_info(analysis: dict, email) -> dict:
        """把 IMAP 提取到的真实附件信息合并进分析结果。

        LLM/规则兜底只能靠正文猜「有没有附件」；这里是权威信息，
        直接覆盖 has_attachment 并补充 attachment_names（文件名/大小）。
        """
        analysis["has_attachment"] = bool(email.attachments)
        analysis["attachment_names"] = [
            f"{a.get('filename', '')} ({a.get('size_str', '')})".strip()
            for a in (email.attachments or [])
        ]
        analysis["attachment_paths"] = [
            a.get("path", "") for a in (email.attachments or [])
        ]
        return analysis

    def _set_running_state(self, tracker: "_ProgressTracker") -> None:
        """在任务真正开始前先把 running 置为 True，避免网页首轮轮询误判空闲。"""
        self._progress_state.update(
            running=True,
            operation=tracker.operation,
            label=tracker.label,
            phase="启动",
            message="任务已提交，等待调度...",
            current=0,
            total=0,
            failed=0,
            skipped=0,
            percent=0,
            eta_seconds=0,
            started_at=time.time(),
            finished_at=0,
            total_time=0,
            completed=False,
            report=None,
        )

    # ==================== 核心扫描逻辑 ====================

    async def _run_scan(
        self,
        push: bool = True,
        tracker: Optional["_ProgressTracker"] = None,
        with_report: Optional[bool] = None,
    ) -> Optional[dict]:
        """执行一次完整扫描周期：拉取 → 逐封分析 → 汇总推送。

        tracker: 进度追踪器（可选）。
        with_report: 是否生成汇总报告；None 时跟随 push（定时任务默认生成）。
        """
        if self._scan_lock.locked():
            logger.warning("邮箱智能整理: 已有扫描在运行，跳过本次")
            if tracker:
                await tracker.finish("已有任务在运行，本次扫描已跳过")
            return None

        async with self._scan_lock:
            if not self.fetcher:
                self._init_components()

            missing = self._missing_configs()
            if missing:
                logger.warning(f"邮箱智能整理: 配置不完整（{', '.join(missing)}），跳过扫描")
                if tracker:
                    await tracker.finish(f"⚠️ 配置不完整: {', '.join(missing)}")
                return None

            logger.info(f"开始扫描 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            if tracker:
                await tracker.start()
                tracker.set_phase("拉取", "正在连接邮箱并拉取新邮件...")
                await tracker._send("⏳ 正在连接邮箱并拉取新邮件...")

            # 1. 拉取邮件
            try:
                emails = await asyncio.to_thread(
                    self.fetcher.get_latest_emails,
                    since_days=int(self.config.get("max_scan_days", 7)),
                    max_count=int(self.config.get("max_emails", 50)),
                    scan_read=True,
                )
            except Exception as e:
                logger.error(f"邮箱智能整理: IMAP 拉取失败 {e}")
                if tracker:
                    await tracker.finish("⚠️ IMAP 拉取失败")
                return None

            # 2. 过滤已处理
            state = self.state_manager.load()
            processed_uids = state.get("processed_uids", set())
            new_emails = [e for e in emails if e.uid not in processed_uids]

            if not new_emails:
                logger.info("邮箱智能整理: 没有新邮件")
                if tracker:
                    await tracker.finish("没有新的邮件")
                self.state_manager.save(
                    datetime.now().isoformat(), processed_uids, []
                )
                return None

            if tracker:
                tracker.set_total(len(new_emails))
                tracker.set_phase(
                    "分析", f"开始分析 {len(new_emails)} 封邮件（并发数: {self.analysis_concurrency}）"
                )
            logger.info(f"发现 {len(new_emails)} 封新邮件，LLM 并发分析（concurrency={self.analysis_concurrency}）...")

            # 3. 并发分析（Semaphore 限流）
            new_uids = set(processed_uids)
            failed_count = 0
            network_aborted = False
            sem = asyncio.Semaphore(self.analysis_concurrency)

            async def _analyze_one(email):
                async with sem:
                    analysis = await asyncio.to_thread(
                        self.analyzer.analyze,
                        email.subject,
                        email.sender,
                        email.date,
                        email.body_text,
                    )
                    self._merge_attachment_info(analysis, email)
                    error_msg = analysis.get("analysis_error", "")
                    if not error_msg:
                        await self._notify_high_priority(analysis, email)
                    self.store.save(email.uid, analysis)
                    new_uids.add(email.uid)
                    logger.info(
                        f"邮件 {email.uid} 分析完成: "
                        f"{analysis.get('title', '')[:30]} "
                        f"[{analysis.get('priority', '')}]"
                    )
                    return analysis

            analyses_results = await asyncio.gather(
                *[_analyze_one(email) for email in new_emails],
                return_exceptions=True,
            )

            # 统计结果（按完成顺序处理）
            for i, result in enumerate(analyses_results):
                if isinstance(result, Exception):
                    failed_count += 1
                    logger.error(f"邮件 {new_emails[i].uid} 分析异常: {result}")
                    if tracker:
                        tracker.mark_failed()
                else:
                    error_msg = result.get("analysis_error", "")
                    if error_msg:
                        failed_count += 1
                        if is_network_error(error_msg) and not network_aborted:
                            network_aborted = True
                            aborted_remaining = len(new_emails) - i - 1
                            logger.warning(
                                f"邮箱智能整理: 网络错误触发熔断，中止剩余 {aborted_remaining} 封分析"
                            )
                            if tracker:
                                await tracker._send(
                                    f"⏹️ 网络错误触发熔断，已中止剩余 {aborted_remaining} 封。\n"
                                    "请检查 llm_api_key / llm_api_base / llm_model 配置与网络连通性。"
                                )
                            break  # 熔断：跳过后面的
                    if tracker:
                        tracker.mark_done(failed=1 if error_msg else 0)
                        await tracker.send_progress()

            # 4. 更新状态
            self.state_manager.save(datetime.now().isoformat(), new_uids, [])
            if failed_count:
                logger.warning(
                    f"邮箱智能整理: 本次 {failed_count}/{len(new_emails)} 封邮件 LLM 分析失败，"
                    "已保存兜底结果。请检查 llm_api_key / llm_api_base / "
                    "llm_model 配置及网络连通性。"
                )

            # 5. 汇总推送（网络熔断时 LLM 不可达，跳过报告生成避免再等一次超时）
            if with_report is None:
                with_report = push
            if with_report and not network_aborted:
                if tracker:
                    tracker.set_phase("报告", "正在生成汇总报告...")
                    await tracker._send("⏳ 正在生成汇总报告...")
                report = await self._generate_report(tracker=tracker)
                if report:
                    self._progress_state["report"] = report
                    if push and self.config.get("push_enabled", True):
                        await self._push_to_target(report)
                    if tracker:
                        await tracker.finish("汇总报告已生成")
                    return report
                if tracker:
                    await tracker.finish("⚠️ 汇总报告生成失败（请检查 LLM 配置）")
                return None

            if tracker:
                if network_aborted:
                    await tracker.finish(
                        f"⏹️ 已中止（网络错误熔断，剩余 {aborted_remaining} 封未处理）"
                    )
                else:
                    await tracker.finish()
            return None

    async def _run_report_task(self, tracker: "_ProgressTracker") -> Optional[dict]:
        """网页触发的独立汇总报告生成任务。"""
        report = await self._generate_report(tracker=tracker)
        if report:
            self._progress_state["report"] = report
            await tracker.finish("汇总报告已生成")
        else:
            await tracker.finish("⚠️ 暂无邮件分析记录或生成失败")
        return report

    async def _run_resummarize(
        self, force: bool = False, tracker: Optional["_ProgressTracker"] = None
    ) -> Optional[dict]:
        """重新总结已扫描范围内的邮件（逐封重新调用 LLM 分析并覆盖旧结果）。

        force=False（默认）：只重新分析「未分析过」或「上次分析失败（带 analysis_error）」
        的邮件（即"没有总结的在总结一遍"）；
        force=True：范围内所有邮件，无论是否已分析，全部强制重新分析覆盖。

        重新分析完成后自动重新生成一次汇总报告（不主动推送，由调用方展示）。
        返回统计信息与最新报告，任何异常/配置缺失返回 None。
        """
        if self._scan_lock.locked():
            logger.warning("邮箱智能整理: 已有扫描/重新总结在运行，跳过本次")
            if tracker:
                await tracker.finish("已有任务在运行，本次重新总结已跳过")
            return None

        async with self._scan_lock:
            if not self.fetcher:
                self._init_components()

            missing = self._missing_configs()
            if missing:
                logger.warning(
                    f"邮箱智能整理: 配置不完整（{', '.join(missing)}），跳过重新总结"
                )
                if tracker:
                    await tracker.finish(f"⚠️ 配置不完整: {', '.join(missing)}")
                return None

            mode_label = "全部邮件（强制覆盖）" if force else "仅未总结/上次失败的邮件"
            logger.info(
                f"开始重新总结（{mode_label}）- "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            if tracker:
                await tracker.start()
                tracker.set_phase("拉取", "正在拉取扫描范围内的邮件...")

            # 1. 重新从 IMAP 拉取扫描范围内的邮件（原始正文未持久化，需重新拉取）
            try:
                emails = await asyncio.to_thread(
                    self.fetcher.get_latest_emails,
                    since_days=int(self.config.get("max_scan_days", 7)),
                    max_count=int(self.config.get("max_emails", 50)),
                    scan_read=True,
                )
            except Exception as e:
                logger.error(f"邮箱智能整理: 重新总结时 IMAP 拉取失败 {e}")
                if tracker:
                    await tracker.finish("⚠️ IMAP 拉取失败")
                return None

            if not emails:
                logger.info("邮箱智能整理: 重新总结范围内没有邮件")
                if tracker:
                    await tracker.finish("没有需要重新总结的邮件")
                return {
                    "total": 0,
                    "reanalyzed": 0,
                    "skipped": 0,
                    "failed": 0,
                    "force": force,
                    "report": None,
                    "message": "扫描范围内没有邮件，无需重新总结。",
                }

            # 2. 逐封判断是否需要重新分析
            state = self.state_manager.load()
            processed_uids = state.get("processed_uids", set())
            new_uids = set(processed_uids)

            reanalyzed = 0
            skipped = 0
            failed = 0

            # 计算实际需要重新分析的数量（用于 ETA）
            need_to_analyze = 0
            for email in emails:
                existing = self.store.load(email.uid)
                need = force or existing is None or bool(existing.get("analysis_error"))
                if need:
                    need_to_analyze += 1

            if tracker:
                tracker.set_total(max(need_to_analyze, len(emails)))
                tracker.set_phase(
                    "分析",
                    f"扫描范围内 {len(emails)} 封，其中 {need_to_analyze} 封需要重新分析",
                )

            # 3. 筛选需要重新分析的邮件，并发处理
            emails_to_analyze = []
            skipped = 0
            for email in emails:
                existing = self.store.load(email.uid)
                need = force or existing is None or bool(existing.get("analysis_error"))
                if need:
                    emails_to_analyze.append(email)
                else:
                    skipped += 1

            network_aborted = False
            reanalyzed = 0
            failed = 0
            remaining_after_abort = 0
            sem = asyncio.Semaphore(self.analysis_concurrency)

            async def _reanalyze_one(email):
                async with sem:
                    analysis = await asyncio.to_thread(
                        self.analyzer.analyze,
                        email.subject,
                        email.sender,
                        email.date,
                        email.body_text,
                    )
                    self._merge_attachment_info(analysis, email)
                    error_msg = analysis.get("analysis_error", "")
                    if not error_msg:
                        await self._notify_high_priority(analysis, email)
                    self.store.save(email.uid, analysis)
                    new_uids.add(email.uid)
                    logger.info(
                        f"邮件 {email.uid} 重新分析完成: "
                        f"{analysis.get('title', '')[:30]} [{analysis.get('priority', '')}]"
                    )
                    return (analysis, error_msg)

            analyses_results = await asyncio.gather(
                *[_reanalyze_one(email) for email in emails_to_analyze],
                return_exceptions=True,
            )

            for result in analyses_results:
                if isinstance(result, Exception):
                    failed += 1
                    logger.error(f"邮件重新分析异常: {result}")
                    if tracker:
                        tracker.mark_failed()
                else:
                    analysis, error_msg = result
                    if error_msg:
                        failed += 1
                        if is_network_error(error_msg) and not network_aborted:
                            network_aborted = True
                            remaining_after_abort = len(emails_to_analyze) - reanalyzed - failed
                            logger.warning(
                                f"邮箱智能整理: 网络错误触发熔断，中止剩余 {remaining_after_abort} 封重新分析"
                            )
                            if tracker:
                                await tracker._send(
                                    f"⏹️ 网络错误触发熔断，已中止剩余 {remaining_after_abort} 封。\n"
                                    "请检查 llm_api_key / llm_api_base / llm_model 配置与网络连通性。"
                                )
                            break
                    else:
                        reanalyzed += 1
                    if tracker:
                        tracker.mark_done(reanalyzed=1 if not error_msg else 0)
                        await tracker.send_progress()

            # 3. 更新已处理状态（本次涉及的邮件都标记为已处理，避免重复扫描）
            self.state_manager.save(datetime.now().isoformat(), new_uids, [])

            result = {
                "total": len(emails),
                "reanalyzed": reanalyzed,
                "skipped": skipped,
                "failed": failed,
                "force": force,
                "report": None,
                "network_aborted": network_aborted,
            }

            # 4. 若有重新分析且未熔断，自动重新生成汇总报告（网络熔断时 LLM 不可达，跳过）
            if tracker and not network_aborted:
                tracker.set_phase("报告", "正在生成汇总报告...")
                await tracker._send("⏳ 正在生成汇总报告...")
            if reanalyzed and not network_aborted:
                report = await self._generate_report(tracker=tracker)
                result["report"] = report
                if report:
                    self._progress_state["report"] = report

            if tracker:
                if network_aborted:
                    await tracker.finish(
                        f"⏹️ 已中止（网络错误熔断，剩余 {remaining_after_abort} 封未处理）"
                    )
                else:
                    await tracker.finish(
                        f"共{len(emails)}封：重新分析{reanalyzed}，跳过{skipped}，失败{failed}"
                    )
            logger.info(
                f"邮箱智能整理: 重新总结完成 共{len(emails)}封，"
                f"重新分析{reanalyzed}封，跳过{skipped}封，失败{failed}封"
                + ("，已因连续网络失败中止" if network_aborted else "")
            )
            return result

    async def _generate_report(
        self, tracker: Optional["_ProgressTracker"] = None
    ) -> Optional[dict]:
        """生成汇总报告（内部持有 _report_lock，同一时间只允许一个在生成）。"""
        if not self.reporter:
            self._init_components()

        if not self.config.get("llm_api_key"):
            logger.warning("邮箱智能整理: 未配置 llm_api_key，跳过报告生成")
            return None

        async with self._report_lock:
            if tracker:
                tracker.set_phase("报告", "正在生成汇总报告...")

            all_analyses = self.store.load_all()
            recent_n = int(self.config.get("report_recent_n", 30))

            try:
                report = await asyncio.to_thread(
                    self.reporter.generate_report, all_analyses, recent_n
                )
                return report
            except Exception as e:
                logger.error(f"邮箱智能整理: 报告生成失败 {e}")
                return None

    # ==================== 主动推送 ====================

    async def _push_to_target(self, report: dict):
        """将报告主动推送到目标 QQ"""
        target_qq = str(self.config.get("target_qq", "")).strip()
        if not target_qq:
            logger.info("邮箱智能整理: 未配置 target_qq，跳过主动推送")
            return

        text = self._format_report(report)

        # 找到运行中的平台实例，构建 session_id: platform:friend:qq
        try:
            platforms = self.context.platform_manager.get_insts()
            sent = False
            for p in platforms:
                if getattr(p, "status", None) and p.status.name != "RUNNING":
                    continue
                platform_id = p.meta().id
                session_id = f"{platform_id}:friend:{target_qq}"
                try:
                    await self.context.send_message(
                        session_id, MessageChain([Plain(text)])
                    )
                    logger.info(f"邮箱智能整理: 汇总报告已推送到 {target_qq} ({platform_id})")
                    sent = True
                    break
                except Exception as e:
                    logger.warning(f"邮箱智能整理: 平台 {platform_id} 发送失败: {e}")
            if not sent:
                logger.warning("邮箱智能整理: 没有可用的平台发送主动消息")
        except Exception as e:
            logger.error(f"邮箱智能整理: 主动推送失败 {e}")

    async def _notify_high_priority(self, analysis: dict, email) -> None:
        """当扫描到 High/Medium 优先级邮件时，立即向目标 QQ 推送提醒。"""
        if not self.config.get("tag_notifications_enabled", True):
            return
        priority = str(analysis.get("priority", "low")).lower()
        if priority not in ("high", "medium"):
            return
        target_qq = str(self.config.get("target_qq", "")).strip()
        if not target_qq:
            return

        title = analysis.get("title", "无标题")
        sender = analysis.get("sender", "")
        sender_short = sender[:20]
        date = analysis.get("date", "")
        summary = analysis.get("body_summary", "")
        action = analysis.get("action_needed", "")
        deadline = analysis.get("action_deadline", "")
        category = analysis.get("category", "")

        prio_label = "🔴 高" if priority == "high" else "🟡 中"
        lines = [f"⚡ 高优邮件提醒 [{prio_label}优先级]"]
        lines.append(f"标题: {title[:35]}")
        lines.append(f"来自: {sender_short}")
        lines.append(f"日期: {date[:10] if date else ''}")
        if summary:
            lines.append(f"摘要: {summary[:40]}")
        if action:
            lines.append(f"行动: {action[:30]}")
        if deadline:
            lines.append(f"截止: {deadline}")
        if category and category != "其他":
            lines.append(f"分类: {category}")
        msg = "\n".join(lines)

        try:
            platforms = self.context.platform_manager.get_insts()
            for p in platforms:
                if getattr(p, "status", None) and p.status.name != "RUNNING":
                    continue
                platform_id = p.meta().id
                session_id = f"{platform_id}:friend:{target_qq}"
                try:
                    await self.context.send_message(
                        session_id, MessageChain([Plain(msg)])
                    )
                    logger.info(
                        f"邮箱智能整理: 高优邮件提醒已发送至 {target_qq} "
                        f"（{title[:20]}，{priority}）"
                    )
                    return
                except Exception as e:
                    logger.warning(f"邮箱智能整理: 平台 {platform_id} 发送失败: {e}")
        except Exception as e:
            logger.error(f"邮箱智能整理: 发送高优提醒失败 {e}")

    # ==================== 消息格式化 ====================

    def _format_report(self, report: dict) -> str:
        lines = []
        lines.append(f"📧 {report.get('title', '邮件汇报')}")
        lines.append("─" * 25)

        if report.get("summary"):
            lines.append(f"总览: {report.get('summary', '')}")
        lines.append(
            f"📊 总数: {report.get('total_count', 0)} | "
            f"重要: {report.get('important_count', 0)} | "
            f"需行动: {report.get('action_required_count', 0)}"
        )
        lines.append("")

        important = report.get("important_emails", [])
        if important:
            lines.append(f"🔴 重要邮件 ({len(important)} 封):")
            for item in important:
                if not isinstance(item, dict):
                    continue
                priority = str(item.get("priority", "low")).upper()
                icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(priority, "⚪")
                lines.append(f"  {icon} [{priority}] {item.get('title', '无标题')}")
                lines.append(f"     来自: {item.get('sender', '')}")
                if item.get("body_summary"):
                    lines.append(f"     摘要: {item.get('body_summary', '')}")
                if item.get("analysis_error"):
                    lines.append(f"     ⚠️ {item['analysis_error']}")
                elif item.get("action_needed"):
                    lines.append(f"     行动: {item.get('action_needed', '')}")
                lines.append("")

        action_items = report.get("action_items", [])
        if action_items:
            lines.append("⚡ 待办:")
            for item in action_items:
                task = ""
                deadline = ""
                if isinstance(item, dict):
                    task = item.get("task", "")
                    deadline = item.get("deadline", "")
                else:
                    task = str(item)
                if deadline and task:
                    lines.append(f"   ⏰ [{deadline}] {task}")
                elif task:
                    lines.append(f"   ⏰ {task}")
            lines.append("")

        lines.append(f"⏰ 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines)

    def _format_email_list(self, analyses: list[dict]) -> str:
        if not analyses:
            return "暂无邮件分析记录，请先发送 /扫描 触发一次扫描。"

        lines = [f"📧 邮件分析列表 (共 {len(analyses)} 封):", ""]
        for i, a in enumerate(analyses[-30:], 1):
            if not isinstance(a, dict):
                continue
            priority = str(a.get("priority", "low")).upper()
            # 标签特殊处理：
            # - "已完成"：原样保留 LOW 但标记已完成
            # - "代办"：强制提升为中优先级
            uid_str = str(a.get("uid", ""))
            user_tags = self.tag_store.get_tags(int(uid_str)) if uid_str.isdigit() else []
            effective_prio = priority
            tag_badges = ""
            if "代办" in user_tags:
                effective_prio = "MEDIUM"
            if "已完成" in user_tags:
                tag_badges = " ✅已完成"
            elif user_tags:
                tag_badges = " " + " ".join(f"[{t}]" for t in user_tags)

            icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(effective_prio, "⚪")
            title = str(a.get("title", "无标题"))[:30]
            sender = str(a.get("sender", ""))[:15]
            date = str(a.get("date", ""))[:10]
            summary = str(a.get("body_summary", ""))[:40]
            attach_mark = " 📎" if (a.get("has_attachment") or a.get("attachment_names")) else ""
            if a.get("analysis_error"):
                lines.append(f"{i}. ⚠️ [LOW] {title}{attach_mark}{tag_badges}（LLM 分析失败）")
                lines.append(f"   {sender} | {date}")
                lines.append(f"   原因: {str(a['analysis_error'])[:60]}")
            else:
                lines.append(f"{i}. {icon} [{effective_prio}] {title}{attach_mark}{tag_badges}")
                lines.append(f"   {sender} | {date}")
                if summary:
                    lines.append(f"   {summary}")
            lines.append("")

        lines.append("回复「邮件 <编号>」查看详情，回复「总结」查看汇总报告。")
        return "\n".join(lines)

    def _format_single_email(self, analysis) -> str:
        if not isinstance(analysis, dict):
            return "该条记录格式异常，无法显示。"
        lines = ["📄 邮件详情", "─" * 25]
        lines.append(f"标题: {analysis.get('title', '')}")
        lines.append(f"发件人: {analysis.get('sender', '')}")
        lines.append(f"日期: {analysis.get('date', '')}")
        lines.append(f"优先级: {analysis.get('priority', 'low')}")
        lines.append(f"重要: {'是' if analysis.get('is_important') else '否'}")
        lines.append(f"分类: {analysis.get('category', '其他')}")

        if analysis.get("analysis_error"):
            lines.append(f"⚠️ LLM 分析失败: {analysis['analysis_error']}")

        if analysis.get("body_summary"):
            lines.append(f"摘要: {analysis['body_summary']}")
        if analysis.get("key_points"):
            lines.append(f"要点: {'; '.join(analysis['key_points'][:3])}")
        if analysis.get("action_needed"):
            lines.append(f"行动: {analysis['action_needed']}")
        if analysis.get("action_deadline"):
            lines.append(f"截止: {analysis['action_deadline']}")
        if analysis.get("amounts"):
            lines.append(f"金额: {'; '.join(analysis['amounts'])}")
        if analysis.get("sentiment"):
            lines.append(f"情感: {analysis['sentiment']}")
        if analysis.get("tags"):
            lines.append(f"标签: {'; '.join(analysis['tags'])}")

        # 附件信息
        attachment_names = analysis.get("attachment_names") or []
        if attachment_names:
            lines.append("")
            lines.append("📎 附件:")
            for name in attachment_names:
                lines.append(f"   · {name}")
            lines.append("（附件文件已随本消息一并发送，如未收到请检查平台文件大小限制）")
        elif analysis.get("has_attachment"):
            lines.append("")
            lines.append("📎 该邮件声称含附件，但未能从 IMAP 提取到文件。")
        return "\n".join(lines)

    # ==================== 命令处理 ====================

    @filter.command("总结")
    async def cmd_summary(self, event: AstrMessageEvent):
        """生成汇总报告"""
        if not self.config.get("llm_api_key"):
            yield event.plain_result(
                "⚠️ 未配置 LLM API Key（llm_api_key），请先在插件配置中填写后再使用本命令。"
            )
            return
        busy = self._busy_hint()
        if busy:
            yield event.plain_result(f"⚠️ {busy}，请稍候再试。")
            return

        tracker = self._make_tracker("report", "⏳ 正在生成汇总报告...")
        tracker.set_callback(lambda m: event.send(MessageChain([Plain(m)])))

        report = await self._generate_report(tracker=tracker)
        if not report:
            await tracker.finish("⚠️ 暂无邮件分析记录或生成失败")
            yield event.plain_result("暂无邮件分析记录，请先发送 /扫描 触发扫描。")
        else:
            await tracker.finish("汇总报告已生成")
            yield event.plain_result(self._format_report(report))

    @filter.command("邮件列表")
    async def cmd_email_list(self, event: AstrMessageEvent):
        """查看邮件列表"""
        all_analyses = self.store.load_all()
        yield event.plain_result(self._format_email_list(all_analyses))

    @filter.command("邮件")
    async def cmd_email_detail(self, event: AstrMessageEvent):
        """查看指定邮件分析详情"""
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("格式: /邮件 <编号>")
            return

        # 处理 "/邮件 列表" 误入本命令的情况
        if args[1].strip() in ("列表", "list"):
            all_analyses = self.store.load_all()
            yield event.plain_result(self._format_email_list(all_analyses))
            return

        try:
            index = int(args[1])
            all_analyses = self.store.load_all()
            if 1 <= index <= len(all_analyses):
                analysis = all_analyses[-index]
                uid = analysis.get("uid", "")
                text = self._format_single_email(analysis)

                # 附件文件随详情消息一并发送（最多 5 个，避免刷屏/超限）
                file_components = self._build_attachment_components(uid, analysis)
                if file_components:
                    yield event.chain_result([Plain(text), *file_components])
                else:
                    yield event.plain_result(text)
            else:
                yield event.plain_result(f"当前只有 {len(all_analyses)} 封邮件记录")
        except ValueError:
            yield event.plain_result("格式: /邮件 <编号>")

    def _build_attachment_components(
        self, uid, analysis: dict, limit: int = 5
    ) -> list[File]:
        """根据分析记录中的附件路径构造 File 消息组件（跳过丢失/超限文件）。"""
        components: list[File] = []
        if not uid:
            return components
        paths = analysis.get("attachment_paths") or []
        for path in paths[:limit]:
            try:
                p = Path(path)
                if not p.exists() or p.stat().st_size > MAX_ATTACHMENT_SIZE:
                    continue
                components.append(File(name=p.name, file=str(p)))
            except Exception:
                continue
        return components

    @filter.command("扫描")
    async def cmd_scan(self, event: AstrMessageEvent):
        """手动触发扫描"""
        missing = self._missing_configs()
        if missing:
            yield event.plain_result(
                f"⚠️ 尚未配置: {', '.join(missing)}。请先在插件配置中填写。"
            )
            return
        if self._scan_lock.locked():
            yield event.plain_result("⚠️ 已有扫描/重新总结正在运行，请等待完成后再试。")
            return

        # 进度追踪：通过 event.send 实时推送进度消息
        tracker = self._make_tracker("scan", "⏳ 正在扫描邮箱并逐封分析...")
        tracker.set_callback(lambda m: event.send(MessageChain([Plain(m)])))

        report = await self._run_scan(push=False, tracker=tracker, with_report=True)
        stats = tracker.stats
        if report:
            yield event.plain_result(
                "✅ 扫描完成，以下是汇总报告：\n" + self._format_report(report)
            )
        elif stats.get("current", 0) > 0:
            yield event.plain_result("✅ 扫描完成（处理详情见上方进度消息）。")
        else:
            yield event.plain_result("✅ 扫描完成，没有新的邮件需要处理。")

    @filter.command("重新总结")
    async def cmd_resummarize(self, event: AstrMessageEvent):
        """重新总结邮件分析。

        默认只重新分析「未分析过」或「上次分析失败」的邮件；
        带参数「全部」（或 force/all）时，扫描范围内所有邮件无论是否已分析
        都强制重新分析并覆盖旧结果。完成后自动重新生成汇总报告。
        """
        missing = self._missing_configs()
        if missing:
            yield event.plain_result(
                f"⚠️ 尚未配置: {', '.join(missing)}。请先在插件配置中填写。"
            )
            return
        if self._scan_lock.locked():
            yield event.plain_result("⚠️ 已有扫描/重新总结正在运行，请等待完成后再试。")
            return

        args = event.message_str.strip().split()
        force = any(k in args for k in ("全部", "force", "all"))
        mode = "全部邮件（强制覆盖）" if force else "仅未总结/上次失败的邮件"

        tracker = self._make_tracker(
            "resummarize", f"⏳ 正在重新总结（{mode}）..."
        )
        tracker.set_callback(lambda m: event.send(MessageChain([Plain(m)])))

        result = await self._run_resummarize(force=force, tracker=tracker)
        if not result:
            yield event.plain_result(
                "⚠️ 重新总结未执行（配置不完整或已有任务在运行），请稍后重试。"
            )
            return

        lines = [
            "🔄 重新总结完成",
            f"  范围: {'全部邮件（强制覆盖）' if result.get('force') else '仅未总结/上次失败的邮件'}",
            f"  扫描邮件: {result.get('total', 0)} 封",
            f"  重新分析: {result.get('reanalyzed', 0)} 封",
            f"  跳过: {result.get('skipped', 0)} 封",
            f"  失败: {result.get('failed', 0)} 封",
            "",
        ]
        if result.get("network_aborted"):
            lines.append(
                "⏹️ 因连续网络/超时失败已中止剩余邮件。\n"
                "请检查 llm_api_key / llm_api_base / llm_model 配置与网络连通性，然后重试。"
            )
        elif result.get("report"):
            lines.append(self._format_report(result["report"]))
        else:
            lines.append(result.get("message") or "本次没有需要重新总结的邮件。")
        yield event.plain_result("\n".join(lines))

    @filter.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助"""
        yield event.plain_result(
            "📧 企业微信邮箱智能整理插件 v1.5.0\n"
            "──────────────\n"
            "可用命令：\n"
            "  /扫描 - 拉取邮箱新邮件并逐封 LLM 分析，完成后自动生成汇总报告\n"
            "  /总结 - 基于已分析结果生成汇总报告\n"
            "  /重新总结 - 重新分析未总结/上次失败的邮件\n"
            "  /重新总结 全部 - 强制重新分析范围内所有邮件并覆盖旧结果\n"
            "  /邮件列表 - 查看已分析邮件列表（最近 30 封，📎=含附件）\n"
            "  /邮件 <编号> - 查看指定邮件详情，附件文件会随消息一起发送\n"
            "  /帮助 - 显示本帮助\n"
            "──────────────\n"
            "使用说明：\n"
            "  · 定时任务按配置自动扫描（间隔模式 / 每日固定时间）\n"
            "  · 扫描/总结过程会实时推送进度条与预计剩余时间（ETA）\n"
            "  · 同一时间只允许一个扫描/重新总结任务，其余请求会被拒绝\n"
            "  · 邮件附件会自动提取保存，查看详情时随消息发送（单文件≤30MB）\n"
            "  · 分析失败的邮件会保存兜底结果，并在列表中标注 ⚠️\n"
            "  · 网页版「邮件看板」支持可视化进度，可在 AstrBot 插件页打开\n"
            "──────────────\n"
            "🏷️ 标签命令：\n"
            "  /标签 邮件编号 标签名 - 为邮件添加标签（如「已完成」「代办」）\n"
            "  /标签列表 - 查看所有有标签的邮件\n"
            "  /去标签 邮件编号 标签名 - 移除指定标签\n"
            "  /清标签 邮件编号 - 清除某邮件所有标签"
        )

    # ==================== 标签 QQ 命令 ====================

    @filter.command("标签")
    async def cmd_tag(self, event: AstrMessageEvent):
        """为邮件添加标签或查看标签列表。

        用法：
        - /标签 邮件编号 标签名 → 添加标签（如 /标签 1 已完成）
        - /标签 列表 → 查看所有有标签的邮件
        - 快捷标签：已完成、代办、重审、优先处理
        """
        args = event.message_str.strip().split()
        all_analyses = self.store.load_all()
        if len(all_analyses) == 0:
            yield event.plain_result("暂无邮件分析记录，请先发送 /扫描。")
            return

        # 处理 "/标签 列表" 或 "/标签 列表"
        if len(args) >= 2 and args[1].strip() in ("列表", "list", "查看"):
            yield event.plain_result(self._format_tag_list(all_analyses))
            return

        # 处理 "/标签 邮件编号 标签名" 或 "/标签 邮件编号 标签名1 标签名2 ..."
        if len(args) < 3:
            yield event.plain_result(
                "格式: /标签 邮件编号 标签名\n"
                "示例: /标签 1 已完成\n"
                "支持多标签: /标签 1 代办 重要\n"
                "快捷标签：已完成、代办、重审、优先处理"
            )
            return

        try:
            index = int(args[1])
        except ValueError:
            yield event.plain_result("格式: /标签 邮件编号 标签名")
            return

        if not (1 <= index <= len(all_analyses)):
            yield event.plain_result(f"当前只有 {len(all_analyses)} 封邮件记录")
            return

        analysis = all_analyses[-index]
        uid = analysis.get("uid", "")
        title = analysis.get("title", "无标题")[:30]

        # 从 args[2:] 取所有标签名
        tags_to_add = [a.strip() for a in args[2:]]
        for tag in tags_to_add:
            if not tag:
                continue
            self.tag_store.add_tag(uid, tag)
            logger.info(f"邮箱智能整理: 为邮件 {uid} 添加标签「{tag}」")

        # 显示当前所有标签
        current_user_tags = self.tag_store.get_tags(uid)
        status = "已添加 " if tags_to_add else "现有"
        tag_display = "、".join(current_user_tags) if current_user_tags else "（无）"
        yield event.plain_result(
            f"🏷️ {status}标签\n"
            f"邮件: {title[:30]}\n"
            f"标签: {tag_display}\n"
            f"提示: 使用「/去标签 {uid} 标签名」移除标签"
        )

    @filter.command("去标签")
    async def cmd_remove_tag(self, event: AstrMessageEvent):
        """移除指定邮件的标签。

        用法：/去标签 邮件编号 标签名
        示例: /去标签 1 已完成
        """
        args = event.message_str.strip().split()
        if len(args) < 3:
            yield event.plain_result(
                "格式: /去标签 邮件编号 标签名\n"
                "示例: /去标签 1 已完成"
            )
            return

        try:
            index = int(args[1])
        except ValueError:
            yield event.plain_result("格式: /去标签 邮件编号 标签名")
            return

        all_analyses = self.store.load_all()
        if not (1 <= index <= len(all_analyses)):
            yield event.plain_result(f"当前只有 {len(all_analyses)} 封邮件记录")
            return

        analysis = all_analyses[-index]
        uid = analysis.get("uid", "")
        title = analysis.get("title", "无标题")[:30]
        tag = args[2].strip()

        if self.tag_store.has_tag(uid, tag):
            self.tag_store.remove_tag(uid, tag)
            logger.info(f"邮箱智能整理: 移除邮件 {uid} 的标签「{tag}」")
            yield event.plain_result(
                f"✅ 已移除标签「{tag}」\n邮件: {title[:30]}"
            )
        else:
            current_tags = self.tag_store.get_tags(uid)
            if current_tags:
                yield event.plain_result(
                    f"⚠️ 该邮件没有「{tag}」标签\n"
                    f"现有标签: {', '.join(current_tags)}"
                )
            else:
                yield event.plain_result(f"该邮件没有用户标签")

    @filter.command("清标签")
    async def cmd_clear_tags(self, event: AstrMessageEvent):
        """清除指定邮件的所有用户标签。

        用法: /清标签 邮件编号
        """
        args = event.message_str.strip().split()
        if len(args) < 2:
            yield event.plain_result("格式: /清标签 邮件编号")
            return

        try:
            index = int(args[1])
        except ValueError:
            yield event.plain_result("格式: /清标签 邮件编号")
            return

        all_analyses = self.store.load_all()
        if not (1 <= index <= len(all_analyses)):
            yield event.plain_result(f"当前只有 {len(all_analyses)} 封邮件记录")
            return

        analysis = all_analyses[-index]
        uid = analysis.get("uid", "")
        title = analysis.get("title", "无标题")[:30]

        current_tags = self.tag_store.get_tags(uid)
        if current_tags:
            self.tag_store.clear_tags(uid)
            logger.info(f"邮箱智能整理: 清除邮件 {uid} 的所有标签: {', '.join(current_tags)}")
            yield event.plain_result(
                f"✅ 已清除所有标签\n邮件: {title[:30]}（原标签: {', '.join(current_tags)}）"
            )
        else:
            yield event.plain_result("该邮件没有用户标签")

    def _format_tag_list(self, all_analyses: list[dict]) -> str:
        """格式化显示所有有标签的邮件。

        特殊处理：
        - "已完成"标签：该邮件不再出现在待办/高优列表中（汇总报告中隐藏）
        - "代办"标签：即使 LLM 判定为 Low 也会按中优先级展示
        - 其他标签：仅用于筛选展示
        """
        tagged_uids = self.tag_store.get_tagged_uids()
        if not tagged_uids:
            return "🏷️ 暂无标记标签的邮件\n可使用 /标签 邮件编号 标签名 添加"

        # 构建 uid → 分析结果的映射
        analysis_map = {
            str(a.get("uid", "")): a for a in all_analyses if isinstance(a, dict)
        }

        lines = ["🏷️ 已标记标签的邮件:", ""]
        done_uids = set(self.tag_store.get_done_uids())
        todo_uids = set(self.tag_store.get_todo_uids())

        for uid in tagged_uids:
            analysis = analysis_map.get(uid)
            if not analysis or not isinstance(analysis, dict):
                continue

            title = analysis.get("title", "无标题")[:30]
            sender = analysis.get("sender", "")[:15]
            date = analysis.get("date", "")[:10]
            prio = str(analysis.get("priority", "low")).upper()
            user_tags = self.tag_store.get_tags(int(uid))
            if not user_tags:
                continue

            # 优先级特殊处理
            effective_prio = prio
            tag_info = ""
            if uid in done_uids:
                tag_info = "🟢[已完成]"
            elif uid in todo_uids:
                effective_prio = "MEDIUM"  # 代办标签强制提升为中优先级
                tag_info = "🏷️[代办]"
            else:
                tag_info = " ".join(f"[{t}]" for t in user_tags)

            icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(effective_prio, "⚪")
            lines.append(f"  {icon} {title} {tag_info}")
            lines.append(f"     来自: {sender} | {date} | 标签: {', '.join(user_tags)}")
            lines.append("")

        lines.append("提示: 可使用「已完成」「代办」等特殊标签")
        lines.append("  - 已完成：不再显示在待办/高优列表中")
        lines.append("  - 代办：即使低优先级也会按中优先级展示")
        return "\n".join(lines)
