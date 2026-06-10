"""
分析服务

封装 TradingAgentsGraph，提供异步分析任务管理。
"""
import uuid
import threading
import logging
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AnalysisTask:
    """分析任务"""
    task_id: str
    user_id: str
    ticker: str
    ticker_name: str
    date: str
    status: TaskStatus = TaskStatus.PENDING
    progress: Dict = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    result: Optional[Dict] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    log_file: Optional[str] = None  # message_tool.log 文件路径
    cancelled: bool = False  # 取消标志

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "ticker": self.ticker,
            "ticker_name": self.ticker_name,
            "date": self.date,
            "status": self.status.value,
            "progress": self.progress,
            "logs": self.logs[-50:],  # 只返回最近50条日志
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at
        }


class AnalysisService:
    """分析服务"""

    # 分析步骤定义（用于进度追踪）
    ANALYSIS_STEPS = [
        ("market_analyst", "市场分析师"),
        ("social_analyst", "情绪分析师"),
        ("news_analyst", "新闻分析师"),
        ("fundamentals_analyst", "基本面分析师"),
        ("bull_researcher", "看涨研究员"),
        ("bear_researcher", "看跌研究员"),
        ("research_manager", "研究主管"),
        ("risky_manager", "激进风控"),
        ("conservative_manager", "保守风控"),
        ("neutral_manager", "中立风控"),
        ("risk_manager", "风险主管"),
        ("consolidation", "综合报告"),
    ]

    def __init__(self):
        """初始化分析服务"""
        self._tasks: Dict[str, AnalysisTask] = {}
        self._user_tasks: Dict[str, List[str]] = {}  # user_id -> [task_ids]
        self._lock = threading.Lock()

    def start_analysis(
        self,
        user_id: str,
        ticker: str,
        ticker_name: str = "",
        date: Optional[str] = None
    ) -> str:
        """
        启动分析任务

        Args:
            user_id: 用户 ID
            ticker: 股票代码
            ticker_name: 股票名称
            date: 分析日期（默认今天）

        Returns:
            task_id: 任务 ID
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        task_id = str(uuid.uuid4())[:8]

        # 创建任务
        task = AnalysisTask(
            task_id=task_id,
            user_id=user_id,
            ticker=ticker,
            ticker_name=ticker_name or ticker,
            date=date,
            progress={
                "current_step": None,
                "current_step_name": None,
                "completed_steps": [],
                "total_steps": len(self.ANALYSIS_STEPS)
            }
        )

        with self._lock:
            self._tasks[task_id] = task
            if user_id not in self._user_tasks:
                self._user_tasks[user_id] = []
            self._user_tasks[user_id].append(task_id)

        # 在后台线程中运行分析
        thread = threading.Thread(
            target=self._run_analysis,
            args=(task_id,),
            daemon=True
        )
        thread.start()

        logger.info(f"分析任务已启动: {task_id} - {ticker} ({date})")
        return task_id

    # 节点名称到步骤 key 的映射
    NODE_TO_STEP = {
        "Market Analyst": "market_analyst",
        "Social Analyst": "social_analyst",
        "News Analyst": "news_analyst",
        "Fundamentals Analyst": "fundamentals_analyst",
        "Bull Researcher": "bull_researcher",
        "Bear Researcher": "bear_researcher",
        "Research Manager": "research_manager",
        "Trader": "trader",
        "Risky Analyst": "risky_manager",
        "Safe Analyst": "conservative_manager",
        "Neutral Analyst": "neutral_manager",
        "Risk Judge": "risk_manager",
        "Consolidation Report": "consolidation",
    }

    def _init_results_dir(self, task_id: str) -> tuple:
        """初始化结果目录和文件（与 CLI 行为一致）"""
        from pathlib import Path
        import csv

        task = self._tasks.get(task_id)
        if not task:
            return None, None, None

        # 剥离市场后缀（.SZ/.SH），与 CLI 行为一致
        ticker_for_path = task.ticker.split('.')[0] if '.' in task.ticker else task.ticker

        # 创建目录结构
        # Docker 容器内: /app/app/services/analysis_service.py → 3个parent → /app → /app/results
        project_dir = Path(__file__).parent.parent.parent
        results_dir = project_dir / "results" / ticker_for_path / task.date
        report_dir = results_dir / "reports"
        results_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)

        # 创建 message_tool.log（与 CLI 一致）
        log_file = results_dir / "message_tool.log"
        log_file.touch(exist_ok=True)

        # tool_data.csv 路径（由 ToolDataLogger 创建和管理）
        tool_data_csv = results_dir / "tool_data.csv"

        return results_dir, log_file, tool_data_csv

    def _run_analysis(self, task_id: str):
        """在后台运行分析（使用流式模式跟踪进度）"""
        task = self._tasks.get(task_id)
        if not task:
            logger.warning(f"任务不存在: {task_id}")
            return

        try:
            task.status = TaskStatus.RUNNING

            # 初始化结果目录和文件（与 CLI 一致）
            results_dir, log_file, tool_data_csv = self._init_results_dir(task_id)
            if log_file:
                task.log_file = str(log_file)  # 保存日志文件路径到 task

            # 开始日志（log_file 已设置，会同时写入文件）
            self._add_log(task_id, f"开始分析 {task.ticker_name} ({task.ticker})")

            # 延迟导入，避免启动时加载
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.utils.data_logger import ToolDataLogger
            from langchain_core.messages import ToolMessage

            # 创建工具数据记录器（与 CLI 一致）
            ticker_for_path = task.ticker.split('.')[0] if '.' in task.ticker else task.ticker
            data_logger = ToolDataLogger(tool_data_csv, ticker_for_path)

            config = DEFAULT_CONFIG.copy()
            # Docker 环境下使用挂载的 named volume；本地/普通服务器优先使用 .env 或默认配置。
            if os.path.exists("/app"):
                config["chroma_db_path"] = "/app/chroma_db"

            if not os.getenv("TUSHARE_TOKEN"):
                selected_analysts = ["market", "social", "news", "fundamentals"]
                config["deep_think_llm"] = config.get("quick_think_llm", "deepseek-chat")
                config["max_tokens"] = 2000
                self._add_log(
                    task_id,
                    "未配置 TUSHARE_TOKEN，启用 AKShare 替代数据模式：运行市场、情绪、新闻、基本面分析师，并跳过 Tushare 专属字段。"
                )
            else:
                selected_analysts = ["market", "social", "news", "fundamentals"]

            # 创建 Graph
            self._add_log(task_id, "初始化分析系统...")
            trading_graph = TradingAgentsGraph(
                config=config,
                selected_analysts=selected_analysts
            )

            # 使用流式模式运行，跟踪每个节点的进度
            self._add_log(task_id, f"开始执行分析流程...")

            # 初始化状态
            init_state = trading_graph.propagator.create_initial_state(
                task.ticker, task.date
            )
            args = trading_graph.propagator.get_graph_args()

            # 流式执行，捕获每个节点完成
            # 注意：使用 stream_mode="values" 时，每个 chunk 是完整的状态快照
            # 与 CLI 一致，使用 trace 列表保存所有 chunks，最后一个是最终状态
            trace = []

            # 设置初始当前步骤
            self._set_current_step(task_id, "market_analyst", "市场分析师")
            self._add_log(task_id, "📊 市场分析师开始分析...")

            # 使用 stream_mode="values"（与 CLI 一致），通过检测报告内容变化追踪进度
            chunk_count = 0
            # 追踪已完成的报告
            completed_reports = set()

            for chunk in trading_graph.graph.stream(init_state, **args):
                # 检查取消标志
                if task.cancelled:
                    logger.info(f"任务 {task_id} 检测到取消标志，正在退出...")
                    task.status = TaskStatus.FAILED
                    task.error = "用户取消"
                    task.completed_at = datetime.now().isoformat()
                    self._add_log(task_id, "⚠️ 分析已被用户取消")
                    return

                chunk_count += 1
                # 保存 chunk 到 trace（与 CLI 一致）
                trace.append(chunk)

                # 调试：打印每个 chunk 包含的 keys（使用 print 确保输出）
                chunk_keys = list(chunk.keys()) if isinstance(chunk, dict) else ["not_a_dict"]
                print(f"[DEBUG] 任务 {task_id}: chunk#{chunk_count} keys={chunk_keys}", flush=True)
                self._add_log(task_id, f"📦 chunk#{chunk_count}: {len(chunk_keys)} keys")

                # 检查关键报告字段
                report_status = []
                if "market_report" in chunk and chunk["market_report"]:
                    report_status.append("market✓")
                if "sentiment_report" in chunk and chunk["sentiment_report"]:
                    report_status.append("sentiment✓")
                if "news_report" in chunk and chunk["news_report"]:
                    report_status.append("news✓")
                if "fundamentals_report" in chunk and chunk["fundamentals_report"]:
                    report_status.append("fundamentals✓")
                if report_status:
                    print(f"[DEBUG] 任务 {task_id}: chunk#{chunk_count} 报告状态: {report_status}", flush=True)

                # 记录工具调用（与 CLI 一致）
                if "messages" in chunk and chunk["messages"]:
                    for message in chunk["messages"]:
                        # 检测工具调用（AIMessage 中的 tool_calls）
                        if hasattr(message, "tool_calls") and message.tool_calls:
                            for tool_call in message.tool_calls:
                                tool_name = tool_call["name"] if isinstance(tool_call, dict) else tool_call.name
                                tool_args = tool_call.get("args", {}) if isinstance(tool_call, dict) else getattr(tool_call, 'args', {})
                                tool_call_id = tool_call.get("id", "") if isinstance(tool_call, dict) else getattr(tool_call, 'id', '')
                                # 注册工具调用到 data_logger
                                data_logger.register_tool_call(tool_call_id, tool_name, tool_args)
                                self._add_log(task_id, f"🔧 调用工具: {tool_name}")

                        # 检测工具返回结果（ToolMessage）- 记录到 CSV
                        if isinstance(message, ToolMessage):
                            tool_call_id = message.tool_call_id
                            result_content = message.content if isinstance(message.content, str) else str(message.content)
                            data_logger.log_tool_result(tool_call_id, result_content)

                # 检测报告完成并实时保存（与 CLI 相同的逻辑）
                # 市场分析师
                if "market_report" in chunk and chunk["market_report"] and "market_analyst" not in completed_reports:
                    print(f"[PROGRESS] 任务 {task_id}: 🎯 检测到市场分析师完成!", flush=True)
                    completed_reports.add("market_analyst")
                    self._update_progress(task_id, "market_analyst")
                    self._add_log(task_id, "✓ 市场分析师完成")
                    # 实时保存报告
                    self._save_report_realtime(task_id, "market_report", chunk["market_report"], "market_report.md")
                    # 设置下一步
                    self._set_current_step(task_id, "social_analyst", "情绪分析师")
                    self._add_log(task_id, "📊 情绪分析师开始分析...")

                # 情绪分析师
                if "sentiment_report" in chunk and chunk["sentiment_report"] and "social_analyst" not in completed_reports:
                    print(f"[PROGRESS] 任务 {task_id}: 🎯 检测到情绪分析师完成!", flush=True)
                    completed_reports.add("social_analyst")
                    self._update_progress(task_id, "social_analyst")
                    self._add_log(task_id, "✓ 情绪分析师完成")
                    # 实时保存报告
                    self._save_report_realtime(task_id, "sentiment_report", chunk["sentiment_report"], "sentiment_report.md")
                    self._set_current_step(task_id, "news_analyst", "新闻分析师")
                    self._add_log(task_id, "📰 新闻分析师开始分析...")

                # 新闻分析师
                if "news_report" in chunk and chunk["news_report"] and "news_analyst" not in completed_reports:
                    print(f"[PROGRESS] 任务 {task_id}: 🎯 检测到新闻分析师完成!", flush=True)
                    completed_reports.add("news_analyst")
                    self._update_progress(task_id, "news_analyst")
                    self._add_log(task_id, "✓ 新闻分析师完成")
                    # 实时保存报告
                    self._save_report_realtime(task_id, "news_report", chunk["news_report"], "news_report.md")
                    self._set_current_step(task_id, "fundamentals_analyst", "基本面分析师")
                    self._add_log(task_id, "📈 基本面分析师开始分析...")

                # 基本面分析师
                if "fundamentals_report" in chunk and chunk["fundamentals_report"] and "fundamentals_analyst" not in completed_reports:
                    print(f"[PROGRESS] 任务 {task_id}: 🎯 检测到基本面分析师完成!", flush=True)
                    completed_reports.add("fundamentals_analyst")
                    self._update_progress(task_id, "fundamentals_analyst")
                    self._add_log(task_id, "✓ 基本面分析师完成")
                    # 实时保存报告
                    self._save_report_realtime(task_id, "fundamentals_report", chunk["fundamentals_report"], "fundamentals_report.md")
                    self._set_current_step(task_id, "bull_researcher", "看涨研究员")
                    self._add_log(task_id, "🔬 研究团队开始辩论...")

                # 研究团队（通过 investment_debate_state 追踪）
                if "investment_debate_state" in chunk and chunk["investment_debate_state"]:
                    debate = chunk["investment_debate_state"]
                    if debate.get("bull_history") and "bull_researcher" not in completed_reports:
                        completed_reports.add("bull_researcher")
                        self._update_progress(task_id, "bull_researcher")
                        self._add_log(task_id, "✓ 看涨研究员完成")
                        self._set_current_step(task_id, "bear_researcher", "看跌研究员")
                    if debate.get("bear_history") and "bear_researcher" not in completed_reports:
                        completed_reports.add("bear_researcher")
                        self._update_progress(task_id, "bear_researcher")
                        self._add_log(task_id, "✓ 看跌研究员完成")
                        self._set_current_step(task_id, "research_manager", "研究主管")
                    if debate.get("judge_decision") and "research_manager" not in completed_reports:
                        completed_reports.add("research_manager")
                        self._update_progress(task_id, "research_manager")
                        self._add_log(task_id, "✓ 研究主管完成")
                        # 保存研究结论报告（供预览使用）
                        research_content = self._format_research_report(debate)
                        self._save_report_realtime(task_id, "research_report", research_content, "research_report.md")
                        self._set_current_step(task_id, "risky_manager", "激进风控")
                        self._add_log(task_id, "🛡️ 风控团队开始评估...")

                # 风险管理团队（通过 risk_debate_state 追踪）
                if "risk_debate_state" in chunk and chunk["risk_debate_state"]:
                    risk = chunk["risk_debate_state"]
                    if risk.get("risky_history") and "risky_manager" not in completed_reports:
                        completed_reports.add("risky_manager")
                        self._update_progress(task_id, "risky_manager")
                        self._add_log(task_id, "✓ 激进风控完成")
                        self._set_current_step(task_id, "conservative_manager", "保守风控")
                    if risk.get("safe_history") and "conservative_manager" not in completed_reports:
                        completed_reports.add("conservative_manager")
                        self._update_progress(task_id, "conservative_manager")
                        self._add_log(task_id, "✓ 保守风控完成")
                        self._set_current_step(task_id, "neutral_manager", "中立风控")
                    if risk.get("neutral_history") and "neutral_manager" not in completed_reports:
                        completed_reports.add("neutral_manager")
                        self._update_progress(task_id, "neutral_manager")
                        self._add_log(task_id, "✓ 中立风控完成")
                        self._set_current_step(task_id, "risk_manager", "风险主管")
                    if risk.get("judge_decision") and "risk_manager" not in completed_reports:
                        completed_reports.add("risk_manager")
                        self._update_progress(task_id, "risk_manager")
                        self._add_log(task_id, "✓ 风险主管完成")
                        # 保存风控评估报告（供预览使用）
                        risk_content = self._format_risk_report(risk)
                        self._save_report_realtime(task_id, "risk_report", risk_content, "risk_report.md")
                        self._set_current_step(task_id, "consolidation", "综合报告")
                        self._add_log(task_id, "📝 正在生成综合报告...")

                # 综合报告
                if "consolidation_report" in chunk and chunk["consolidation_report"] and "consolidation" not in completed_reports:
                    completed_reports.add("consolidation")
                    self._update_progress(task_id, "consolidation")
                    self._add_log(task_id, "✓ 综合报告生成完成")
                    # 实时保存综合报告
                    self._save_report_realtime(task_id, "consolidation_report", chunk["consolidation_report"], "consolidation_report.md")

            logger.info(f"分析任务 {task_id}: graph.stream() 完成, 共 {chunk_count} 个 chunks")
            self._add_log(task_id, f"流式执行完成，共 {chunk_count} 个 chunks")

            # 使用最后一个 chunk 作为最终状态（与 CLI 一致）
            if not trace:
                raise ValueError("流式执行未返回任何 chunk")

            full_final_state = trace[-1]
            trading_graph.ticker = task.ticker
            trading_graph.curr_state = full_final_state

            # 调试：打印状态中的关键字段
            state_keys = list(full_final_state.keys())
            logger.info(f"分析任务 {task_id}: 最终状态包含 keys: {state_keys}")
            self._add_log(task_id, f"状态 keys: {len(state_keys)} 个")

            # 检查 final_trade_decision 是否存在
            if "final_trade_decision" not in full_final_state:
                logger.warning(f"分析任务 {task_id}: final_trade_decision 不存在于状态中")
                self._add_log(task_id, "⚠️ 警告: final_trade_decision 缺失，尝试从报告提取")
                # 尝试从 consolidation_report 或 risk_debate_state 中提取
                if "risk_debate_state" in full_final_state and full_final_state["risk_debate_state"]:
                    risk_state = full_final_state["risk_debate_state"]
                    if risk_state.get("judge_decision"):
                        full_final_state["final_trade_decision"] = risk_state["judge_decision"]
                        self._add_log(task_id, "使用风险主管决策作为最终决策")
                elif "consolidation_report" in full_final_state and full_final_state["consolidation_report"]:
                    full_final_state["final_trade_decision"] = full_final_state["consolidation_report"]
                    self._add_log(task_id, "使用综合报告作为最终决策")
                else:
                    full_final_state["final_trade_decision"] = "HOLD"
                    self._add_log(task_id, "无法提取决策，默认 HOLD")

            # 提取结果
            signal = trading_graph.process_signal(full_final_state.get("final_trade_decision", "HOLD"))
            self._add_log(task_id, f"提取信号: {signal}")
            result = self._extract_result(full_final_state, signal, task.ticker)
            task.result = result

            # 保存报告到磁盘
            self._save_reports_to_disk(task, full_final_state, result)

            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now().isoformat()

            self._add_log(task_id, f"分析完成！交易信号: {signal}")
            logger.info(f"分析任务完成: {task_id}")

        except Exception as e:
            logger.error(f"分析任务失败: {task_id} - {e}", exc_info=True)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.completed_at = datetime.now().isoformat()
            self._add_log(task_id, f"分析失败: {e}")

    def _update_progress(self, task_id: str, step: str):
        """更新进度（只更新状态，不添加日志，日志由调用方控制）"""
        task = self._tasks.get(task_id)
        if not task:
            return

        # 查找步骤名称
        step_name = step
        for s, name in self.ANALYSIS_STEPS:
            if s == step:
                step_name = name
                break

        # 更新进度
        if step not in task.progress["completed_steps"]:
            task.progress["completed_steps"].append(step)

        task.progress["current_step"] = step
        task.progress["current_step_name"] = step_name

    def _set_current_step(self, task_id: str, step: str, step_name: str):
        """设置当前执行的步骤（不添加到completed_steps）"""
        task = self._tasks.get(task_id)
        if task:
            task.progress["current_step"] = step
            task.progress["current_step_name"] = step_name

    def _save_report_realtime(self, task_id: str, report_key: str, content: str, filename: str):
        """实时保存报告（与 CLI 行为一致）"""
        task = self._tasks.get(task_id)
        if not task or not content:
            return

        from pathlib import Path

        # 剥离市场后缀（.SZ/.SH），与 CLI 行为一致
        ticker_for_path = task.ticker.split('.')[0] if '.' in task.ticker else task.ticker

        # 获取 results 目录（与 _init_results_dir 保持一致）
        project_dir = Path(__file__).parent.parent.parent
        report_dir = project_dir / "results" / ticker_for_path / task.date / "reports"

        try:
            report_dir.mkdir(parents=True, exist_ok=True)
            file_path = report_dir / filename
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[SAVE] 任务 {task_id}: 已保存 {filename}", flush=True)
            self._add_log(task_id, f"📄 已保存报告: {filename}")
        except Exception as e:
            logger.error(f"实时保存报告失败: {e}")

    def _format_research_report(self, debate_state: dict) -> str:
        """格式化研究结论报告"""
        bull = debate_state.get('bull_history', '暂无')
        bear = debate_state.get('bear_history', '暂无')
        decision = debate_state.get('judge_decision', '暂无')

        return f"""# 研究结论报告

## 看涨观点

{bull}

## 看跌观点

{bear}

## 研究主管结论

{decision}
"""

    def _format_risk_report(self, risk_state: dict) -> str:
        """格式化风控评估报告"""
        risky = risk_state.get('risky_history', '暂无')
        safe = risk_state.get('safe_history', '暂无')
        neutral = risk_state.get('neutral_history', '暂无')
        decision = risk_state.get('judge_decision', '暂无')

        return f"""# 风控评估报告

## 激进风控意见

{risky}

## 保守风控意见

{safe}

## 中立风控意见

{neutral}

## 风控主管结论

{decision}
"""

    def _add_log(self, task_id: str, message: str):
        """添加日志（同时写入 message_tool.log）"""
        task = self._tasks.get(task_id)
        if task:
            timestamp = datetime.now().strftime("%H:%M:%S")
            task.logs.append(f"[{timestamp}] {message}")
            # 同时写入 message_tool.log（与 CLI 一致）
            if task.log_file:
                try:
                    content = message.replace("\n", " ")
                    with open(task.log_file, "a", encoding="utf-8") as f:
                        f.write(f"{timestamp} [Log] {content}\n")
                except Exception:
                    pass  # 忽略写入失败

    def _extract_result(self, final_state: dict, signal: str, ticker: str) -> dict:
        """提取分析结果"""
        result = {
            "ticker": ticker,
            "signal": signal,
            "decision": self._signal_to_decision(signal),
            "reports": {}
        }

        # 提取各类报告
        report_keys = [
            ("consolidation_report", "综合报告"),
            ("final_trade_decision", "最终决策"),
            ("market_report", "市场分析"),
            ("sentiment_report", "情绪分析"),
            ("news_report", "新闻分析"),
            ("fundamentals_report", "基本面分析"),
        ]

        for key, name in report_keys:
            if key in final_state and final_state[key]:
                result["reports"][key] = {
                    "name": name,
                    "content": final_state[key]
                }

        # 尝试解析综合报告中的关键信息
        if "consolidation_report" in result["reports"]:
            result["summary"] = self._extract_summary(
                result["reports"]["consolidation_report"]["content"]
            )
            report_decision = result["summary"].get("decision")
            if report_decision:
                result["decision"] = report_decision

        return result

    def _save_reports_to_disk(self, task: AnalysisTask, final_state: dict, result: dict):
        """保存报告到磁盘（与 CLI 行为一致）"""
        from pathlib import Path
        import json

        # 剥离市场后缀（.SZ/.SH），与 CLI 行为一致
        ticker_for_path = task.ticker.split('.')[0] if '.' in task.ticker else task.ticker

        # 获取 results 目录（与 _init_results_dir 保持一致）
        project_dir = Path(__file__).parent.parent.parent
        results_dir = project_dir / "results" / ticker_for_path / task.date

        try:
            # 创建目录
            report_dir = results_dir / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)

            # 保存各个报告
            report_mappings = {
                "market_report": "market_report.md",
                "sentiment_report": "sentiment_report.md",
                "news_report": "news_report.md",
                "fundamentals_report": "fundamentals_report.md",
                "consolidation_report": "consolidation_report.md",
                "final_trade_decision": "final_trade_decision.md",
                "trader_investment_plan": "trader_investment_plan.md",
            }

            saved_count = 0
            for state_key, filename in report_mappings.items():
                content = final_state.get(state_key)
                if content:
                    file_path = report_dir / filename
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    saved_count += 1
                    self._add_log(task.task_id, f"报告已保存: {filename}")

            # 保存完整状态（JSON 格式，方便后续分析）
            state_log = {
                "ticker": task.ticker,
                "ticker_name": task.ticker_name,
                "date": task.date,
                "signal": result.get("signal"),
                "decision": result.get("decision"),
                "user_id": task.user_id,
                "created_at": task.created_at,
                "completed_at": task.completed_at,
            }
            with open(results_dir / "analysis_summary.json", "w", encoding="utf-8") as f:
                json.dump(state_log, f, ensure_ascii=False, indent=2)

            logger.info(f"报告已保存到 {report_dir}，共 {saved_count} 个文件")
            self._add_log(task.task_id, f"所有报告已保存到 results/{task.ticker}/{task.date}/reports/")

        except Exception as e:
            logger.error(f"保存报告失败: {e}", exc_info=True)
            self._add_log(task.task_id, f"报告保存失败: {e}")

    def _signal_to_decision(self, signal: str) -> str:
        """将信号转换为决策文本"""
        signal_map = {
            "buy": "买入",
            "sell": "卖出",
            "hold": "持有",
            "reduce": "减持",
            "avoid": "观望",
            "watch": "观望",
            "strong_buy": "强烈买入",
            "strong_sell": "强烈卖出",
        }
        return signal_map.get(signal.lower(), signal)

    def _normalize_decision_text(self, decision: str) -> str:
        """标准化报告中提取到的中文/英文决策文本"""
        if not decision:
            return decision

        text = decision.strip().strip("【】[]()（）")
        text_upper = text.upper().replace(" ", "_")

        if "强烈卖出" in text or text_upper == "STRONG_SELL":
            return "强烈卖出"
        if "强烈买入" in text or text_upper == "STRONG_BUY":
            return "强烈买入"
        if "减持" in text or "回避" in text or text_upper in {"REDUCE", "AVOID"}:
            return "减持"
        if "卖出" in text or text_upper == "SELL":
            return "卖出"
        if "买入" in text or text_upper == "BUY":
            return "买入"
        if "观望" in text or text_upper == "WATCH":
            return "观望"
        if "持有" in text or text_upper == "HOLD":
            return "持有"
        return text

    def _extract_summary(self, consolidation_report: str) -> dict:
        """从综合报告中提取摘要信息"""
        import re

        summary = {
            "decision": None,
            "target_price": None,
            "confidence": None,
            "key_points": []
        }

        # 尝试提取决策
        decision_terms = r"强烈买入|强烈卖出|买入|卖出|减持|持有|观望|回避|BUY|SELL|HOLD|REDUCE|AVOID|WATCH|STRONG BUY|STRONG SELL|STRONG_BUY|STRONG_SELL"
        decision_patterns = [
            r"投资评级[：:]\s*【([^】\n]+)】",
            r"投资评级[：:]\s*([^\n（(]+)",
            rf"投资评级[：:]\s*【?\s*({decision_terms})",
            rf"投资建议[：:]\s*【?\s*({decision_terms})",
            rf"建议[：:]\s*【?\s*({decision_terms})",
            rf"决策[：:]\s*【?\s*({decision_terms})",
        ]
        for pattern in decision_patterns:
            match = re.search(pattern, consolidation_report, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                parts = [p.strip() for p in re.split(r"[/／]", candidate) if p.strip()]
                if len(parts) > 2:
                    continue
                summary["decision"] = self._normalize_decision_text(candidate)
                break

        # 尝试提取目标价
        price_patterns = [
            r"目标价[：:]\s*[¥￥]?([\d.]+)",
            r"目标价位[：:]\s*[¥￥]?([\d.]+)",
            r"target.*?[：:]\s*[¥￥]?([\d.]+)",
        ]
        for pattern in price_patterns:
            match = re.search(pattern, consolidation_report, re.IGNORECASE)
            if match:
                summary["target_price"] = float(match.group(1))
                break

        # 尝试提取置信度
        conf_patterns = [
            r"置信度[：:]\s*([\d.]+)%?",
            r"confidence[：:]\s*([\d.]+)%?",
        ]
        for pattern in conf_patterns:
            match = re.search(pattern, consolidation_report, re.IGNORECASE)
            if match:
                conf = float(match.group(1))
                summary["confidence"] = conf if conf <= 1 else conf / 100
                break

        return summary

    def get_task_status(self, task_id: str) -> Optional[dict]:
        """获取任务状态"""
        task = self._tasks.get(task_id)
        if task:
            return task.to_dict()
        return None

    def get_task_result(self, task_id: str) -> Optional[dict]:
        """获取任务结果"""
        task = self._tasks.get(task_id)
        if task and task.status == TaskStatus.COMPLETED:
            return task.result
        return None

    def get_user_history(self, user_id: str, limit: int = 10) -> List[dict]:
        """获取用户的历史分析"""
        task_ids = self._user_tasks.get(user_id, [])

        # 获取任务列表（按创建时间倒序）
        tasks = []
        for task_id in reversed(task_ids):
            task = self._tasks.get(task_id)
            if task:
                tasks.append({
                    "task_id": task.task_id,
                    "ticker": task.ticker,
                    "ticker_name": task.ticker_name,
                    "date": task.date,
                    "status": task.status.value,
                    "decision": task.result.get("decision") if task.result else None,
                    "created_at": task.created_at,
                    "completed_at": task.completed_at
                })

            if len(tasks) >= limit:
                break

        return tasks

    def cancel_task(self, task_id: str) -> bool:
        """取消任务（支持取消 PENDING 和 RUNNING 状态的任务）"""
        task = self._tasks.get(task_id)
        if not task:
            return False

        if task.status == TaskStatus.PENDING:
            # 直接取消未开始的任务
            task.status = TaskStatus.FAILED
            task.error = "用户取消"
            task.completed_at = datetime.now().isoformat()
            return True
        elif task.status == TaskStatus.RUNNING:
            # 设置取消标志，让运行中的任务在下一个 chunk 时检测到并退出
            task.cancelled = True
            logger.info(f"任务 {task_id} 已标记为取消，等待任务退出...")
            return True

        return False

    def _get_results_base_dir(self) -> Path:
        """获取结果目录根路径（与 _init_results_dir 保持一致）"""
        from pathlib import Path
        return Path(__file__).parent.parent.parent / "results"

    def get_intermediate_report(self, task_id: str, report_type: str) -> Optional[str]:
        """
        获取分析过程中的中间报告

        Args:
            task_id: 任务 ID
            report_type: 报告类型 (market_report, sentiment_report, news_report, fundamentals_report)

        Returns:
            报告内容字符串，如果不存在则返回 None
        """
        from pathlib import Path

        task = self._tasks.get(task_id)
        if not task:
            return None

        # 构建报告文件路径
        # 报告保存在 results/{ticker}/{date}/reports/{report_type}.md
        ticker_code = task.ticker.split('.')[0] if '.' in task.ticker else task.ticker
        report_dir = self._get_results_base_dir() / ticker_code / task.date / "reports"
        report_file = report_dir / f"{report_type}.md"

        if not report_file.exists():
            return None

        try:
            with open(report_file, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            logger.error(f"读取报告文件失败: {report_file}, 错误: {e}")
            return None

    def browse_all_stocks(self) -> List[Dict]:
        """
        浏览所有有历史报告的股票

        Returns:
            股票列表，每项包含 ticker, latest_date, report_count
        """
        from pathlib import Path

        results_dir = self._get_results_base_dir()
        if not results_dir.exists():
            return []

        stocks = []
        for ticker_dir in sorted(results_dir.iterdir()):
            if not ticker_dir.is_dir():
                continue

            # 获取所有日期目录
            dates = []
            for date_dir in ticker_dir.iterdir():
                if date_dir.is_dir() and (date_dir / "reports").exists():
                    dates.append(date_dir.name)

            if dates:
                dates.sort(reverse=True)
                stocks.append({
                    "ticker": ticker_dir.name,
                    "latest_date": dates[0],
                    "report_count": len(dates)
                })

        # 按最新日期排序
        stocks.sort(key=lambda x: x["latest_date"], reverse=True)
        return stocks

    def get_stock_report_dates(self, ticker: str) -> List[Dict]:
        """
        获取某只股票的所有分析日期

        Args:
            ticker: 股票代码（如 600036）

        Returns:
            日期列表，每项包含 date, has_summary, reports
        """
        from pathlib import Path

        ticker_code = ticker.split('.')[0] if '.' in ticker else ticker
        ticker_dir = self._get_results_base_dir() / ticker_code

        if not ticker_dir.exists():
            return []

        dates = []
        for date_dir in sorted(ticker_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue

            report_dir = date_dir / "reports"
            summary_file = date_dir / "analysis_summary.json"

            # 检查存在的报告类型
            available_reports = []
            if report_dir.exists():
                for report_file in report_dir.glob("*.md"):
                    available_reports.append(report_file.stem)

            dates.append({
                "date": date_dir.name,
                "has_summary": summary_file.exists(),
                "reports": available_reports
            })

        return dates

    def get_historical_report(self, ticker: str, date: str, report_type: str = "final_report") -> Optional[Dict]:
        """
        获取历史报告内容

        Args:
            ticker: 股票代码
            date: 日期（YYYY-MM-DD）
            report_type: 报告类型（final_report, market_report, sentiment_report, news_report, fundamentals_report）

        Returns:
            报告内容字典，包含 content, summary（如果有）
        """
        ticker_code = ticker.split('.')[0] if '.' in ticker else ticker
        date_dir = self._get_results_base_dir() / ticker_code / date

        if not date_dir.exists():
            return None

        result = {}

        # 报告类型映射：前端名称 -> 实际文件名
        REPORT_FILE_MAP = {
            "final_report": "consolidation_report",
            "market_report": "market_report",
            "sentiment_report": "sentiment_report",
            "news_report": "news_report",
            "fundamentals_report": "fundamentals_report",
        }

        # 读取报告文件
        report_dir = date_dir / "reports"
        actual_report_name = REPORT_FILE_MAP.get(report_type, report_type)
        report_file = report_dir / f"{actual_report_name}.md"

        if report_file.exists():
            try:
                with open(report_file, 'r', encoding='utf-8') as f:
                    result["content"] = f.read()
            except Exception as e:
                logger.error(f"读取报告失败: {report_file}, 错误: {e}")
                result["content"] = None
        else:
            result["content"] = None

        # 读取分析摘要（如果请求的是最终报告）
        if report_type == "final_report":
            summary_file = date_dir / "analysis_summary.json"
            if summary_file.exists():
                try:
                    with open(summary_file, 'r', encoding='utf-8') as f:
                        result["summary"] = json.load(f)
                except Exception as e:
                    logger.error(f"读取摘要失败: {summary_file}, 错误: {e}")
                    result["summary"] = None

        return result if result.get("content") else None
