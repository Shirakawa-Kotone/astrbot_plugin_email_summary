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
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .core.imap_fetcher import IMAPFetcher, StateManager
from .core.email_analyzer import EmailAnalyzer, AnalysisStore
from .core.summary_reporter import SummaryReporter

PLUGIN_DIR_NAME = "email_summary_assistant"

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
    """读取 POST JSON body（兼容 v4.24.x quart 与 v4.27 astrbot.api.web）。

    无论哪个版本，通过 context.register_web_api() 注册的 handler 都运行在
    Quart 兼容请求上下文，因此优先使用 quart 的 request.get_json()。
    """
    import inspect

    payload = None
    try:
        from quart import request as _q_request

        fn = getattr(_q_request, "get_json", None)
        if callable(fn):
            if inspect.iscoroutinefunction(fn):
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
                res = await fn() if inspect.iscoroutinefunction(fn) else fn()
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
    "1.1.0",
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

        # 核心组件（在 initialize 中构建）
        self.fetcher: Optional[IMAPFetcher] = None
        self.analyzer: Optional[EmailAnalyzer] = None
        self.reporter: Optional[SummaryReporter] = None
        self.store = AnalysisStore(str(self.data_dir))
        self.state_manager = StateManager(str(self.data_dir))

        # 定时调度器（initialize 中创建并启动）
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._scan_lock = asyncio.Lock()

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
        """生成汇总报告（需要 LLM Key）"""
        if not self.config.get("llm_api_key"):
            return _web_err("未配置 LLM API Key（llm_api_key），请先在插件配置中填写。")
        report = await self._generate_report()
        if not report:
            return _web_err("暂无邮件分析记录，请先触发一次 /扫描。")
        return _web_ok(report)

    async def _web_api_scan(self):
        """POST → 立即触发一次扫描（后台执行，不等待）"""
        missing = [
            name
            for name, key in (
                ("email_address", "email_address"),
                ("email_auth_code", "email_auth_code"),
                ("llm_api_key", "llm_api_key"),
            )
            if not self.config.get(key)
        ]
        if missing:
            return _web_err(f"尚未配置: {', '.join(missing)}。请先在插件配置中填写。")
        asyncio.create_task(self._run_scan(push=False))
        return _web_ok({"message": "扫描已开始，请稍后刷新查看结果。"})

    async def _web_api_resummarize(self):
        """POST {force: bool} → 重新总结（后台执行，不等待）。

        force=false（默认）：只重新分析「未分析过」或「上次分析失败」的邮件；
        force=true：范围内所有邮件无论是否已分析都重新分析并覆盖旧结果。
        重新分析完成后会自动重新生成汇总报告，可在网页上点击「生成汇总」查看。
        """
        missing = [
            name
            for name, key in (
                ("email_address", "email_address"),
                ("email_auth_code", "email_auth_code"),
                ("llm_api_key", "llm_api_key"),
            )
            if not self.config.get(key)
        ]
        if missing:
            return _web_err(f"尚未配置: {', '.join(missing)}。请先在插件配置中填写。")
        try:
            body = await _web_body_json({})
        except Exception:
            body = {}
        force = bool(body.get("force", False))
        mode = "全部邮件（强制覆盖）" if force else "仅未总结/上次失败的邮件"
        asyncio.create_task(self._run_resummarize(force=force))
        return _web_ok(
            {"message": f"重新总结已开始（{mode}），完成后请点击「📊 生成汇总」查看最新报告。"}
        )

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
        )
        api_key = self.config.get("llm_api_key", "")
        api_base = self.config.get("llm_api_base", "https://api.openai.com/v1")
        model = self.config.get("llm_model", "gpt-4o-mini")

        self.analyzer = EmailAnalyzer(api_key=api_key, api_base=api_base, model=model)
        self.reporter = SummaryReporter(
            api_key=api_key,
            api_base=api_base,
            model=model,
            mode=self.config.get("summary_mode", "balanced"),
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

    # ==================== 核心扫描逻辑 ====================

    async def _run_scan(self, push: bool = True) -> Optional[dict]:
        """执行一次完整扫描周期：拉取 → 逐封分析 → 汇总推送"""
        if self._scan_lock.locked():
            logger.warning("邮箱智能整理: 已有扫描在运行，跳过本次")
            return None

        async with self._scan_lock:
            if not self.fetcher:
                self._init_components()

            # 配置完整性检查
            if not self.config.get("email_address") or not self.config.get(
                "email_auth_code"
            ):
                logger.warning("邮箱智能整理: 邮箱配置不完整，跳过扫描")
                return None
            if not self.config.get("llm_api_key"):
                logger.warning("邮箱智能整理: LLM API Key 未配置，跳过扫描")
                return None

            logger.info(f"开始扫描 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

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
                return None

            # 2. 过滤已处理
            state = self.state_manager.load()
            processed_uids = state.get("processed_uids", set())
            new_emails = [e for e in emails if e.uid not in processed_uids]

            if not new_emails:
                logger.info("邮箱智能整理: 没有新邮件")
                self.state_manager.save(
                    datetime.now().isoformat(), processed_uids, []
                )
                return None

            logger.info(f"发现 {len(new_emails)} 封新邮件，逐封 LLM 分析...")

            # 3. 逐封分析（串行避免限流）
            new_uids = set(processed_uids)
            failed_count = 0
            for email in new_emails:
                try:
                    analysis = await asyncio.to_thread(
                        self.analyzer.analyze,
                        email.subject,
                        email.sender,
                        email.date,
                        email.body_text,
                    )
                    if analysis.get("analysis_error"):
                        failed_count += 1
                        logger.error(
                            f"邮件 {email.uid} LLM 分析失败: {analysis['analysis_error']}"
                        )
                    self.store.save(email.uid, analysis)
                    new_uids.add(email.uid)
                    logger.info(
                        f"邮件 {email.uid} 分析完成: "
                        f"{analysis.get('title', '')[:30]} "
                        f"[{analysis.get('priority', '')}]"
                    )
                except Exception as e:
                    failed_count += 1
                    logger.error(f"邮件 {email.uid} 分析失败: {e}")

            # 4. 更新状态
            self.state_manager.save(datetime.now().isoformat(), new_uids, [])
            if failed_count:
                logger.warning(
                    f"邮箱智能整理: 本次 {failed_count}/{len(new_emails)} 封邮件 LLM 分析失败，"
                    "已保存兜底结果（优先级标记为 low）。请检查 llm_api_key / llm_api_base / "
                    "llm_model 配置及网络连通性。"
                )

            # 5. 汇总推送
            if push and self.config.get("push_enabled", True):
                report = await self._generate_report()
                if report:
                    await self._push_to_target(report)
                    return report
            return None

    async def _run_resummarize(self, force: bool = False) -> Optional[dict]:
        """重新总结已扫描范围内的邮件（逐封重新调用 LLM 分析并覆盖旧结果）。

        force=False（默认）：只重新分析「未分析过」或「上次分析失败（带 analysis_error）」
        的邮件（即“没有总结的在总结一遍”）；
        force=True：范围内所有邮件，无论是否已分析，全部强制重新分析覆盖。

        重新分析完成后自动重新生成一次汇总报告（不主动推送，由调用方展示）。
        返回统计信息与最新报告，任何异常/配置缺失返回 None。
        """
        if self._scan_lock.locked():
            logger.warning("邮箱智能整理: 已有扫描/重新总结在运行，跳过本次")
            return None

        async with self._scan_lock:
            if not self.fetcher:
                self._init_components()

            # 配置完整性检查
            if not self.config.get("email_address") or not self.config.get(
                "email_auth_code"
            ):
                logger.warning("邮箱智能整理: 邮箱配置不完整，跳过重新总结")
                return None
            if not self.config.get("llm_api_key"):
                logger.warning("邮箱智能整理: LLM API Key 未配置，跳过重新总结")
                return None

            logger.info(
                f"开始重新总结（force={force}）- "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

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
                return None

            if not emails:
                logger.info("邮箱智能整理: 重新总结范围内没有邮件")
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
            for email in emails:
                existing = self.store.load(email.uid)
                need = force or existing is None or bool(existing.get("analysis_error"))
                if not need:
                    skipped += 1
                    continue
                try:
                    analysis = await asyncio.to_thread(
                        self.analyzer.analyze,
                        email.subject,
                        email.sender,
                        email.date,
                        email.body_text,
                    )
                    if analysis.get("analysis_error"):
                        failed += 1
                        logger.error(
                            f"邮件 {email.uid} 重新分析失败: {analysis['analysis_error']}"
                        )
                    self.store.save(email.uid, analysis)
                    new_uids.add(email.uid)
                    reanalyzed += 1
                    logger.info(
                        f"邮件 {email.uid} 重新分析完成: "
                        f"{analysis.get('title', '')[:30]} [{analysis.get('priority', '')}]"
                    )
                except Exception as e:
                    failed += 1
                    logger.error(f"邮件 {email.uid} 重新分析失败: {e}")

            # 3. 更新已处理状态（本次涉及的邮件都标记为已处理，避免重复扫描）
            self.state_manager.save(datetime.now().isoformat(), new_uids, [])

            result = {
                "total": len(emails),
                "reanalyzed": reanalyzed,
                "skipped": skipped,
                "failed": failed,
                "force": force,
                "report": None,
            }

            # 4. 若有重新分析，自动重新生成汇总报告
            if reanalyzed:
                report = await self._generate_report()
                result["report"] = report

            logger.info(
                f"邮箱智能整理: 重新总结完成 共{len(emails)}封，"
                f"重新分析{reanalyzed}封，跳过{skipped}封，失败{failed}封"
            )
            return result

    async def _generate_report(self) -> Optional[dict]:
        """生成汇总报告"""
        if not self.reporter:
            self._init_components()

        if not self.config.get("llm_api_key"):
            logger.warning("邮箱智能整理: 未配置 llm_api_key，跳过报告生成")
            return None

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
                if item.get("action_needed"):
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
            icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(priority, "⚪")
            title = str(a.get("title", "无标题"))[:30]
            sender = str(a.get("sender", ""))[:15]
            date = str(a.get("date", ""))[:10]
            summary = str(a.get("body_summary", ""))[:40]
            if a.get("analysis_error"):
                lines.append(f"{i}. ⚠️ [LOW] {title}（LLM 分析失败）")
                lines.append(f"   {sender} | {date}")
                lines.append(f"   原因: {str(a['analysis_error'])[:60]}")
            else:
                lines.append(f"{i}. {icon} [{priority}] {title}")
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
        yield event.plain_result("⏳ 正在生成汇总报告...")

        report = await self._generate_report()
        if not report:
            yield event.plain_result("暂无邮件分析记录，请先发送 /扫描 触发扫描。")
        else:
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
                yield event.plain_result(self._format_single_email(analysis))
            else:
                yield event.plain_result(f"当前只有 {len(all_analyses)} 封邮件记录")
        except ValueError:
            yield event.plain_result("格式: /邮件 <编号>")

    @filter.command("扫描")
    async def cmd_scan(self, event: AstrMessageEvent):
        """手动触发扫描"""
        missing = [
            name
            for name, key in (
                ("email_address", "email_address"),
                ("email_auth_code", "email_auth_code"),
                ("llm_api_key", "llm_api_key"),
            )
            if not self.config.get(key)
        ]
        if missing:
            yield event.plain_result(
                f"⚠️ 尚未配置: {', '.join(missing)}。请先在插件配置中填写。"
            )
            return
        yield event.plain_result("⏳ 正在扫描邮箱并逐封分析，请稍候...")

        report = await self._run_scan(push=False)
        if report:
            yield event.plain_result(
                "✅ 扫描完成，以下是汇总报告：\n" + self._format_report(report)
            )
        else:
            yield event.plain_result("✅ 扫描完成，没有新的邮件需要处理。")

    @filter.command("重新总结")
    async def cmd_resummarize(self, event: AstrMessageEvent):
        """重新总结邮件分析。

        默认只重新分析「未分析过」或「上次分析失败」的邮件；
        带参数「全部」（或 force/all）时，扫描范围内所有邮件无论是否已分析
        都强制重新分析并覆盖旧结果。完成后自动重新生成汇总报告。
        """
        missing = [
            name
            for name, key in (
                ("email_address", "email_address"),
                ("email_auth_code", "email_auth_code"),
                ("llm_api_key", "llm_api_key"),
            )
            if not self.config.get(key)
        ]
        if missing:
            yield event.plain_result(
                f"⚠️ 尚未配置: {', '.join(missing)}。请先在插件配置中填写。"
            )
            return

        args = event.message_str.strip().split()
        force = any(k in args for k in ("全部", "force", "all"))
        mode = "全部邮件（强制覆盖）" if force else "仅未总结/上次失败的邮件"
        yield event.plain_result(f"⏳ 正在重新总结（{mode}），请稍候...")

        result = await self._run_resummarize(force=force)
        if not result:
            yield event.plain_result(
                "⚠️ 重新总结未执行（配置不完整或已有扫描/重新总结在运行），请稍后重试。"
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
        if result.get("report"):
            lines.append(self._format_report(result["report"]))
        else:
            lines.append(result.get("message") or "本次没有需要重新总结的邮件。")
        yield event.plain_result("\n".join(lines))

    @filter.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助"""
        yield event.plain_result(
            "📧 企业微信邮箱智能整理插件\n"
            "可用命令：\n"
            "  /总结 - 生成邮件汇总报告\n"
            "  /邮件列表 - 查看已分析邮件列表\n"
            "  /邮件 <编号> - 查看指定邮件详情\n"
            "  /扫描 - 立即触发一次邮箱扫描\n"
            "  /重新总结 - 重新分析未总结/上次失败的邮件\n"
            "  /重新总结 全部 - 强制重新分析所有邮件并覆盖旧结果\n"
            "  /帮助 - 显示本帮助"
        )
