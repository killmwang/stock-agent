"""
A股综合研报生成器

整合7份分析报告，生成专业、结构清晰、可执行的综合投资研究报告
支持自动记录决策到 Memory，并在下次分析时查询历史决策
"""

import re
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from tradingagents.agents.utils.agent_utils import is_china_stock
from tradingagents.agents.utils.valuation_validator import (
    validate_valuation_report,
    format_validation_warnings,
    extract_daily_basic_from_report,
    validate_dividend_yield,
    is_high_dividend_stock,
    extract_target_price
)

logger = logging.getLogger(__name__)


RATING_PRIORITY = {
    "强烈卖出": 0,
    "卖出": 1,
    "减持": 2,
    "回避": 2,
    "观望": 3,
    "持有": 4,
    "买入": 5,
    "强烈买入": 6,
}


def _split_rating_parts(rating: str) -> List[str]:
    return [p.strip() for p in re.split(r'[/／]', rating) if p.strip()]


def _canonical_rating(rating: str) -> str:
    text = rating.strip().strip("【】[]()（）")
    text_upper = text.upper().replace(" ", "_")

    if "强烈卖出" in text or text_upper == "STRONG_SELL":
        return "强烈卖出"
    if "强烈买入" in text or text_upper == "STRONG_BUY":
        return "强烈买入"
    if "卖出" in text or text_upper == "SELL":
        return "卖出"
    if "减持" in text or "回避" in text or text_upper in {"REDUCE", "AVOID"}:
        return "减持"
    if "观望" in text or text_upper == "WATCH":
        return "观望"
    if "持有" in text or text_upper == "HOLD":
        return "持有"
    if "买入" in text or text_upper == "BUY":
        return "买入"
    return ""


def _normalize_rating_text(rating: str) -> str:
    """Normalize a rating, using the more conservative side for mixed ratings."""
    if not rating:
        return ""

    parts = _split_rating_parts(rating)
    if len(parts) > 2:
        return ""
    candidates = [_canonical_rating(part) for part in (parts or [rating])]
    candidates = [candidate for candidate in candidates if candidate]
    if not candidates:
        return rating.strip().strip("【】[]()（）")
    return min(candidates, key=lambda item: RATING_PRIORITY.get(item, 99))


def _extract_current_price(market_report: str) -> Optional[float]:
    """
    从市场报告中提取当前/收盘价格

    Args:
        market_report: 市场技术分析报告文本

    Returns:
        float: 当前价格，提取失败返回 None
    """
    if not market_report:
        return None

    # 按优先级尝试多种价格模式
    patterns = [
        r'当前价[格]?[：:]\s*([\d.]+)',
        r'收盘价[：:]\s*([\d.]+)',
        r'最新价[：:]\s*([\d.]+)',
        r'现价[：:]\s*([\d.]+)',
        r'close[：:]\s*([\d.]+)',
        r'\|\s*收盘价\s*\|\s*([\d.]+)',
        r'股价[：:]\s*([\d.]+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, market_report, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue

    return None


def _extract_report_rating(report: str) -> str:
    """Extract the explicit investment rating from the consolidated report."""
    rating_patterns = [
        r'投资评级[：:]\s*【([^】]+)】',
        r'投资评级[：:]\s*([^\n（(]+)',
    ]
    for pattern in rating_patterns:
        match = re.search(pattern, report)
        if not match:
            continue
        rating = match.group(1).strip()
        normalized = _normalize_rating_text(rating)
        if normalized:
            return normalized
    return ""


def _extract_position_size(report: str) -> Optional[float]:
    """Extract the first explicit recommended position size percentage."""
    patterns = [
        r'建议仓位[：:]\s*(?:不应超过|上限)?\s*(\d+(?:\.\d+)?)\s*%',
        r'仓位(?:占比|上限)?[：:]\s*(?:不应超过|上限)?\s*(\d+(?:\.\d+)?)\s*%',
    ]
    for pattern in patterns:
        match = re.search(pattern, report)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def _extract_report_target_price(report: str) -> Optional[float]:
    """Extract the first target price stated in the consolidated report."""
    patterns = [
        r'目标价[位]?[：:]\s*[¥￥]?(\d+(?:\.\d+)?)',
        r'目标价[位]?\s*[¥￥]?(\d+(?:\.\d+)?)\s*元',
    ]
    for pattern in patterns:
        match = re.search(pattern, report)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def _format_price(value: float) -> str:
    return f"{value:.2f}"


def _extract_report_current_price(report: str) -> Optional[float]:
    """Extract current price from the consolidated report as a fallback."""
    patterns = [
        r'当前股价[：:]\s*[¥￥]?(\d+(?:\.\d+)?)',
        r'当前价[格]?[：:]\s*[¥￥]?(\d+(?:\.\d+)?)',
        r'现价[：:]\s*[¥￥]?(\d+(?:\.\d+)?)',
        r'较现价\s*[¥￥]?(\d+(?:\.\d+)?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, report)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def _correct_quarter_eps_pe_target(
    report: str,
    target_price: Optional[float],
    current_price: Optional[float],
) -> tuple[str, Optional[float]]:
    """
    Correct PE targets that directly multiply quarterly EPS by annual PE.

    Example bad pattern: EPS 10.42 x 11 PE = 114.62 while price is 1266.74.
    If no annual/TTM EPS wording is present and the implied target is far below
    current price, assume the EPS is single-period and annualize it.
    """
    if not current_price:
        return report, target_price

    formula_pattern = (
        r'(\d+(?:\.\d+)?)\s*元?\s*[×xX*]\s*'
        r'(\d+(?:\.\d+)?)\s*(?:倍|x|X)?\s*=?\s*'
        r'(\d+(?:\.\d+)?)\s*元'
    )

    for match in re.finditer(formula_pattern, report):
        try:
            eps = float(match.group(1))
            multiple = float(match.group(2))
            stated_target = float(match.group(3))
        except ValueError:
            continue

        direct_target = eps * multiple
        if stated_target == 0:
            continue
        if abs(stated_target - direct_target) / stated_target > 0.03:
            continue

        context = report[max(0, match.start() - 220): min(len(report), match.end() + 80)]
        if re.search(r'TTM|ttm|年化|全年|年度|预测EPS|全年预测', context):
            continue
        has_eps_context = re.search(r'EPS|每股收益', context, re.IGNORECASE)
        has_pe_context = re.search(r'PE|市盈率', context, re.IGNORECASE)
        has_book_value_context = re.search(
            r'市净率|每股净资产|BVPS|book\s*value',
            context,
            re.IGNORECASE,
        )
        if has_book_value_context and not (has_eps_context and has_pe_context):
            continue
        if not has_eps_context or not (has_pe_context or multiple >= 5):
            continue

        if stated_target / current_price >= 0.5:
            continue

        corrected_target = direct_target * 4
        old_price = _format_price(stated_target)
        new_price = _format_price(corrected_target)
        report = re.sub(
            rf'{re.escape(old_price)}\s*元',
            f'{new_price}元',
            report,
            count=0,
        )

        note = (
            f"- EPS口径校验：原计算疑似使用单期EPS {eps:.2f}元直接乘以"
            f"{multiple:.1f}倍PE；PE估值应使用TTM/全年预测/年化EPS，"
            f"已按年化EPS {eps * 4:.2f}元修正目标价为{new_price}元。"
        )
        lines = report.split("\n")
        inserted = False
        for i, line in enumerate(lines):
            if "目标价位推导" in line or "目标价" in line:
                lines.insert(i + 1, note)
                inserted = True
                break
        if not inserted:
            lines.insert(0, note)
        report = "\n".join(lines)

        logger.warning(
            "[Consistency] PE目标价EPS口径已修正: %.2f -> %.2f (EPS %.2f x %.1f x 4)",
            stated_target,
            corrected_target,
            eps,
            multiple,
        )
        return report, corrected_target

    return report, target_price


def _correct_target_change_text(
    report: str,
    target_price: Optional[float],
    current_price: Optional[float],
) -> str:
    """Correct target-vs-current percentage text when both prices are known."""
    if not target_price or not current_price:
        return report

    change_pct = (target_price - current_price) / current_price * 100
    direction = "上涨" if change_pct >= 0 else "下跌"
    replacement = f"较现价{current_price:.2f}元{direction}{abs(change_pct):.1f}%"
    pattern = r'较现价\s*[¥￥]?\s*\d+(?:\.\d+)?\s*元?\s*(?:上涨|下跌)\s*\d+(?:\.\d+)?%'
    return re.sub(pattern, replacement, report)


def _replace_investment_rating(report: str, rating: str) -> str:
    """Replace or insert the investment rating line."""
    pattern = r'(?m)^(\s*[-*]?\s*)投资评级[：:]\s*【?[^】\n]+】?'
    match = re.search(pattern, report)
    if match:
        prefix = match.group(1)
        replacement = f"{prefix}投资评级：【{rating}】"
        return report[:match.start()] + replacement + report[match.end():]

    lines = report.split("\n")
    for i, line in enumerate(lines):
        if "执行摘要" in line or "Executive Summary" in line:
            lines.insert(i + 1, f"- 投资评级：【{rating}】")
            return "\n".join(lines)
    return f"- 投资评级：【{rating}】\n{report}"


def _insert_consistency_note(report: str, rating: str, reason: str) -> str:
    """Insert a concise note explaining an automatic consistency correction."""
    note = f"- 一致性校验：{reason}，评级已调整为【{rating}】。"
    if "一致性校验" in report:
        return report

    lines = report.split("\n")
    for i, line in enumerate(lines):
        if "投资评级" in line:
            lines.insert(i + 1, note)
            return "\n".join(lines)
    lines.insert(0, note)
    return "\n".join(lines)


def _rating_to_signal(rating: str) -> str:
    rating = _normalize_rating_text(rating)
    if "买入" in rating:
        return "BUY"
    if "卖出" in rating or "减持" in rating or "回避" in rating:
        return "SELL"
    return "HOLD"


def _enforce_consolidation_consistency(
    report: str,
    current_price: Optional[float],
) -> tuple[str, Optional[str]]:
    """
    Enforce consistency among rating, target price, and position advice.

    The LLM may produce contradictory text such as "持有" with "0%仓位/清仓".
    This deterministic pass keeps the final report internally consistent.
    """
    rating = _extract_report_rating(report)
    position_size = _extract_position_size(report)
    target_price = _extract_report_target_price(report)
    if current_price is None:
        current_price = _extract_report_current_price(report)
    report, target_price = _correct_quarter_eps_pe_target(
        report,
        target_price,
        current_price,
    )
    report = _correct_target_change_text(report, target_price, current_price)
    exit_language = any(
        term in report
        for term in ["清仓", "空仓", "回避", "不建议入场", "当前无任何买入信号"]
    )

    corrected_rating = ""
    reason = ""

    if position_size is not None and position_size <= 0:
        corrected_rating = "卖出"
        reason = "报告建议仓位为0%，与持有评级冲突"
    elif exit_language:
        corrected_rating = "卖出"
        reason = "报告出现清仓/回避/不建议入场等防守性操作建议"
    elif current_price and target_price:
        downside = (current_price - target_price) / current_price
        if downside >= 0.10 and (
            not rating or rating in {"持有", "观望", "买入", "强烈买入"}
        ):
            corrected_rating = "卖出"
            reason = f"目标价较现价低约{downside * 100:.1f}%"
        elif downside >= 0.03 and (
            not rating or rating in {"持有", "观望", "买入", "强烈买入"}
        ):
            corrected_rating = "减持"
            reason = f"目标价较现价低约{downside * 100:.1f}%"

    if not corrected_rating or corrected_rating == rating:
        return report, None

    report = _replace_investment_rating(report, corrected_rating)
    report = _insert_consistency_note(report, corrected_rating, reason)
    logger.warning(
        "[Consistency] 综合报告评级已修正: %s -> %s (%s)",
        rating or "未提取",
        corrected_rating,
        reason,
    )
    return report, _rating_to_signal(corrected_rating)


def _auto_update_past_outcomes(
    memory,
    ticker: str,
    current_date: str,
    current_price: float
) -> Dict[str, Any]:
    """
    自动更新该股票历史决策的实际结果

    在每次分析时调用，检查该股票的历史决策，计算实际收益

    Args:
        memory: FinancialSituationMemory 实例
        ticker: 股票代码
        current_date: 当前分析日期 (YYYY-MM-DD)
        current_price: 当前股价

    Returns:
        Dict: 更新结果统计 {updated: int, skipped: int, errors: int}
    """
    from tradingagents.agents.utils.memory import get_historical_price

    result = {"updated": 0, "skipped": 0, "errors": 0, "details": []}

    if memory is None or current_price is None:
        return result

    try:
        # 获取所有记录
        all_records = memory.situation_collection.get(include=["metadatas"])

        if not all_records["ids"]:
            return result

        # 筛选该股票的未更新记录
        for i, metadata in enumerate(all_records["metadatas"]):
            record_id = all_records["ids"][i]
            record_ticker = metadata.get("ticker", "")
            outcome_updated = metadata.get("outcome_updated", False)
            decision_date = metadata.get("decision_date", "")

            # 只处理同一股票、未更新结果、且不是当天的记录
            if record_ticker != ticker:
                continue
            if outcome_updated:
                result["skipped"] += 1
                continue
            if decision_date == current_date:
                result["skipped"] += 1
                continue
            if not decision_date:
                result["skipped"] += 1
                continue

            try:
                # 获取决策日的历史价格
                decision_price = get_historical_price(ticker, decision_date)

                if decision_price is None:
                    logger.warning(f"无法获取 {ticker} 在 {decision_date} 的历史价格")
                    result["errors"] += 1
                    continue

                # 计算持仓天数
                try:
                    date_format = "%Y-%m-%d"
                    d1 = datetime.strptime(decision_date, date_format)
                    d2 = datetime.strptime(current_date, date_format)
                    days_held = (d2 - d1).days
                except ValueError:
                    days_held = 0

                # 计算实际收益率
                actual_return = (current_price - decision_price) / decision_price * 100

                # 根据决策类型调整收益（SELL决策收益反转）
                decision_type = metadata.get("decision_type", "HOLD")
                if decision_type in ["SELL", "STRONG_SELL", "REDUCE"]:
                    actual_return = -actual_return

                # 更新结果
                success = memory.update_outcome(
                    record_id=record_id,
                    actual_return=actual_return,
                    days_held=days_held,
                    exit_date=current_date,
                    exit_reason=f"自动追踪: 决策价 {decision_price:.2f} → 当前价 {current_price:.2f}"
                )

                if success:
                    result["updated"] += 1
                    result["details"].append({
                        "record_id": record_id,
                        "decision_date": decision_date,
                        "decision_price": decision_price,
                        "current_price": current_price,
                        "return": actual_return,
                        "days_held": days_held
                    })
                    logger.info(
                        f"✅ 自动更新历史决策 {record_id}: "
                        f"{decision_price:.2f} → {current_price:.2f}, "
                        f"收益 {actual_return:.2f}%, 持仓 {days_held} 天"
                    )
                else:
                    result["errors"] += 1

            except Exception as e:
                logger.warning(f"更新记录 {record_id} 时出错: {e}")
                result["errors"] += 1

    except Exception as e:
        logger.error(f"自动追踪历史决策失败: {e}")
        result["errors"] += 1

    return result


CONSOLIDATION_SYSTEM_PROMPT = '''您是一位资深的A股投资研究总监，负责整合团队的研究成果并撰写最终的综合研究报告。

═══════════════════════════════════════════════════════════════
【跨语言思维链指令】Cross-Lingual Chain of Thought
═══════════════════════════════════════════════════════════════

**Step 1: Think in English** (Internal reasoning)
For report synthesis, valuation calculation, and risk-reward analysis:
- Use English to structure your analytical framework
- Apply universal standards: DCF, PE/PB valuation, risk matrix
- Ensure mathematical accuracy in target price derivation

**Step 2: Preserve A-share Context** (Domain knowledge)
以下内容必须用中文理解，不可英文化：
- 投资术语：强烈买入/买入/持有/减持/卖出
- 资金术语：北向资金、融资余额、主力资金、香港中央结算
- 市场术语：涨停板、板块轮动、龙头效应、抱团股
- 风险术语：质押风险、解禁压力、商誉减值

**Step 3: Output in Chinese** (Final response)
- 使用中文输出专业研报
- 数据引用必须标明来源
- 投资建议必须具体可执行

═══════════════════════════════════════════════════════════════

## 输入报告

您将收到以下8份分析材料：
1. **市场技术分析报告** - 技术指标、趋势分析、支撑/阻力位
2. **市场情绪报告** - 资金流向、千股千评、北向资金
3. **新闻舆情报告** - 公司新闻、行业动态、宏观政策
4. **基本面分析报告** - 财报分析、估值指标、盈利能力
5. **投资计划** - 研究团队的多空辩论结论
6. **交易员计划** - 具体的交易策略建议
7. **最终交易决策** - 风险评估团队的综合判断
8. **上次决策反思**（如有）- 上次分析的决策回顾、实际表现、经验教训

## 报告要求

请生成一份**专业、结构清晰、可执行**的综合投资研究报告，包含以下部分：

### 1. 执行摘要 (Executive Summary)
- 投资评级：【强烈买入/买入/持有/减持/卖出】
- **目标价位推导**（必须严格遵循基本面分析师的估值决策）：
  - **第一步**：提取基本面分析报告中的**估值决策**数据
  - **第二步**：严格遵循指定的**估值方法**和**目标倍数区间**
  - **禁止**：自行更换估值方法或倍数区间
  - **EPS口径校验**：若使用PE估值，必须确认EPS是TTM/全年预测/年化EPS；若只拿到单季度EPS，必须年化后再乘PE，禁止直接用季度EPS × 年度PE
  - **计算公式**：目标价 = 基础每股收益（或净资产）× 目标倍数区间中值
  - **结论**：目标价 XX元（较现价上涨/下跌X%）。估值方法来源：基本面分析报告（指定方法）。
- 核心投资逻辑（3-5点，每点需引用具体数据）
- 主要风险提示（2-3点）

### 2. 多维度分析汇总

#### 2.1 基本面评估
- 盈利能力与成长性（**必须引用具体数据**，格式：指标名=数值，据XX报告）
- 估值水平合理性：
  - 当前PE/PB数值及其近期分位（如：PE 7.2，处于近1年 XX% 分位）
  - 与行业均值/可比公司对比
- 财务健康度（资产负债率、现金流等）

#### 2.2 技术面评估
- 当前趋势判断（多头/空头/震荡）
- 关键价位（**必须给出具体数字**）：
  - 第一支撑位：XX元（依据：XX）
  - 第二支撑位：XX元（依据：XX）
  - 第一阻力位：XX元（依据：XX）
  - 关键突破位：XX元
- 技术指标信号：
  - RSI=XX（超买>70/超卖<30/中性）
  - MACD=XX（金叉/死叉/零轴上下）

#### 2.3 资金面评估
- 主力资金动向：近X日净流入/流出 XX万元（据情绪报告）
- 外资态度（基于前十大股东季度数据）：
  - 香港中央结算持股占比X%，较上期±Y%（数据来源：YYYY年Q季报）
  - 是否进入沪深港通十大成交股（反映外资交易活跃度）
  - ⚠️ 北向资金日度数据已于2024年8月停更，仅季度数据可用
- 融资余额：XX亿元，近X日变化 XX%（判断杠杆情绪）

#### 2.4 消息面评估
- 重大利好/利空（**具体事件+日期**）
- 行业政策影响
- 宏观经济背景（PMI=XX，处于扩张/收缩区间）

### 3. 投资建议

#### 3.1 操作策略
- **建议仓位**：XX%（给出理由）
- **盈亏比测算**（必须计算）：
  - 潜在收益：目标价XX - 现价XX = +XX元（+XX%）
  - 潜在亏损：现价XX - 止损价XX = -XX元（-XX%）
  - **盈亏比 = 潜在收益/潜在亏损 = X:1**
  - 评估：盈亏比>2:1为可接受，<1.5:1不建议入场
- **入场时机**（条件触发式，必须具体）：
  - 方案A（回踩买入）：价格回落至XX-XX区间 + 企稳信号（如：缩量不破/阳线反包）
  - 方案B（突破买入）：放量突破XX并站稳X个交易日 + 资金面配合（全口径转正）
- **目标价位**：
  - 短期（1个月）：XX元（技术阻力位）
  - 中期（3-6个月）：XX元（估值目标）
- **止损价位**：XX元（跌破此位执行止损，理由：XX）

#### 3.2 分批建仓/减仓计划
| 批次 | 价位区间 | 仓位占比 | 触发条件（必须具体） |
|------|---------|---------|---------------------|
| 第一批 | XX-XX元 | XX% | 条件1 + 条件2 |
| 第二批 | XX-XX元 | XX% | 条件1 + 条件2 |

### 4. 风险评估矩阵

| 风险类型 | 风险描述（具体化） | 概率 | 影响程度 | 应对措施 |
|---------|-------------------|------|---------|---------|
| 市场风险 | 大盘系统性下跌，沪指跌破XX点 | 低/中/高 | 低/中/高 | 跌破XX元止损 |
| 行业风险 | XX行业政策变化/周期下行 | 低/中/高 | 低/中/高 | 分散配置 |
| 公司风险 | 季报业绩不及预期/资产质量恶化 | 低/中/高 | 低/中/高 | 跟踪季报 |
| 流动性风险 | 融资盘集中平仓/资金持续流出 | 低/中/高 | 低/中/高 | 监控融资余额 |

### 5. 关键监测指标

1. **宏观指标**：下月PMI发布日（关注是否回到50以上）
2. **公司指标**：下一财报发布日期（重点关注XX指标）
3. **资金指标**：全口径资金流向、融资余额变化趋势
4. **技术点位**：
   - 向上确认：放量站稳XX元
   - 向下警示：跌破XX元

### 6. 历史决策回顾（仅当有上次决策反思时）

**重要**：仅当"报告8：上次决策反思"包含有效内容时才生成此部分。如果显示"首次分析此股票"或"无历史决策记录"，则**完全跳过此部分**，不要输出任何内容。

如果有有效的历史决策反思，请在此部分：
- 简要回顾上次决策及其结果（决策类型、当时价格、实际涨跌）
- 分析决策正确/错误的原因
- 说明本次分析如何吸收经验教训（例如：上次过于保守，本次需更关注XXX）

### 7. 免责声明

本报告由AI系统自动生成，仅供参考，不构成投资建议。投资有风险，入市需谨慎。

---

## 格式要求
- 使用 Markdown 格式（不要在外层包裹 ```markdown）
- **数据引用格式**：所有数据必须标明来源，格式为「指标=数值（据XX报告）」
- 观点需有数据支撑，避免空泛表述
- 语言专业但易于理解
- 建议必须具体、可操作，避免模糊表述

## 重要原则
1. **量化优先**：所有结论必须有数据支撑，能算的必须算（盈亏比、估值推导等）
2. **逻辑自洽**：最终结论必须与各维度分析相符，不能自相矛盾
3. **条件触发**：入场/止损条件必须是可验证的具体条件，而非模糊描述
4. **风险收益平衡**：既要揭示风险，也要识别机会。错过低估机会和买入高估股票都是决策失误
5. **可执行性**：建议必须具体到价位、仓位、时机、触发条件
6. **评级-仓位一致**：若建议仓位为0%、清仓、回避或不建议入场，投资评级不得写为“持有”或“买入”，应写为“卖出/减持/观望”中最贴近的一项
7. **目标价一致**：若主要目标价显著低于当前价，投资评级不得写为“买入/持有”，必须解释为减持或卖出逻辑
8. **估值口径一致**：PE估值必须使用TTM EPS、全年预测EPS或年化EPS；使用季度EPS时必须先乘以4或说明年化方法，避免目标价低一个数量级
9. **公式类型一致**：PE公式使用EPS，PB公式使用每股净资产/BVPS，股息率公式使用每股分红；不得把PB公式误写成PE公式，也不得把季度EPS直接套年度PE
10. **数学自检**：输出前必须复算 目标价、较现价涨跌幅、潜在收益、潜在亏损、盈亏比；如任一结果与评级冲突，先修正评级或操作建议再输出
'''


def _extract_decision_info(final_decision: str, consolidation_report: str) -> Dict[str, Any]:
    """
    从最终决策和综合报告中提取关键信息

    Returns:
        Dict: 包含 decision_type, confidence, target_price, stop_loss 等
    """
    info = {
        "decision_type": "HOLD",
        "confidence": 0.5,
        "target_price": None,
        "stop_loss": None,
        "position_size": None,
    }

    # 1. 先尝试精确匹配投资评级行
    rating_patterns = [
        r'投资评级[：:]\s*【([^】]+)】',     # 投资评级：【卖出】
        r'投资评级[：:]\s*([^\n（(]+)',      # 投资评级：卖出 或 投资评级：卖出（Reduce）
    ]

    rating_text = ""
    for pattern in rating_patterns:
        match = re.search(pattern, consolidation_report)
        if match:
            rating_text = _normalize_rating_text(match.group(1).strip())
            if rating_text:
                break

    # 2. 在提取的评级文本中判断决策类型（调整顺序：卖出优先）
    if rating_text:
        rating_lower = rating_text.lower()
        if "强烈卖出" in rating_text:
            info["decision_type"] = "STRONG_SELL"
            info["confidence"] = 0.9
        elif "强烈买入" in rating_text:
            info["decision_type"] = "STRONG_BUY"
            info["confidence"] = 0.9
        elif "卖出" in rating_text or "sell" in rating_lower:
            info["decision_type"] = "SELL"
            info["confidence"] = 0.7
        elif "减持" in rating_text or "reduce" in rating_lower:
            info["decision_type"] = "REDUCE"
            info["confidence"] = 0.6
        elif "观望" in rating_text or "watch" in rating_lower:
            info["decision_type"] = "HOLD"
            info["confidence"] = 0.5
        elif "买入" in rating_text or "buy" in rating_lower:
            info["decision_type"] = "BUY"
            info["confidence"] = 0.7
        elif "持有" in rating_text or "hold" in rating_lower:
            info["decision_type"] = "HOLD"
            info["confidence"] = 0.5

    # 3. 如果精确匹配失败，降级到全文搜索（原有逻辑）
    if info["decision_type"] == "HOLD" and not rating_text:
        decision_text = final_decision + " " + consolidation_report
        decision_text_upper = decision_text.upper()

        # 注意顺序：卖出相关优先检查
        if "强烈卖出" in decision_text:
            info["decision_type"] = "STRONG_SELL"
            info["confidence"] = 0.9
        elif "强烈买入" in decision_text:
            info["decision_type"] = "STRONG_BUY"
            info["confidence"] = 0.9
        elif "STRONG SELL" in decision_text_upper:
            info["decision_type"] = "STRONG_SELL"
            info["confidence"] = 0.9
        elif "STRONG BUY" in decision_text_upper:
            info["decision_type"] = "STRONG_BUY"
            info["confidence"] = 0.9
        # 中文检查卖出优先
        elif "减持" in decision_text:
            info["decision_type"] = "REDUCE"
            info["confidence"] = 0.6
        elif "卖出" in decision_text and "买入" not in decision_text:
            info["decision_type"] = "SELL"
            info["confidence"] = 0.7
        elif "买入" in decision_text and "卖出" not in decision_text:
            info["decision_type"] = "BUY"
            info["confidence"] = 0.7

    # 尝试提取目标价
    target_match = re.search(r'目标价[位]?[：:]\s*[¥￥]?(\d+\.?\d*)', consolidation_report)
    if target_match:
        info["target_price"] = float(target_match.group(1))

    # 尝试提取止损价
    stop_match = re.search(r'止损价[位]?[：:]\s*(\d+\.?\d*)', consolidation_report)
    if stop_match:
        info["stop_loss"] = float(stop_match.group(1))

    # 尝试提取仓位
    position_match = re.search(r'建议仓位[：:]\s*(\d+(?:\.\d+)?)%', consolidation_report)
    if position_match:
        info["position_size"] = float(position_match.group(1))

    return info


def _format_historical_decisions(memories: list) -> str:
    """
    格式化历史决策为可读文本

    Args:
        memories: 从 memory.get_memories() 返回的记忆列表

    Returns:
        str: 格式化的历史决策文本
    """
    if not memories:
        return "首次分析此股票，无历史决策记录"

    result = []
    for i, mem in enumerate(memories, 1):
        similarity = mem.get("similarity_score", 0) * 100
        situation = mem.get("matched_situation", "")[:500]  # 截断过长内容
        recommendation = mem.get("recommendation", "")[:300]

        # 提取额外信息（如果有）
        decision_type = mem.get("decision_type", "未知")
        decision_date = mem.get("decision_date", "未知日期")
        actual_return = mem.get("actual_return")

        result.append(f"### 历史决策 {i} (相似度: {similarity:.1f}%)")
        result.append(f"**决策日期**: {decision_date}")
        result.append(f"**决策类型**: {decision_type}")

        if actual_return is not None:
            outcome = "盈利" if actual_return > 0 else "亏损"
            result.append(f"**实际结果**: {outcome} {abs(actual_return):.2f}%")

        result.append(f"\n**当时市场情况**:\n{situation}...")
        result.append(f"\n**当时建议**:\n{recommendation}...")
        result.append("\n---\n")

    return "\n".join(result)


def create_consolidation_analyst(llm, decision_memory=None):
    """
    创建A股综合研报分析师节点

    Args:
        llm: 语言模型实例
        decision_memory: 决策记忆存储（FinancialSituationMemory 实例）

    Returns:
        consolidation_node: 综合报告生成节点函数
    """

    def consolidation_node(state: Dict[str, Any]) -> Dict[str, Any]:
        """
        整合所有分析报告，生成综合研报

        Args:
            state: 包含所有分析报告的状态字典

        Returns:
            包含 consolidation_report 的字典
        """
        ticker = state.get("company_of_interest", "未知股票")
        trade_date = state.get("trade_date", "未知日期")

        # 获取股票名称（如果有的话）
        stock_name = ""
        market_report = state.get("market_report", "")
        if "名称" in market_report:
            # 尝试从市场报告中提取股票名称
            name_match = re.search(r'名称[：:]\s*(\S+)', market_report)
            if name_match:
                stock_name = name_match.group(1)

        # ========== 0. 自动追踪历史决策结果 ==========
        current_price = _extract_current_price(market_report)
        if current_price:
            logger.info(f"[Memory] 提取当前价格: {current_price}")

        if decision_memory is not None and current_price is not None:
            try:
                tracking_result = _auto_update_past_outcomes(
                    memory=decision_memory,
                    ticker=ticker,
                    current_date=trade_date,
                    current_price=current_price
                )
                if tracking_result["updated"] > 0:
                    logger.info(
                        f"[Memory] 自动追踪完成: 更新 {tracking_result['updated']} 条, "
                        f"跳过 {tracking_result['skipped']} 条, 错误 {tracking_result['errors']} 条"
                    )
            except Exception as e:
                logger.warning(f"[Memory] 自动追踪历史决策失败: {e}")

        # ========== 1. 查询历史决策 ==========
        previous_decision_reflection = "首次分析此股票，无历史决策记录"

        if decision_memory is not None:
            try:
                # 构建当前市场情况摘要（用于相似度匹配）
                current_situation = f"""
股票: {ticker} {stock_name}
日期: {trade_date}

市场技术面: {state.get("market_report", "")[:800]}

情绪面: {state.get("sentiment_report", "")[:500]}

新闻面: {state.get("news_report", "")[:500]}

基本面: {state.get("fundamentals_report", "")[:500]}
"""
                # 查询相似历史决策（最多3条），排除当天的记录
                historical_memories = decision_memory.get_memories(
                    current_situation,
                    n_matches=3,
                    exclude_date=trade_date  # 排除当天记录，避免当日多次分析时引用自己
                )

                if historical_memories:
                    previous_decision_reflection = _format_historical_decisions(historical_memories)
                    logger.info(f"找到 {len(historical_memories)} 条历史决策记录")
                else:
                    logger.info(f"股票 {ticker} 无历史决策记录")

            except Exception as e:
                logger.warning(f"查询历史决策失败: {e}")
                previous_decision_reflection = f"查询历史决策时出错: {str(e)}"

        # 构建输入材料
        input_materials = f"""
# {stock_name}（{ticker}）综合分析材料

**分析日期**: {trade_date}
**生成时间**: 由 TradingAgents AI Research 系统生成

---

## 报告 1：市场技术分析

{state.get("market_report", "暂无数据")}

---

## 报告 2：市场情绪分析

{state.get("sentiment_report", "暂无数据")}

---

## 报告 3：新闻舆情分析

{state.get("news_report", "暂无数据")}

---

## 报告 4：基本面分析

{state.get("fundamentals_report", "暂无数据")}

---

## 报告 5：投资计划（研究团队多空辩论结论）

{state.get("investment_plan", "暂无数据")}

---

## 报告 6：交易员执行计划

{state.get("trader_investment_plan", "暂无数据")}

---

## 报告 7：最终交易决策（风险评估团队）

{state.get("final_trade_decision", "暂无数据")}

---

## 报告 8：上次决策反思（如有）

{previous_decision_reflection}

---

请根据以上报告，生成一份专业的综合投资研究报告。如果有上次决策反思，请在报告中体现对历史决策的回顾和经验教训的吸收。
"""

        # 构建消息
        messages = [
            {"role": "system", "content": CONSOLIDATION_SYSTEM_PROMPT},
            {"role": "user", "content": input_materials}
        ]

        # 调用LLM生成报告
        try:
            response = llm.invoke(messages)
            consolidation_report = response.content
        except Exception as e:
            consolidation_report = f"""
# {stock_name}（{ticker}）综合投资研究报告

**分析日期**: {trade_date}

---

## 报告生成失败

综合报告生成过程中发生错误: {str(e)}

请查看各独立分析报告获取详细信息。
"""

        # ========== 1.5 估值数据一致性验证 ==========
        try:
            fundamentals_report = state.get("fundamentals_report", "")
            if fundamentals_report and current_price:
                # 从基本面报告中提取估值数据
                daily_basic_data = extract_daily_basic_from_report(fundamentals_report)

                # 执行验证
                validation_result = validate_valuation_report(
                    fundamentals_report=fundamentals_report,
                    current_price=current_price,
                    daily_basic_data=daily_basic_data
                )

                # 如果验证不通过，在报告开头插入警告
                if not validation_result.get("passed", True):
                    warning_text = format_validation_warnings(validation_result)
                    if warning_text:
                        # 在报告标题后插入警告
                        lines = consolidation_report.split('\n')
                        insert_pos = 0
                        for i, line in enumerate(lines):
                            if line.startswith('# ') or line.startswith('**分析日期'):
                                insert_pos = i + 1
                                if line.startswith('**分析日期'):
                                    break
                        lines.insert(insert_pos, '\n' + warning_text)
                        consolidation_report = '\n'.join(lines)
                        logger.warning(f"[Validation] 估值验证发现问题: {validation_result.get('warnings', [])}")
                else:
                    logger.info("[Validation] PE/PB估值数据验证通过")

                # ========== 1.6 股息率交叉验证（对高股息股票）==========
                if is_high_dividend_stock(fundamentals_report):
                    primary_target = extract_target_price(consolidation_report) or extract_target_price(fundamentals_report)
                    div_validation = validate_dividend_yield(
                        fundamentals_report=fundamentals_report,
                        current_price=current_price,
                        primary_target_price=primary_target
                    )

                    if div_validation.get("applicable") and not div_validation.get("passed", True):
                        # 合并股息率验证警告到现有警告
                        div_warnings = div_validation.get("warnings", [])
                        if div_warnings:
                            div_warning_text = "\n## ⚠️ 股息率验证警告\n\n"
                            for i, w in enumerate(div_warnings, 1):
                                div_warning_text += f"{i}. {w}\n"
                            div_warning_text += "\n---\n"

                            # 插入到报告中
                            lines = consolidation_report.split('\n')
                            insert_pos = 0
                            for i, line in enumerate(lines):
                                if '数据一致性警告' in line or line.startswith('## 一、'):
                                    insert_pos = i
                                    break
                            if insert_pos > 0:
                                lines.insert(insert_pos, div_warning_text)
                            else:
                                # 找报告开头插入
                                for i, line in enumerate(lines):
                                    if line.startswith('# ') or line.startswith('**分析日期'):
                                        insert_pos = i + 1
                                        if line.startswith('**分析日期'):
                                            break
                                lines.insert(insert_pos, '\n' + div_warning_text)
                            consolidation_report = '\n'.join(lines)
                            logger.warning(f"[Validation] 股息率验证发现问题: {div_warnings}")
                    elif div_validation.get("applicable"):
                        logger.info("[Validation] 股息率验证通过")
                        details = div_validation.get("details", {})
                        if details.get("dividend_target_price"):
                            logger.info(f"[Validation] 股息率目标价: {details['dividend_target_price']:.2f}元")
        except Exception as e:
            logger.warning(f"[Validation] 估值验证过程出错: {e}")

        # ========== 1.7 投资评级/仓位/目标价一致性校验 ==========
        corrected_signal = None
        try:
            consolidation_report, corrected_signal = _enforce_consolidation_consistency(
                consolidation_report,
                current_price=current_price,
            )
        except Exception as e:
            logger.warning(f"[Consistency] 综合报告一致性校验失败: {e}")

        # ========== 2. 记录本次决策到 Memory ==========
        logger.info(f"[Memory] decision_memory is None: {decision_memory is None}")
        if decision_memory is not None:
            logger.info(f"[Memory] decision_memory type: {type(decision_memory)}")
            logger.info(f"[Memory] has add_decision_with_context: {hasattr(decision_memory, 'add_decision_with_context')}")
            try:
                # 构建当前市场情况摘要
                current_situation = f"""
股票: {ticker} {stock_name}
日期: {trade_date}

【技术面】
{state.get("market_report", "")[:1000]}

【情绪面】
{state.get("sentiment_report", "")[:800]}

【新闻面】
{state.get("news_report", "")[:800]}

【基本面】
{state.get("fundamentals_report", "")[:800]}

【最终决策】
{state.get("final_trade_decision", "")[:500]}
"""
                # 提取决策信息
                final_decision = state.get("final_trade_decision", "")
                decision_info = _extract_decision_info(final_decision, consolidation_report)
                logger.info(f"[Memory] decision_info extracted: {decision_info['decision_type']}")

                # 构建建议摘要
                recommendation = f"""
决策类型: {decision_info['decision_type']}
置信度: {decision_info['confidence']}
目标价: {decision_info.get('target_price', '未指定')}
止损价: {decision_info.get('stop_loss', '未指定')}
建议仓位: {decision_info.get('position_size', '未指定')}%

综合报告摘要:
{consolidation_report[:1500]}
"""
                # 使用 add_decision_with_context 记录（如果可用）
                if hasattr(decision_memory, 'add_decision_with_context'):
                    logger.info(f"[Memory] Calling add_decision_with_context...")
                    record_id = decision_memory.add_decision_with_context(
                        situation=current_situation,
                        recommendation=recommendation,
                        ticker=ticker,
                        decision_date=trade_date,
                        decision_type=decision_info['decision_type'],
                        confidence=decision_info['confidence'],
                        extra_context={
                            "stock_name": stock_name,
                            "target_price": decision_info.get('target_price'),
                            "stop_loss": decision_info.get('stop_loss'),
                            "position_size": decision_info.get('position_size'),
                        }
                    )
                    logger.info(f"✅ 决策已记录到 Memory: {record_id}")
                else:
                    # 使用基本的 add_situations 方法
                    logger.info(f"[Memory] Calling add_situations (fallback)...")
                    decision_memory.add_situations([(current_situation, recommendation)])
                    logger.info(f"✅ 决策已记录到 Memory (基本模式)")

            except Exception as e:
                import traceback
                logger.error(f"❌ 记录决策到 Memory 失败: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")

        result = {
            "consolidation_report": consolidation_report
        }
        if corrected_signal:
            original_decision = state.get("final_trade_decision", "")
            result["final_trade_decision"] = (
                f"{corrected_signal}\n\n"
                "【综合报告一致性校验】评级、仓位和目标价出现冲突，"
                f"最终交易信号已按综合报告修正为 {corrected_signal}。\n\n"
                f"原风险评估决策：\n{original_decision}"
            )
        return result

    return consolidation_node
