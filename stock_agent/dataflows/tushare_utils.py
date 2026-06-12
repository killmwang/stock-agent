"""
Tushare Pro 数据获取模块

提供中国A股数据获取功能，包括：
- 财务报表（利润表、资产负债表、现金流量表）
- 财务指标（ROE、ROA、毛利率等150+指标）
- 每日估值（PE、PB、市值、换手率）
- 业绩预告
- 股东数据
- 资金流向
- 宏观经济数据
"""

import os
import logging
from typing import Optional
from datetime import datetime, timedelta

import tushare as ts
import pandas as pd
import numpy as np

from .retry_utils import (
    retry_with_backoff,
    safe_api_call,
    get_tushare_error_message,
    DataResponse,
    ErrorCategory
)

logger = logging.getLogger(__name__)


# 全局 pro API 实例
_pro_api = None


def get_tushare_token() -> str:
    """
    获取 Tushare Token，优先从环境变量读取，其次从 .env 文件读取
    """
    # 优先环境变量
    token = os.getenv("TUSHARE_TOKEN", "")
    if token:
        return token

    # 其次尝试从 .env 文件读取
    try:
        from dotenv import load_dotenv
        import pathlib
        # 尝试多个可能的 .env 位置
        possible_paths = [
            pathlib.Path(".env"),
            pathlib.Path(__file__).parent.parent.parent / ".env",  # 项目根目录
        ]
        for env_path in possible_paths:
            if env_path.exists():
                load_dotenv(env_path)
                token = os.getenv("TUSHARE_TOKEN", "")
                if token:
                    return token
    except ImportError:
        pass

    # 再次尝试从配置文件读取
    try:
        from stock_agent.default_config import DEFAULT_CONFIG
        token = DEFAULT_CONFIG.get("tushare_token", "")
        if token:
            return token
    except ImportError:
        pass

    return ""


def get_pro_api():
    """获取 Tushare Pro API 实例"""
    global _pro_api
    if _pro_api is None:
        token = get_tushare_token()
        if not token:
            raise ValueError(
                "Tushare Token 未设置。请设置环境变量 TUSHARE_TOKEN 或在 default_config.py 中配置 tushare_token。\n"
                "获取Token: https://tushare.pro/register"
            )
        ts.set_token(token)
        _pro_api = ts.pro_api()
    return _pro_api


def convert_stock_code(stock_code: str) -> str:
    """
    将股票代码转换为 Tushare 格式

    Args:
        stock_code: 6位股票代码 (如 "601899") 或带后缀格式 (如 "601899.SH")

    Returns:
        Tushare 格式的股票代码 (如 "601899.SH")
    """
    # 移除可能的后缀
    clean_code = stock_code.split('.')[0]

    # 根据代码前缀确定交易所
    if clean_code.startswith(('6', '9')):  # 上海
        return f"{clean_code}.SH"
    elif clean_code.startswith(('0', '2', '3')):  # 深圳
        return f"{clean_code}.SZ"
    elif clean_code.startswith(('4', '8')):  # 北交所/新三板
        return f"{clean_code}.BJ"
    else:
        return f"{clean_code}.SH"  # 默认上海


@retry_with_backoff(max_retries=3, initial_delay=1.0, backoff_factor=2.0)
def _fetch_stock_basic(ts_code: str):
    """内部函数：获取股票基本信息（带重试）"""
    pro = get_pro_api()
    return pro.stock_basic(
        ts_code=ts_code,
        fields='ts_code,symbol,name,area,industry,fullname,list_date,market'
    )


def get_stock_basic_info(stock_code: str) -> str:
    """
    获取股票基本信息（支持模糊搜索）

    支持三种输入方式:
    1. 股票代码: "601899", "000001", "300750"
    2. 完整名称: "紫金矿业", "贵州茅台"
    3. 模糊名称: "紫金", "茅台"

    Args:
        stock_code: 股票代码或名称

    Returns:
        股票基本信息的格式化字符串
    """
    try:
        pro = get_pro_api()

        # 1. 判断输入类型：代码 vs 名称
        clean_code = stock_code.strip()

        # 如果是纯数字且长度为6，认为是股票代码
        if clean_code.isdigit() and len(clean_code) == 6:
            ts_code = convert_stock_code(clean_code)
            df = _fetch_stock_basic(ts_code)

            if df.empty:
                return f"[not_found] 未找到股票 {stock_code} 的基本信息。请确认代码正确且股票未退市。"

            row = df.iloc[0]
            return _format_stock_basic_info(row)

        # 2. 名称搜索（精确匹配 + 模糊匹配）
        df_all = pro.stock_basic(
            exchange='',
            list_status='L',  # 只搜索上市中的股票
            fields='ts_code,symbol,name,area,industry,fullname,list_date,market'
        )

        if df_all.empty:
            return "[error] 无法获取股票列表数据"

        # 2.1 精确匹配名称
        exact_match = df_all[df_all['name'] == clean_code]
        if not exact_match.empty:
            row = exact_match.iloc[0]
            return _format_stock_basic_info(row)

        # 2.2 模糊匹配名称（包含关系）
        fuzzy_match = df_all[df_all['name'].str.contains(clean_code, na=False)]

        if fuzzy_match.empty:
            # 2.3 尝试匹配全称
            fuzzy_match = df_all[df_all['fullname'].str.contains(clean_code, na=False)]

        if fuzzy_match.empty:
            return f"[not_found] 未找到匹配 '{stock_code}' 的股票。请尝试更精确的名称或使用6位代码。"

        if len(fuzzy_match) == 1:
            row = fuzzy_match.iloc[0]
            return _format_stock_basic_info(row)

        # 2.4 多个匹配结果，返回候选列表
        result = [f"## 找到 {len(fuzzy_match)} 个匹配结果，请选择具体股票代码：\n"]
        result.append("| 代码 | 名称 | 行业 | 地区 |")
        result.append("|------|------|------|------|")

        for _, row in fuzzy_match.head(10).iterrows():
            ts_code = row.get('ts_code', 'N/A')
            name = row.get('name', 'N/A')
            industry = row.get('industry', 'N/A')
            area = row.get('area', 'N/A')
            result.append(f"| {ts_code} | {name} | {industry} | {area} |")

        if len(fuzzy_match) > 10:
            result.append(f"\n*（仅显示前10个，共{len(fuzzy_match)}个匹配结果）*")

        result.append("\n**提示**: 请使用具体的6位股票代码重新查询。")
        return "\n".join(result)

    except Exception as e:
        logger.error(f"获取股票基本信息失败 [{stock_code}]: {e}")
        return get_tushare_error_message(stock_code, "股票基本信息", e)


def _format_stock_basic_info(row) -> str:
    """格式化单只股票的基本信息"""
    return f"""
## 股票基本信息

- **代码**: {row.get('ts_code', 'N/A')}
- **名称**: {row.get('name', 'N/A')}
- **全称**: {row.get('fullname', 'N/A')}
- **行业**: {row.get('industry', 'N/A')}
- **地区**: {row.get('area', 'N/A')}
- **上市日期**: {row.get('list_date', 'N/A')}
- **市场**: {row.get('market', 'N/A')}
"""


def get_financial_statements(stock_code: str) -> str:
    """
    获取财务报表（利润表、资产负债表、现金流量表）

    Args:
        stock_code: 股票代码

    Returns:
        财务报表的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        result = []
        result.append("# 财务报表分析\n")

        # 获取利润表
        income_df = pro.income(ts_code=ts_code,
                              fields='ts_code,end_date,revenue,operate_profit,total_profit,n_income,basic_eps')
        if not income_df.empty:
            income_df = income_df.head(4)  # 最近4个季度
            result.append("## 利润表（最近4个季度）\n")
            result.append("| 报告期 | 营业收入(亿) | 营业利润(亿) | 利润总额(亿) | 净利润(亿) | 基本EPS |")
            result.append("|--------|------------|------------|------------|----------|---------|")
            for _, row in income_df.iterrows():
                revenue = row['revenue'] / 1e8 if pd.notna(row['revenue']) else 0
                op_profit = row['operate_profit'] / 1e8 if pd.notna(row['operate_profit']) else 0
                total_profit = row['total_profit'] / 1e8 if pd.notna(row['total_profit']) else 0
                n_income = row['n_income'] / 1e8 if pd.notna(row['n_income']) else 0
                eps = row['basic_eps'] if pd.notna(row['basic_eps']) else 0
                result.append(f"| {row['end_date']} | {revenue:.2f} | {op_profit:.2f} | {total_profit:.2f} | {n_income:.2f} | {eps:.3f} |")
            result.append("")

        # 获取资产负债表
        balance_df = pro.balancesheet(ts_code=ts_code,
                                      fields='ts_code,end_date,total_assets,total_liab,total_hldr_eqy_exc_min_int,money_cap')
        if not balance_df.empty:
            balance_df = balance_df.head(4)
            result.append("## 资产负债表（最近4个季度）\n")
            result.append("| 报告期 | 总资产(亿) | 总负债(亿) | 股东权益(亿) | 货币资金(亿) |")
            result.append("|--------|----------|----------|------------|------------|")
            for _, row in balance_df.iterrows():
                total_assets = row['total_assets'] / 1e8 if pd.notna(row['total_assets']) else 0
                total_liab = row['total_liab'] / 1e8 if pd.notna(row['total_liab']) else 0
                equity = row['total_hldr_eqy_exc_min_int'] / 1e8 if pd.notna(row['total_hldr_eqy_exc_min_int']) else 0
                cash = row['money_cap'] / 1e8 if pd.notna(row['money_cap']) else 0
                result.append(f"| {row['end_date']} | {total_assets:.2f} | {total_liab:.2f} | {equity:.2f} | {cash:.2f} |")
            result.append("")

        # 获取现金流量表
        cashflow_df = pro.cashflow(ts_code=ts_code,
                                   fields='ts_code,end_date,n_cashflow_act,n_cashflow_inv_act,n_cash_flows_fnc_act,free_cashflow')
        if not cashflow_df.empty:
            cashflow_df = cashflow_df.head(4)
            result.append("## 现金流量表（最近4个季度）\n")
            result.append("| 报告期 | 经营现金流(亿) | 投资现金流(亿) | 筹资现金流(亿) | 自由现金流(亿) |")
            result.append("|--------|--------------|--------------|--------------|--------------|")
            for _, row in cashflow_df.iterrows():
                cf_op = row['n_cashflow_act'] / 1e8 if pd.notna(row['n_cashflow_act']) else 0
                cf_inv = row['n_cashflow_inv_act'] / 1e8 if pd.notna(row['n_cashflow_inv_act']) else 0
                cf_fin = row['n_cash_flows_fnc_act'] / 1e8 if pd.notna(row['n_cash_flows_fnc_act']) else 0
                fcf = row['free_cashflow'] / 1e8 if pd.notna(row['free_cashflow']) else 0
                result.append(f"| {row['end_date']} | {cf_op:.2f} | {cf_inv:.2f} | {cf_fin:.2f} | {fcf:.2f} |")
            result.append("")

        return "\n".join(result) if result else "未获取到财务报表数据"

    except Exception as e:
        return f"获取财务报表失败: {str(e)}"


def _calc_cycle_position(current: float, min_val: float, max_val: float) -> str:
    """计算当前值在历史区间中的周期位置"""
    if max_val == min_val or pd.isna(current):
        return "—"
    ratio = (current - min_val) / (max_val - min_val)
    if ratio <= 0.25:
        return "**低位**"
    elif ratio <= 0.5:
        return "偏低"
    elif ratio <= 0.75:
        return "偏高"
    else:
        return "**高位**"


def get_financial_indicators(stock_code: str) -> str:
    """
    获取财务指标（ROE、ROA、毛利率、净利率等）

    返回近4季度详细数据 + 5年历史摘要（用于周期股估值）

    Args:
        stock_code: 股票代码

    Returns:
        财务指标的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        # 注意：gross_margin是毛利(金额)，grossprofit_margin才是销售毛利率(百分比)
        df = pro.fina_indicator(ts_code=ts_code,
                               fields='ts_code,end_date,eps,bps,roe,roa,grossprofit_margin,netprofit_margin,debt_to_assets,current_ratio,quick_ratio,netprofit_yoy,tr_yoy')

        if df.empty:
            return f"未找到股票 {stock_code} 的财务指标"

        # 获取20个季度（5年）用于历史分析
        df_full = df.head(20)
        # 近4季度用于详细表格
        df_recent = df.head(4)

        result = []
        result.append("# 财务指标分析\n")

        # === 历史摘要（周期分析用）===
        if len(df_full) >= 8:  # 至少2年数据才显示摘要
            result.append("## 历史指标摘要（周期分析）\n")
            result.append(f"*数据覆盖: {df_full['end_date'].iloc[-1]} ~ {df_full['end_date'].iloc[0]}，共{len(df_full)}个季度*\n")
            result.append("| 指标 | 5年平均 | 5年最高 | 5年最低 | 当前 | 周期位置 |")
            result.append("|------|--------|--------|--------|------|---------|")

            # EPS
            eps_values = df_full['eps'].dropna()
            if len(eps_values) >= 4:
                avg_eps = eps_values.mean()
                max_eps = eps_values.max()
                min_eps = eps_values.min()
                current_eps = eps_values.iloc[0]
                position = _calc_cycle_position(current_eps, min_eps, max_eps)
                result.append(f"| EPS(元) | {avg_eps:.2f} | {max_eps:.2f} | {min_eps:.2f} | {current_eps:.2f} | {position} |")

            # ROE
            roe_values = df_full['roe'].dropna()
            if len(roe_values) >= 4:
                avg_roe = roe_values.mean()
                max_roe = roe_values.max()
                min_roe = roe_values.min()
                current_roe = roe_values.iloc[0]
                position = _calc_cycle_position(current_roe, min_roe, max_roe)
                result.append(f"| ROE(%) | {avg_roe:.1f} | {max_roe:.1f} | {min_roe:.1f} | {current_roe:.1f} | {position} |")

            # 毛利率
            gm_values = df_full['grossprofit_margin'].dropna()
            if len(gm_values) >= 4:
                avg_gm = gm_values.mean()
                max_gm = gm_values.max()
                min_gm = gm_values.min()
                current_gm = gm_values.iloc[0]
                position = _calc_cycle_position(current_gm, min_gm, max_gm)
                result.append(f"| 毛利率(%) | {avg_gm:.1f} | {max_gm:.1f} | {min_gm:.1f} | {current_gm:.1f} | {position} |")

            # 净利润增速
            np_yoy_values = df_full['netprofit_yoy'].dropna()
            if len(np_yoy_values) >= 4:
                avg_np = np_yoy_values.mean()
                max_np = np_yoy_values.max()
                min_np = np_yoy_values.min()
                current_np = np_yoy_values.iloc[0]
                position = _calc_cycle_position(current_np, min_np, max_np)
                result.append(f"| 净利润增速(%) | {avg_np:.1f} | {max_np:.1f} | {min_np:.1f} | {current_np:.1f} | {position} |")

            result.append("")

        # === 近4季度详细数据 ===
        result.append("## 盈利能力指标（近4季）\n")
        result.append("| 报告期 | ROE(%) | ROA(%) | 毛利率(%) | 净利率(%) |")
        result.append("|--------|--------|--------|----------|----------|")
        for _, row in df_recent.iterrows():
            roe = row['roe'] if pd.notna(row['roe']) else 0
            roa = row['roa'] if pd.notna(row['roa']) else 0
            gm = row['grossprofit_margin'] if pd.notna(row['grossprofit_margin']) else 0
            npm = row['netprofit_margin'] if pd.notna(row['netprofit_margin']) else 0
            result.append(f"| {row['end_date']} | {roe:.2f} | {roa:.2f} | {gm:.2f} | {npm:.2f} |")
        result.append("")

        # 每股指标
        result.append("## 每股指标（近4季）\n")
        result.append("| 报告期 | EPS(元) | BPS(元) |")
        result.append("|--------|---------|---------|")
        for _, row in df_recent.iterrows():
            eps = row['eps'] if pd.notna(row['eps']) else 0
            bps = row['bps'] if pd.notna(row['bps']) else 0
            result.append(f"| {row['end_date']} | {eps:.3f} | {bps:.2f} |")
        result.append("")

        # 偿债能力
        result.append("## 偿债能力指标（近4季）\n")
        result.append("| 报告期 | 资产负债率(%) | 流动比率 | 速动比率 |")
        result.append("|--------|--------------|---------|---------|")
        for _, row in df_recent.iterrows():
            debt_ratio = row['debt_to_assets'] if pd.notna(row['debt_to_assets']) else 0
            current = row['current_ratio'] if pd.notna(row['current_ratio']) else 0
            quick = row['quick_ratio'] if pd.notna(row['quick_ratio']) else 0
            result.append(f"| {row['end_date']} | {debt_ratio:.2f} | {current:.2f} | {quick:.2f} |")
        result.append("")

        # 增长率
        result.append("## 增长率指标（近4季）\n")
        result.append("| 报告期 | 净利润同比(%) | 营收同比(%) |")
        result.append("|--------|-------------|-----------|")
        for _, row in df_recent.iterrows():
            np_yoy = row['netprofit_yoy'] if pd.notna(row['netprofit_yoy']) else 0
            tr_yoy = row['tr_yoy'] if pd.notna(row['tr_yoy']) else 0
            result.append(f"| {row['end_date']} | {np_yoy:.2f} | {tr_yoy:.2f} |")
        result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取财务指标失败: {str(e)}"


def get_daily_basic(stock_code: str, trade_date: Optional[str] = None) -> str:
    """
    获取每日估值指标（PE、PB、市值、换手率等）+ 历史估值统计

    Args:
        stock_code: 股票代码
        trade_date: 交易日期 (YYYYMMDD格式)，默认获取最近数据

    Returns:
        估值指标的格式化字符串，包含近3年历史估值统计
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        # 安全转换函数
        def safe_float(val, default=0.0):
            """安全转换为float，处理None和NaN"""
            if val is None or pd.isna(val):
                return default
            return float(val)

        # 获取近3年历史数据用于估值分位计算
        end_date = datetime.now().strftime('%Y%m%d')
        start_date_3y = (datetime.now() - timedelta(days=365*3)).strftime('%Y%m%d')

        df_history = pro.daily_basic(
            ts_code=ts_code,
            start_date=start_date_3y,
            end_date=end_date,
            fields='ts_code,trade_date,pe,pb,ps,total_mv,circ_mv,turnover_rate,volume_ratio,dv_ratio,dv_ttm'
        )

        if df_history.empty:
            return f"未找到股票 {stock_code} 的估值数据"

        # 最近10天数据用于展示
        df_recent = df_history.head(10)

        result = []
        result.append("# 估值指标分析\n")

        # ===== 获取当前股价（daily_basic 不包含 close，需从 daily 获取）=====
        try:
            df_daily = pro.daily(ts_code=ts_code, start_date=end_date, end_date=end_date, fields='trade_date,close')
            if df_daily.empty:
                # 如果当天没数据，往前找最近的交易日
                recent_start = (datetime.now() - timedelta(days=10)).strftime('%Y%m%d')
                df_daily = pro.daily(ts_code=ts_code, start_date=recent_start, end_date=end_date, fields='trade_date,close')

            if not df_daily.empty:
                current_price = safe_float(df_daily.iloc[0]['close'])
                trade_date = df_daily.iloc[0]['trade_date']
                result.append(f"**当前股价**: {current_price:.2f}元（{trade_date}收盘价）\n")
        except Exception as e:
            logger.warning(f"获取收盘价失败: {e}")

        # ===== 历史估值统计（重要！用于确定估值区间依据）=====
        result.append("## 历史估值统计（近3年）\n")
        result.append("**此数据用于确定估值区间依据，多情景估值时必须引用**\n")

        # 过滤有效的 PE/PB 数据（排除负值和异常值）
        pe_valid = df_history['pe'][(df_history['pe'] > 0) & (df_history['pe'] < 1000)]
        pb_valid = df_history['pb'][(df_history['pb'] > 0) & (df_history['pb'] < 50)]

        if len(pe_valid) > 10:
            pe_min = safe_float(pe_valid.min())
            pe_25 = safe_float(pe_valid.quantile(0.25))
            pe_median = safe_float(pe_valid.median())
            pe_75 = safe_float(pe_valid.quantile(0.75))
            pe_max = safe_float(pe_valid.max())
            latest_pe = safe_float(df_recent.iloc[0]['pe']) if pd.notna(df_recent.iloc[0]['pe']) else 0

            # 计算当前PE所处分位
            if latest_pe > 0:
                pe_percentile = safe_float((pe_valid < latest_pe).sum() / len(pe_valid) * 100)
            else:
                pe_percentile = 0

            result.append("| PE指标 | 最小值 | 25%分位 | 中位数 | 75%分位 | 最大值 | 当前值 | **当前分位** |")
            result.append("|--------|--------|---------|--------|---------|--------|--------|-------------|")
            result.append(f"| PE(TTM) | {pe_min:.1f} | {pe_25:.1f} | {pe_median:.1f} | {pe_75:.1f} | {pe_max:.1f} | {latest_pe:.1f} | **{pe_percentile:.0f}%** |")
            result.append("")

            # PE 建议估值区间
            result.append("**PE估值区间依据**：")
            result.append(f"- PE悲观区间下限：{pe_25:.1f}（25%分位）")
            result.append(f"- PE中性参考：{pe_median:.1f}（中位数）")
            result.append(f"- PE乐观区间上限：{pe_75:.1f}（75%分位）")
            if pe_percentile > 80:
                result.append(f"- ⚠️ 当前PE处于历史**{pe_percentile:.0f}%分位**，估值偏高")
            elif pe_percentile < 20:
                result.append(f"- ✅ 当前PE处于历史**{pe_percentile:.0f}%分位**，估值偏低")
            result.append("")

        if len(pb_valid) > 10:
            pb_min = safe_float(pb_valid.min())
            pb_25 = safe_float(pb_valid.quantile(0.25))
            pb_median = safe_float(pb_valid.median())
            pb_75 = safe_float(pb_valid.quantile(0.75))
            pb_max = safe_float(pb_valid.max())
            latest_pb = safe_float(df_recent.iloc[0]['pb']) if pd.notna(df_recent.iloc[0]['pb']) else 0

            # 计算当前PB所处分位
            if latest_pb > 0:
                pb_percentile = safe_float((pb_valid < latest_pb).sum() / len(pb_valid) * 100)
            else:
                pb_percentile = 0

            result.append("| PB指标 | 最小值 | 25%分位 | 中位数 | 75%分位 | 最大值 | 当前值 | **当前分位** |")
            result.append("|--------|--------|---------|--------|---------|--------|--------|-------------|")
            result.append(f"| PB | {pb_min:.2f} | {pb_25:.2f} | {pb_median:.2f} | {pb_75:.2f} | {pb_max:.2f} | {latest_pb:.2f} | **{pb_percentile:.0f}%** |")
            result.append("")

            # 给出建议估值区间
            result.append("**PB估值区间依据**：")
            result.append(f"- PB悲观区间下限：{pb_25:.2f}（25%分位）")
            result.append(f"- PB中性参考：{pb_median:.2f}（中位数）")
            result.append(f"- PB乐观区间上限：{pb_75:.2f}（75%分位）")
            if pb_percentile > 80:
                result.append(f"- ⚠️ 当前PB处于历史**{pb_percentile:.0f}%分位**，估值偏高")
            elif pb_percentile < 20:
                result.append(f"- ✅ 当前PB处于历史**{pb_percentile:.0f}%分位**，估值偏低")
            result.append("")

        # ===== 股息率分析（高息股重要指标）=====
        latest_dv_ratio = safe_float(df_recent.iloc[0].get('dv_ratio')) if 'dv_ratio' in df_recent.columns else 0
        latest_dv_ttm = safe_float(df_recent.iloc[0].get('dv_ttm')) if 'dv_ttm' in df_recent.columns else 0
        # 获取最新PB用于高息股判断
        current_pb = safe_float(df_recent.iloc[0]['pb']) if pd.notna(df_recent.iloc[0]['pb']) else 0

        if latest_dv_ratio > 0 or latest_dv_ttm > 0:
            result.append("## 股息率分析\n")
            result.append(f"- **股息率**: {latest_dv_ratio:.2f}%")
            result.append(f"- **股息率(TTM)**: {latest_dv_ttm:.2f}%")

            # 高息股判断标准
            if latest_dv_ratio >= 5:
                result.append(f"- ✅ **高分红股**: 股息率≥5%，属于高息股")
                if current_pb > 0 and current_pb < 1:
                    result.append(f"- ✅ **低估值高分红**: 股息率{latest_dv_ratio:.2f}% + PB{current_pb:.2f}<1，具备安全边际")
            elif latest_dv_ratio >= 3:
                result.append(f"- 📊 中等分红: 股息率在3%-5%之间")
            elif latest_dv_ratio > 0:
                result.append(f"- 📊 普通分红: 股息率<3%")
            result.append("")

        # ===== 近期估值数据 =====
        result.append("## 每日估值数据（最近10个交易日）\n")
        result.append("| 日期 | PE(TTM) | PB | PS | 股息率(%) | 总市值(亿) | 流通市值(亿) | 换手率(%) |")
        result.append("|------|---------|-----|-----|----------|-----------|------------|----------|")

        for _, row in df_recent.iterrows():
            pe = row['pe'] if pd.notna(row['pe']) else 0
            pb = row['pb'] if pd.notna(row['pb']) else 0
            ps = row['ps'] if pd.notna(row['ps']) else 0
            dv_ratio = row.get('dv_ratio', 0) if pd.notna(row.get('dv_ratio')) else 0
            total_mv = row['total_mv'] / 10000 if pd.notna(row['total_mv']) else 0
            circ_mv = row['circ_mv'] / 10000 if pd.notna(row['circ_mv']) else 0
            turnover = row['turnover_rate'] if pd.notna(row['turnover_rate']) else 0
            result.append(f"| {row['trade_date']} | {pe:.2f} | {pb:.2f} | {ps:.2f} | {dv_ratio:.2f} | {total_mv:.2f} | {circ_mv:.2f} | {turnover:.2f} |")

        result.append("")
        return "\n".join(result)

    except Exception as e:
        logger.error(f"获取估值数据失败 [{stock_code}]: {e}")
        return f"获取估值数据失败: {str(e)}"


def get_forecast(stock_code: str) -> str:
    """
    获取业绩预告

    Args:
        stock_code: 股票代码

    Returns:
        业绩预告的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        df = pro.forecast(ts_code=ts_code)

        if df.empty:
            return f"股票 {stock_code} 暂无业绩预告"

        df = df.head(5)  # 最近5条

        result = []
        result.append("# 业绩预告\n")

        for _, row in df.iterrows():
            result.append(f"## {row['end_date']} 业绩预告\n")
            result.append(f"- **公告日期**: {row.get('ann_date', 'N/A')}")
            result.append(f"- **预告类型**: {row.get('type', 'N/A')}")
            result.append(f"- **业绩变动幅度**: {row.get('p_change_min', 0):.1f}% ~ {row.get('p_change_max', 0):.1f}%")

            net_min = row.get('net_profit_min', 0)
            net_max = row.get('net_profit_max', 0)
            if net_min and net_max:
                result.append(f"- **预计净利润**: {net_min/10000:.2f}亿 ~ {net_max/10000:.2f}亿")

            if row.get('summary'):
                result.append(f"- **预告摘要**: {row['summary'][:200]}...")

            if row.get('change_reason'):
                result.append(f"- **变动原因**: {row['change_reason'][:300]}...")

            result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取业绩预告失败: {str(e)}"


def get_top10_holders(stock_code: str) -> str:
    """
    获取前十大股东

    Args:
        stock_code: 股票代码

    Returns:
        前十大股东的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        # 获取最近两期数据进行对比
        df = pro.top10_holders(ts_code=ts_code)

        if df.empty:
            return f"未找到股票 {stock_code} 的股东数据"

        # 获取最新一期
        latest_date = df['end_date'].max()
        latest_df = df[df['end_date'] == latest_date].head(10)

        result = []
        result.append("# 前十大股东分析\n")
        result.append(f"## 截至 {latest_date} 前十大股东\n")
        result.append("| 排名 | 股东名称 | 持股数量(万股) | 持股比例(%) | 股东类型 |")
        result.append("|------|---------|--------------|------------|---------|")

        for i, (_, row) in enumerate(latest_df.iterrows(), 1):
            name = row['holder_name'][:20] if len(row['holder_name']) > 20 else row['holder_name']
            amount = row['hold_amount'] / 10000 if pd.notna(row['hold_amount']) else 0
            ratio = row['hold_ratio'] if pd.notna(row['hold_ratio']) else 0
            holder_type = row.get('holder_type', 'N/A')
            result.append(f"| {i} | {name} | {amount:.2f} | {ratio:.2f} | {holder_type} |")

        result.append("")

        # 计算机构持股比例
        total_ratio = latest_df['hold_ratio'].sum()
        result.append(f"**前十大股东合计持股**: {total_ratio:.2f}%")
        result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取股东数据失败: {str(e)}"


def get_holder_number(stock_code: str) -> str:
    """
    获取股东人数变化趋势（筹码集中度）

    Args:
        stock_code: 股票代码

    Returns:
        股东人数的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        df = pro.stk_holdernumber(ts_code=ts_code)

        if df.empty:
            return f"未找到股票 {stock_code} 的股东人数数据"

        df = df.head(8)  # 最近8期

        result = []
        result.append("# 股东人数变化（筹码集中度）\n")
        result.append("| 报告期 | 股东人数 | 环比变化 |")
        result.append("|--------|---------|---------|")

        prev_num = None
        for _, row in df.iterrows():
            num = row['holder_num']
            if prev_num:
                change = (num - prev_num) / prev_num * 100
                change_str = f"{change:+.2f}%"
            else:
                change_str = "-"
            result.append(f"| {row['end_date']} | {num:,} | {change_str} |")
            prev_num = num

        result.append("")

        # 分析趋势
        latest = df.iloc[0]['holder_num']
        oldest = df.iloc[-1]['holder_num']
        total_change = (latest - oldest) / oldest * 100

        if total_change < -10:
            trend = "股东人数持续减少，筹码趋于集中，可能有主力吸筹"
        elif total_change > 10:
            trend = "股东人数持续增加，筹码趋于分散，可能有主力出货"
        else:
            trend = "股东人数相对稳定，筹码分布变化不大"

        result.append(f"**趋势分析**: {trend}")
        result.append(f"**期间变化**: {total_change:+.2f}%")
        result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取股东人数数据失败: {str(e)}"


def get_moneyflow(stock_code: str, days: int = 10) -> str:
    """
    获取个股资金流向（含主力态度判断）

    分析维度:
    - 特大单（>100万）: 机构/大户行为
    - 大单（20-100万）: 中大资金行为
    - 主力合计 = 特大单 + 大单: 代表主力资金整体态度

    Args:
        stock_code: 股票代码
        days: 获取天数，默认10天

    Returns:
        资金流向的格式化字符串，包含主力态度判断
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days*2)).strftime('%Y%m%d')

        df = pro.moneyflow(ts_code=ts_code, start_date=start_date, end_date=end_date)

        if df.empty:
            return f"未找到股票 {stock_code} 的资金流向数据"

        df = df.head(days)

        result = []
        result.append(f"# {ts_code} 资金流向分析\n")

        # 计算累计数据
        total_elg_net = 0  # 特大单净额
        total_lg_net = 0   # 大单净额
        total_md_net = 0   # 中单净额
        total_sm_net = 0   # 小单净额
        total_net = 0      # 总净额

        daily_data = []
        for _, row in df.iterrows():
            # 特大单（>100万）
            elg_net = (row.get('buy_elg_amount', 0) - row.get('sell_elg_amount', 0)) / 10000
            # 大单（20-100万）
            lg_net = (row.get('buy_lg_amount', 0) - row.get('sell_lg_amount', 0)) / 10000
            # 中单
            md_net = (row.get('buy_md_amount', 0) - row.get('sell_md_amount', 0)) / 10000
            # 小单
            sm_net = (row.get('buy_sm_amount', 0) - row.get('sell_sm_amount', 0)) / 10000
            # 主力合计
            main_net = elg_net + lg_net

            total_elg_net += elg_net
            total_lg_net += lg_net
            total_md_net += md_net
            total_sm_net += sm_net
            total_net += row.get('net_mf_amount', 0) / 10000

            daily_data.append({
                'date': row['trade_date'],
                'elg_net': elg_net,
                'lg_net': lg_net,
                'main_net': main_net,
                'md_net': md_net,
                'sm_net': sm_net
            })

        # 主力合计
        total_main_net = total_elg_net + total_lg_net

        # 主力态度判断
        if total_main_net > 1000:  # >1000万净流入
            attitude = "强势增持"
            attitude_emoji = "🟢🟢"
        elif total_main_net > 0:
            attitude = "小幅净流入"
            attitude_emoji = "🟢"
        elif total_main_net > -1000:
            attitude = "小幅净流出"
            attitude_emoji = "🔴"
        else:  # < -1000万
            attitude = "持续减持"
            attitude_emoji = "🔴🔴"

        # 输出汇总
        result.append("## 主力资金汇总（近{}日）\n".format(days))
        result.append("| 资金类型 | 净流入(万元) | 说明 |")
        result.append("|---------|-------------|------|")
        result.append(f"| 特大单(>100万) | {total_elg_net:+,.0f} | 机构/大户 |")
        result.append(f"| 大单(20-100万) | {total_lg_net:+,.0f} | 中大资金 |")
        result.append(f"| **主力合计** | **{total_main_net:+,.0f}** | 特大+大单 |")
        result.append(f"| 中单 | {total_md_net:+,.0f} | 中小资金 |")
        result.append(f"| 小单 | {total_sm_net:+,.0f} | 散户 |")
        result.append(f"| 总净流入 | {total_net:+,.0f} | 全部 |")

        result.append(f"\n## 主力态度判断\n")
        result.append(f"- **主力态度**: {attitude_emoji} {attitude}")
        result.append(f"- **主力净流入**: {total_main_net:+,.0f}万元")

        # 资金结构分析
        if total_main_net > 0 and total_sm_net < 0:
            result.append(f"- **资金结构**: 主力吸筹，散户出货（良性换手）")
        elif total_main_net < 0 and total_sm_net > 0:
            result.append(f"- **资金结构**: 主力出货，散户接盘（风险信号）")
        elif total_main_net > 0 and total_sm_net > 0:
            result.append(f"- **资金结构**: 全面流入，市场看多")
        else:
            result.append(f"- **资金结构**: 全面流出，市场看空")

        # 每日明细
        result.append("\n## 每日明细（单位：万元）\n")
        result.append("| 日期 | 特大单净 | 大单净 | 主力净 | 中单净 | 小单净 |")
        result.append("|------|---------|--------|--------|--------|--------|")

        for d in daily_data[:10]:
            result.append(f"| {d['date']} | {d['elg_net']:+.0f} | {d['lg_net']:+.0f} | {d['main_net']:+.0f} | {d['md_net']:+.0f} | {d['sm_net']:+.0f} |")

        result.append("")
        return "\n".join(result)

    except Exception as e:
        return f"获取资金流向数据失败: {str(e)}"


def get_hsgt_flow() -> str:
    """
    获取沪深港通资金流向（北向资金整体流向）

    ⚠️ 数据已停更说明：
    2024年8月19日起，沪深交所调整信息披露机制，北向资金整体流向数据已停止实时披露。
    此函数保留用于向后兼容，但不再返回有效数据。

    建议替代方案：
    - get_hsgt_top10(): 获取每日北向资金十大成交股（仍可用）
    - 前十大股东中的"香港中央结算"持股比例变化可作为参考

    Returns:
        说明信息
    """
    return """# 北向资金整体流向

**⚠️ 数据已停更**

2024年8月19日起，沪深交所调整信息披露机制，北向资金整体流向数据已停止实时披露。

**可用替代数据源：**
1. **北向十大成交股** (`hsgt_top10`)：查看每日北向资金最活跃的股票
2. **前十大股东**: 关注"香港中央结算"持股比例季度变化

请使用以上替代数据进行分析。

注：港交所自2024年8月20日起停止披露北向资金每日数据，个股持股明细(hk_hold)仅有季度快照。
"""


def get_margin_data(stock_code: str) -> str:
    """
    获取融资融券数据

    Args:
        stock_code: 股票代码

    Returns:
        融资融券数据的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')

        df = pro.margin_detail(ts_code=ts_code, start_date=start_date, end_date=end_date)

        if df.empty:
            return f"未找到股票 {stock_code} 的融资融券数据"

        df = df.head(10)  # 最近10天

        result = []
        result.append("# 融资融券分析\n")
        result.append("## 最近10个交易日融资融券数据\n")
        result.append("| 日期 | 融资余额(亿) | 融资买入(亿) | 融券余额(万) | 融券卖出(万股) |")
        result.append("|------|------------|------------|------------|--------------|")

        for _, row in df.iterrows():
            rzye = row.get('rzye', 0) / 1e8 if pd.notna(row.get('rzye')) else 0
            rzmre = row.get('rzmre', 0) / 1e8 if pd.notna(row.get('rzmre')) else 0
            rqye = row.get('rqye', 0) / 1e4 if pd.notna(row.get('rqye')) else 0
            rqmcl = row.get('rqmcl', 0) / 1e4 if pd.notna(row.get('rqmcl')) else 0
            result.append(f"| {row['trade_date']} | {rzye:.2f} | {rzmre:.2f} | {rqye:.2f} | {rqmcl:.2f} |")

        result.append("")

        # 分析趋势
        latest = df.iloc[0]
        oldest = df.iloc[-1]
        rzye_change = (latest.get('rzye', 0) - oldest.get('rzye', 0)) / oldest.get('rzye', 1) * 100 if oldest.get('rzye') else 0

        result.append(f"**融资余额变化**: {rzye_change:+.2f}%")
        if rzye_change > 5:
            result.append("**市场情绪**: 融资余额上升，杠杆资金看多")
        elif rzye_change < -5:
            result.append("**市场情绪**: 融资余额下降，杠杆资金谨慎")
        else:
            result.append("**市场情绪**: 融资余额稳定，市场情绪中性")
        result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取融资融券数据失败: {str(e)}"


def get_pmi() -> str:
    """
    获取PMI采购经理指数

    Returns:
        PMI数据的格式化字符串
    """
    try:
        pro = get_pro_api()

        df = pro.cn_pmi()

        if df.empty:
            return "未获取到PMI数据"

        df = df.head(6)  # 最近6个月

        result = []
        result.append("# 宏观经济指标 - PMI\n")
        result.append("## 采购经理指数（最近6个月）\n")
        result.append("| 月份 | 制造业PMI | 新订单 | 生产 | 从业人员 |")
        result.append("|------|----------|--------|------|---------|")

        for _, row in df.iterrows():
            month = row.get('MONTH', 'N/A')
            pmi = row.get('PMI010000', 0)  # 制造业PMI
            new_order = row.get('PMI010100', 0)  # 新订单
            production = row.get('PMI010200', 0)  # 生产
            employment = row.get('PMI010300', 0)  # 从业人员
            result.append(f"| {month} | {pmi:.1f} | {new_order:.1f} | {production:.1f} | {employment:.1f} |")

        result.append("")

        # 分析
        latest_pmi = df.iloc[0].get('PMI010000', 50)
        if latest_pmi > 50:
            result.append(f"**宏观经济分析**: 制造业PMI为{latest_pmi:.1f}，位于扩张区间，经济景气度向好")
        else:
            result.append(f"**宏观经济分析**: 制造业PMI为{latest_pmi:.1f}，位于收缩区间，经济面临压力")
        result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取PMI数据失败: {str(e)}"


def calculate_ttm_dividend(df: pd.DataFrame, ts_code: str = None) -> tuple:
    """
    计算TTM分红（过去12个月所有分红累加）

    逻辑：
    1. 优先按除权日(ex_date)筛选过去12个月的分红记录
    2. 若无ex_date，按年报日期(end_date)筛选最近完整年度的所有分红
    3. 累加所有符合条件的现金分红

    Args:
        df: 分红数据DataFrame（需包含cash_div, ex_date或end_date列）
        ts_code: 股票代码（用于日志）

    Returns:
        (ttm_dividend, dividend_details, count, date_range)
        - ttm_dividend: TTM分红金额
        - dividend_details: 分红明细列表 [{"date": "2024-06-20", "amount": 0.98, "type": "中期"}]
        - count: 分红次数
        - date_range: 统计区间 "2024-01-19 至 2025-01-19"
    """
    if df.empty:
        return 0, [], 0, ""

    today = datetime.now()
    one_year_ago = today - timedelta(days=365)

    # 筛选有效现金分红记录
    df_valid = df[df['cash_div'].notna() & (df['cash_div'] > 0)].copy()
    if df_valid.empty:
        return 0, [], 0, ""

    # 尝试用除权日筛选过去12个月
    df_ttm = pd.DataFrame()
    date_range = ""

    if 'ex_date' in df_valid.columns:
        # 清洗ex_date列
        df_valid['ex_date_clean'] = df_valid['ex_date'].apply(
            lambda x: str(x) if pd.notna(x) and x != '' else None
        )
        df_with_ex = df_valid[df_valid['ex_date_clean'].notna()].copy()

        if not df_with_ex.empty:
            try:
                df_with_ex['ex_date_dt'] = pd.to_datetime(df_with_ex['ex_date_clean'], errors='coerce')
                mask = df_with_ex['ex_date_dt'] >= one_year_ago
                df_ttm = df_with_ex[mask]

                if not df_ttm.empty:
                    date_range = f"{one_year_ago.strftime('%Y-%m-%d')} 至 {today.strftime('%Y-%m-%d')}"
            except Exception:
                pass

    # 回退：若无有效除权日，取最近完整年度的所有分红
    if df_ttm.empty and 'end_date' in df_valid.columns:
        # 找到最近年度
        df_valid['year'] = df_valid['end_date'].astype(str).str[:4]
        latest_year = df_valid['year'].max()
        if latest_year:
            df_ttm = df_valid[df_valid['year'] == latest_year]
            date_range = f"{latest_year}年度全部分红"

    # 如果仍为空，取最近一条
    if df_ttm.empty:
        df_ttm = df_valid.head(1)
        date_range = "最近一次分红"

    # 累加计算
    ttm_div = float(df_ttm['cash_div'].sum())
    count = len(df_ttm)

    # 生成明细
    details = []
    for _, row in df_ttm.iterrows():
        ex_date = row.get('ex_date', '')
        end_date = row.get('end_date', 'N/A')
        cash_div = float(row.get('cash_div', 0))

        # 推断分红类型
        if pd.notna(end_date):
            month = str(end_date)[4:6] if len(str(end_date)) >= 6 else ""
            if month in ['06', '07']:
                div_type = "中期"
            elif month in ['12', '01']:
                div_type = "年终"
            else:
                div_type = "其他"
        else:
            div_type = ""

        date_str = ex_date if pd.notna(ex_date) and ex_date else end_date
        details.append({
            "date": str(date_str),
            "amount": cash_div,
            "type": div_type,
            "end_date": str(end_date)
        })

    return ttm_div, details, count, date_range


def calculate_historical_yield_percentiles(
    ts_code: str,
    df_dividend: pd.DataFrame,
    years: int = 5
) -> dict:
    """
    计算历史股息率分位数（使用真实历史股价）

    逻辑：
    1. 获取过去N年每年年末的收盘价
    2. 计算每年的年度累计分红
    3. 历史股息率 = 年度分红 / 年末收盘价
    4. 返回25%/50%/75%分位

    Args:
        ts_code: Tushare格式股票代码
        df_dividend: 分红数据DataFrame
        years: 回溯年数，默认5年

    Returns:
        {
            "yield_25_pct": 3.5,   # 较低股息率（乐观情景）
            "yield_50_pct": 4.5,   # 中位数（中性情景）
            "yield_75_pct": 5.5,   # 较高股息率（悲观情景）
            "yield_min": 2.0,
            "yield_max": 7.0,
            "data_source": "历史5年分位计算" | "行业经验值",
            "sample_years": 5,
            "yearly_data": [{"year": "2023", "dividend": 2.55, "close": 41.0, "yield": 6.22}],
            "success": True
        }
    """
    result = {
        "yield_25_pct": None,
        "yield_50_pct": None,
        "yield_75_pct": None,
        "yield_min": None,
        "yield_max": None,
        "data_source": "",
        "sample_years": 0,
        "yearly_data": [],
        "success": False
    }

    try:
        pro = get_pro_api()

        # 1. 获取过去N年的年末收盘价
        current_year = datetime.now().year
        year_end_prices = {}

        for y in range(current_year - years, current_year):
            # 尝试获取该年最后一个交易日的收盘价
            year_end = f"{y}1231"
            year_start = f"{y}1201"

            try:
                df_price = pro.daily(
                    ts_code=ts_code,
                    start_date=year_start,
                    end_date=year_end,
                    fields='trade_date,close'
                )
                if not df_price.empty:
                    # 取该期间最后一个交易日
                    year_end_prices[str(y)] = float(df_price.iloc[0]['close'])
            except Exception:
                continue

        if len(year_end_prices) < 3:
            result["data_source"] = "历史数据不足，无法计算分位"
            return result

        # 2. 计算每年的年度累计分红
        df_valid = df_dividend[df_dividend['cash_div'].notna() & (df_dividend['cash_div'] > 0)].copy()
        if 'end_date' in df_valid.columns:
            df_valid['year'] = df_valid['end_date'].astype(str).str[:4]
        else:
            result["data_source"] = "分红数据缺少年度信息"
            return result

        # 按年度汇总分红
        yearly_dividends = df_valid.groupby('year')['cash_div'].sum().to_dict()

        # 3. 计算各年度股息率
        yearly_yields = []
        yearly_data = []

        for year, close_price in year_end_prices.items():
            div_amount = yearly_dividends.get(year, 0)
            if div_amount > 0 and close_price > 0:
                yield_pct = (div_amount / close_price) * 100
                yearly_yields.append(yield_pct)
                yearly_data.append({
                    "year": year,
                    "dividend": round(div_amount, 3),
                    "close": round(close_price, 2),
                    "yield": round(yield_pct, 2)
                })

        if len(yearly_yields) < 3:
            result["data_source"] = "有效年度数据不足3年"
            return result

        # 4. 计算分位数
        yields_array = np.array(yearly_yields)
        result["yield_min"] = round(float(yields_array.min()), 2)
        result["yield_25_pct"] = round(float(np.percentile(yields_array, 25)), 2)
        result["yield_50_pct"] = round(float(np.percentile(yields_array, 50)), 2)
        result["yield_75_pct"] = round(float(np.percentile(yields_array, 75)), 2)
        result["yield_max"] = round(float(yields_array.max()), 2)
        result["data_source"] = f"历史{len(yearly_yields)}年分位计算"
        result["sample_years"] = len(yearly_yields)
        result["yearly_data"] = sorted(yearly_data, key=lambda x: x['year'], reverse=True)
        result["success"] = True

    except Exception as e:
        logger.warning(f"计算历史股息率分位失败: {e}")
        result["data_source"] = f"计算失败: {str(e)}"

    return result


def identify_special_dividends(df_valid: pd.DataFrame, avg_div: float) -> tuple:
    """
    识别特殊分红记录

    规则：
    1. 单次分红金额超过近5年均值200%
    2. 送股+转增比例>5（高送转）

    Args:
        df_valid: 有效分红记录DataFrame
        avg_div: 平均分红金额

    Returns:
        (special_indices, special_records): 特殊分红索引列表和记录详情
    """
    special_indices = []
    special_records = []

    for idx, row in df_valid.iterrows():
        cash_div = row.get('cash_div', 0) or 0
        stk_div = row.get('stk_div', 0) or 0
        stk_bo = row.get('stk_bo_rate', 0) or 0
        end_date = row.get('end_date', 'N/A')

        # 规则1：超过均值200%
        if avg_div > 0 and cash_div > avg_div * 2:
            special_indices.append(idx)
            special_records.append(f"{end_date}年度{cash_div:.3f}元（超均值200%）")
            continue

        # 规则2：高送转
        if (stk_div + stk_bo) > 5:
            special_indices.append(idx)
            special_records.append(f"{end_date}年度高送转（送{stk_div:.0f}转{stk_bo:.0f}）")
            continue

    return special_indices, special_records


def select_dividend_base(recent_div: float, avg_3y_div: float, avg_5y_div: float) -> tuple:
    """
    分红基数选择规则

    规则：
    1. 默认使用TTM分红（近1年分红）
    2. 若当年分红较3年均值波动超过±50%，则使用近3年平均

    Args:
        recent_div: 近1年分红
        avg_3y_div: 近3年平均分红
        avg_5y_div: 近5年平均分红

    Returns:
        (selected_base, reason): 选定的基数和选择原因
    """
    if avg_3y_div <= 0:
        return recent_div, "TTM分红（无历史对比数据）"

    # 检测异常波动
    volatility = abs(recent_div - avg_3y_div) / avg_3y_div if avg_3y_div > 0 else 0

    if volatility > 0.5:
        # 波动超过50%，使用3年平均
        return avg_3y_div, f"近3年平均（TTM波动{volatility*100:.0f}%>50%）"
    else:
        return recent_div, "TTM分红（近12个月）"


def calculate_cv_excluding_special(df_valid: pd.DataFrame, special_indices: list) -> tuple:
    """
    剔除特殊分红后计算波动系数

    Args:
        df_valid: 有效分红记录DataFrame
        special_indices: 特殊分红索引列表

    Returns:
        (cv, excluded_info, sample_count): 波动系数、剔除信息、有效样本数
    """
    # 剔除特殊分红
    df_normal = df_valid.drop(special_indices, errors='ignore')

    if len(df_normal) < 3:
        return None, "有效常规分红记录不足3年", 0

    # 使用近5年数据计算
    div_values = df_normal.head(5)['cash_div']
    div_std = div_values.std()
    div_mean = div_values.mean()
    cv = (div_std / div_mean) * 100 if div_mean > 0 else 100

    excluded_count = len(special_indices)
    excluded_info = f"剔除{excluded_count}条特殊分红" if excluded_count > 0 else "无特殊分红"

    return cv, excluded_info, len(div_values)


def get_dividend(stock_code: str, current_price: Optional[float] = None) -> str:
    """
    获取分红送股历史及股息率估值数据

    Args:
        stock_code: 股票代码
        current_price: 当前股价（可选，若不提供则自动获取）

    Returns:
        分红历史及股息率估值数据的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        df = pro.dividend(ts_code=ts_code)

        if df.empty:
            return f"未找到股票 {stock_code} 的分红历史"

        # 安全转换函数
        def safe_float(val, default=0.0):
            if val is None or pd.isna(val):
                return default
            return float(val)

        # ===== 获取当前股价（若未提供）=====
        if current_price is None or current_price <= 0:
            try:
                end_date = datetime.now().strftime('%Y%m%d')
                recent_start = (datetime.now() - timedelta(days=10)).strftime('%Y%m%d')
                df_daily = pro.daily(ts_code=ts_code, start_date=recent_start, end_date=end_date, fields='trade_date,close')
                if not df_daily.empty:
                    current_price = safe_float(df_daily.iloc[0]['close'])
            except Exception as e:
                logger.warning(f"获取收盘价失败: {e}")
                current_price = 0.0

        # ===== 分红历史表格 =====
        df_display = df.head(10)  # 展示最近10次

        result = []
        result.append("# 分红送股历史\n")
        result.append("| 分红年度 | 每股分红(元) | 送股(股) | 转增(股) | 除权日 |")
        result.append("|---------|------------|---------|---------|--------|")

        for _, row in df_display.iterrows():
            end_date = row.get('end_date', 'N/A')
            cash_div = safe_float(row.get('cash_div', 0))
            stk_div = safe_float(row.get('stk_div', 0))
            stk_bo = safe_float(row.get('stk_bo_rate', 0))
            ex_date = row.get('ex_date', 'N/A')
            result.append(f"| {end_date} | {cash_div:.3f} | {stk_div:.2f} | {stk_bo:.2f} | {ex_date} |")

        result.append("")

        # ===== 提取分红数据 =====
        # 筛选有效分红记录（现金分红>0）
        df_valid = df[df['cash_div'].notna() & (df['cash_div'] > 0)].copy()
        record_count = len(df_valid)

        # ===== 计算TTM分红（累加过去12个月所有分红）=====
        ttm_div, ttm_details, ttm_count, ttm_date_range = calculate_ttm_dividend(df, ts_code)

        # 近3年平均分红（按年度汇总后平均，需按年份降序排列取最近N年）
        if 'end_date' in df_valid.columns:
            df_valid['year'] = df_valid['end_date'].astype(str).str[:4]
            yearly_sums = df_valid.groupby('year')['cash_div'].sum().sort_index(ascending=False)
            avg_3y_div = safe_float(yearly_sums.head(3).mean()) if len(yearly_sums) >= 1 else 0
            avg_5y_div = safe_float(yearly_sums.head(5).mean()) if len(yearly_sums) >= 1 else 0
        else:
            avg_3y_div = safe_float(df_valid.head(3)['cash_div'].mean()) if record_count >= 1 else 0
            avg_5y_div = safe_float(df_valid.head(5)['cash_div'].mean()) if record_count >= 1 else 0

        # ===== 识别特殊分红 =====
        special_indices, special_records = identify_special_dividends(df_valid.head(5), avg_5y_div)

        # ===== 选择估值基数（使用TTM分红）=====
        selected_base, base_reason = select_dividend_base(ttm_div, avg_3y_div, avg_5y_div)

        # ===== 输出TTM分红信息 =====
        result.append("## 分红数据汇总\n")
        result.append(f"**TTM分红（近12个月累计）**: {ttm_div:.3f}元")

        if ttm_count > 1:
            result.append(f"- 分红次数：{ttm_count}次")
            result.append(f"- 统计区间：{ttm_date_range}")
            result.append("- 分红明细：")
            for detail in ttm_details:
                type_str = f"（{detail['type']}）" if detail['type'] else ""
                result.append(f"  - {detail['date']}: {detail['amount']:.3f}元{type_str}")
        elif ttm_count == 1:
            result.append(f"- 统计说明：{ttm_date_range}（单次分红）")

        result.append("")
        result.append(f"**近3年年均分红**: {avg_3y_div:.3f}元/年")
        result.append(f"**近5年年均分红**: {avg_5y_div:.3f}元/年")
        result.append("")
        result.append(f"**📌 估值基数选择**: {selected_base:.3f}元 ({base_reason})")
        if special_records:
            result.append(f"**⚠️ 特殊分红识别**: {'; '.join(special_records)}")
        result.append("")

        # ===== 当前股息率计算（使用TTM分红）=====
        if current_price > 0 and ttm_div > 0:
            current_yield = (ttm_div / current_price) * 100
            result.append(f"**当前股价**: {current_price:.2f}元")
            result.append(f"**当前股息率**: {current_yield:.2f}%（TTM分红{ttm_div:.3f}元 ÷ 股价{current_price:.2f}元）")
            result.append("")

            # ===== 股息率历史分位计算（使用真实历史股价）=====
            hist_yield = calculate_historical_yield_percentiles(ts_code, df, years=5)

            if hist_yield["success"]:
                result.append("## 股息率历史分位（真实历史股价计算）\n")
                result.append("| 最小值 | 25%分位 | 中位数 | 75%分位 | 最大值 | 样本年数 |")
                result.append("|--------|---------|--------|---------|--------|---------|")
                result.append(f"| {hist_yield['yield_min']:.2f}% | {hist_yield['yield_25_pct']:.2f}% | {hist_yield['yield_50_pct']:.2f}% | {hist_yield['yield_75_pct']:.2f}% | {hist_yield['yield_max']:.2f}% | {hist_yield['sample_years']}年 |")
                result.append(f"\n**数据来源**: {hist_yield['data_source']}")
                result.append("")

                # 年度明细表
                if hist_yield["yearly_data"]:
                    result.append("**年度股息率明细**:")
                    result.append("| 年度 | 年度分红(元) | 年末股价(元) | 股息率 |")
                    result.append("|------|-------------|-------------|--------|")
                    for yd in hist_yield["yearly_data"][:5]:
                        result.append(f"| {yd['year']} | {yd['dividend']:.3f} | {yd['close']:.2f} | {yd['yield']:.2f}% |")
                    result.append("")

                # 当前股息率分位评估
                if current_yield <= hist_yield['yield_25_pct']:
                    result.append(f"**当前股息率{current_yield:.2f}%位于历史低位**（<25%分位），股价可能被高估")
                elif current_yield >= hist_yield['yield_75_pct']:
                    result.append(f"**当前股息率{current_yield:.2f}%位于历史高位**（>75%分位），股价可能被低估")
                else:
                    result.append(f"**当前股息率{current_yield:.2f}%位于历史中位区间**")
                result.append("")

            else:
                # 回退行业固定区间
                result.append("## 股息率参考区间（行业经验值）\n")
                result.append(f"⚠️ {hist_yield['data_source']}，使用行业经验值，**置信度-10%**\n")
                result.append("| 行业 | 25%分位 | 中位数 | 75%分位 | 说明 |")
                result.append("|------|---------|--------|---------|------|")
                result.append("| 公用事业(电力) | 3.0% | 3.5% | 4.5% | 长江电力等 |")
                result.append("| 银行 | 4.0% | 5.0% | 6.0% | 国有大行 |")
                result.append("| 煤炭 | 4.0% | 5.5% | 7.0% | 中国神华等 |")
                result.append("| 高速公路 | 4.0% | 5.0% | 7.0% | 现金流稳定 |")
                result.append("| 港口 | 3.5% | 4.5% | 6.0% | 周期性较弱 |")
                result.append("")

            # ===== 股息率目标价参考（使用历史分位或行业经验值）=====
            result.append("## 股息率目标价参考\n")
            result.append("**用于高股息股票（公用事业/银行/煤炭/高速公路）的估值交叉验证**\n")
            result.append(f"**估值基数**: {selected_base:.3f}元 ({base_reason})\n")

            # 使用历史分位或行业经验值生成目标股息率
            if hist_yield["success"]:
                yield_pessimistic = hist_yield['yield_75_pct']  # 高股息率 = 悲观
                yield_neutral = hist_yield['yield_50_pct']      # 中位数 = 中性
                yield_optimistic = hist_yield['yield_25_pct']   # 低股息率 = 乐观
                yield_source = "历史分位"
            else:
                # 默认使用煤炭/高股息行业经验值
                yield_pessimistic = 7.0
                yield_neutral = 5.5
                yield_optimistic = 4.0
                yield_source = "行业经验值"

            result.append(f"**目标股息率来源**: {yield_source}\n")
            result.append("| 情景 | 目标股息率 | 对应目标价 | 较当前涨跌幅 |")
            result.append("|------|-----------|-----------|------------|")

            scenarios = [
                ("悲观（高收益要求）", yield_pessimistic),
                ("中性", yield_neutral),
                ("乐观（低收益接受）", yield_optimistic),
            ]

            for scenario, target_yield in scenarios:
                if selected_base > 0 and target_yield > 0:
                    target_price = selected_base / (target_yield / 100)
                    change_pct = (target_price - current_price) / current_price * 100
                    result.append(f"| {scenario} | {target_yield:.1f}% | {target_price:.2f}元 | {change_pct:+.1f}% |")

            # 计算加权目标价
            if selected_base > 0:
                weighted_price = (
                    0.25 * (selected_base / (yield_pessimistic / 100)) +
                    0.50 * (selected_base / (yield_neutral / 100)) +
                    0.25 * (selected_base / (yield_optimistic / 100))
                )
                weighted_change = (weighted_price - current_price) / current_price * 100
                result.append(f"| **加权（25/50/25）** | - | **{weighted_price:.2f}元** | **{weighted_change:+.1f}%** |")

            result.append("")
            result.append(f"**计算公式**: 目标价 = 估值基数({selected_base:.3f}元) ÷ 目标股息率")
            result.append("")

            # ===== 分红稳定性评估（剔除特殊分红）=====
            if record_count >= 3:
                div_cv, excluded_info, sample_count = calculate_cv_excluding_special(
                    df_valid.head(5), special_indices
                )

                result.append("## 分红稳定性评估\n")

                if special_records:
                    result.append(f"**剔除记录**: {'; '.join(special_records)}")
                    result.append(f"**有效样本**: 近{sample_count}年常规分红（{excluded_info}）")
                    result.append("")

                if div_cv is not None:
                    if div_cv < 10:
                        result.append(f"✅ **分红非常稳定**：波动系数{div_cv:.1f}%（<10%），适合股息率估值")
                    elif div_cv < 30:
                        result.append(f"⚠️ **分红较稳定**：波动系数{div_cv:.1f}%（10%-30%），股息率估值可参考")
                    else:
                        result.append(f"❌ **分红波动较大**：波动系数{div_cv:.1f}%（>30%），股息率估值置信度较低")
                else:
                    result.append(f"⚠️ {excluded_info}，无法计算波动系数")
                result.append("")

        else:
            if ttm_div <= 0:
                result.append("⚠️ **无现金分红记录**，不适用股息率估值法")
            elif current_price <= 0:
                result.append("⚠️ 无法获取当前股价，股息率相关计算略过")
            result.append("")

        return "\n".join(result)

    except Exception as e:
        logger.error(f"获取分红历史失败: {str(e)}")
        return f"获取分红历史失败: {str(e)}"


def get_top_list(stock_code: str, days: int = 30) -> str:
    """
    获取龙虎榜数据

    Args:
        stock_code: 股票代码
        days: 查询天数，默认30天

    Returns:
        龙虎榜数据的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        # TuShare top_list API要求使用trade_date参数
        # 先获取最近的交易日历，然后逐日查询
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')

        # 获取交易日历
        cal_df = pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date, is_open='1')
        if cal_df.empty:
            return f"获取交易日历失败"

        trade_dates = cal_df.sort_values('cal_date', ascending=False)['cal_date'].head(days).tolist()

        all_data = []
        for trade_date in trade_dates[:10]:  # 最多查询最近10个交易日
            try:
                df = pro.top_list(trade_date=trade_date, ts_code=ts_code)
                if not df.empty:
                    all_data.append(df)
            except Exception:
                continue

        if not all_data:
            return f"股票 {stock_code} 近期未上龙虎榜"

        df = pd.concat(all_data, ignore_index=True)
        df = df.sort_values('trade_date', ascending=False)

        result = []
        result.append("# 龙虎榜分析\n")

        for _, row in df.iterrows():
            result.append(f"## {row['trade_date']} 龙虎榜\n")
            result.append(f"- **上榜原因**: {row.get('reason', 'N/A')}")
            result.append(f"- **收盘价**: {row.get('close', 0):.2f}元")
            result.append(f"- **涨跌幅**: {row.get('pct_change', 0):.2f}%")
            result.append(f"- **换手率**: {row.get('turnover_rate', 0):.2f}%")

            l_buy = row.get('l_buy', 0) / 1e8 if pd.notna(row.get('l_buy')) else 0
            l_sell = row.get('l_sell', 0) / 1e8 if pd.notna(row.get('l_sell')) else 0
            net = row.get('net_amount', 0) / 1e8 if pd.notna(row.get('net_amount')) else 0

            result.append(f"- **龙虎榜买入**: {l_buy:.2f}亿元")
            result.append(f"- **龙虎榜卖出**: {l_sell:.2f}亿元")
            result.append(f"- **净买入**: {net:+.2f}亿元")
            result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取龙虎榜数据失败: {str(e)}"


# 综合数据获取函数（供工具调用）

def get_china_stock_comprehensive(stock_code: str, trade_date: Optional[str] = None) -> str:
    """
    获取中国A股综合数据

    Args:
        stock_code: 股票代码
        trade_date: 交易日期

    Returns:
        综合数据的格式化字符串
    """
    result = []

    # 基本信息
    result.append(get_stock_basic_info(stock_code))

    # 估值数据
    result.append(get_daily_basic(stock_code, trade_date))

    # 财务指标
    result.append(get_financial_indicators(stock_code))

    # 业绩预告
    result.append(get_forecast(stock_code))

    return "\n".join(result)


def get_china_stock_fundamentals(stock_code: str) -> str:
    """
    获取基本面综合数据

    Args:
        stock_code: 股票代码

    Returns:
        基本面数据的格式化字符串
    """
    result = []

    # 财务报表
    result.append(get_financial_statements(stock_code))

    # 财务指标
    result.append(get_financial_indicators(stock_code))

    # 业绩预告
    result.append(get_forecast(stock_code))

    # 分红历史
    result.append(get_dividend(stock_code))

    return "\n".join(result)


def get_china_stock_sentiment(stock_code: str) -> str:
    """
    获取市场情绪综合数据

    Args:
        stock_code: 股票代码

    Returns:
        市场情绪数据的格式化字符串
    """
    result = []

    # 资金流向
    result.append(get_moneyflow(stock_code))

    # 北向资金（使用十大成交股替代已停更的整体流向）
    result.append(get_hsgt_top10())

    # 融资融券
    result.append(get_margin_data(stock_code))

    # 股东数据（含香港中央结算持股比例）
    result.append(get_top10_holders(stock_code))
    result.append(get_holder_number(stock_code))

    return "\n".join(result)


# ============= 新增数据源函数（Phase 1.1 扩展） =============


# ============= 已废弃函数说明 =============
#
# get_hk_hold() 函数已移除
# 废弃原因：港交所自2024年8月20日起停止披露北向资金每日数据
# hk_hold API 目前仅返回季度数据（每年3/6/9/12月），无法用于短期交易分析
#
# 替代方案：
# 1. get_hsgt_top10() - 查看每日北向资金十大成交股
# 2. get_top10_holders() - 通过"香港中央结算"持股比例季度变化判断外资态度
# ============================================


def get_hsgt_top10(trade_date: Optional[str] = None) -> str:
    """
    获取沪深港通十大成交股

    Args:
        trade_date: 交易日期 YYYYMMDD，默认最近交易日

    Returns:
        格式化字符串，包含买入/卖出成交额前10、净买入金额
    """
    try:
        pro = get_pro_api()

        if trade_date is None:
            # 获取最近交易日
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=10)).strftime('%Y%m%d')
            # 先获取一条数据确定最新交易日
            df_check = pro.hsgt_top10(start_date=start_date, end_date=end_date)
            if df_check.empty:
                return "未获取到沪深港通十大成交股数据"
            trade_date = df_check['trade_date'].max()

        # 获取沪股通十大 (market_type='1') 和深股通十大 (market_type='3')
        df_sh = pro.hsgt_top10(trade_date=trade_date, market_type='1')
        df_sz = pro.hsgt_top10(trade_date=trade_date, market_type='3')

        result = []
        result.append(f"# 沪深港通十大成交股 ({trade_date})\n")

        if not df_sh.empty:
            result.append("## 沪股通十大成交股\n")
            result.append("| 排名 | 代码 | 名称 | 收盘价 | 涨跌幅(%) | 净买入(万) |")
            result.append("|------|------|------|--------|----------|-----------|")
            for _, row in df_sh.head(10).iterrows():
                rank = row.get('rank', 0)
                ts_code = row.get('ts_code', 'N/A')
                name = row.get('name', 'N/A')[:8]
                close = row.get('close', 0)
                change = row.get('change', 0) if pd.notna(row.get('change')) else 0
                net_amount = row.get('net_amount', 0) / 10000 if pd.notna(row.get('net_amount')) else 0
                result.append(f"| {rank} | {ts_code} | {name} | {close:.2f} | {change:.2f} | {net_amount:+.2f} |")
            result.append("")

        if not df_sz.empty:
            result.append("## 深股通十大成交股\n")
            result.append("| 排名 | 代码 | 名称 | 收盘价 | 涨跌幅(%) | 净买入(万) |")
            result.append("|------|------|------|--------|----------|-----------|")
            for _, row in df_sz.head(10).iterrows():
                rank = row.get('rank', 0)
                ts_code = row.get('ts_code', 'N/A')
                name = row.get('name', 'N/A')[:8]
                close = row.get('close', 0)
                change = row.get('change', 0) if pd.notna(row.get('change')) else 0
                net_amount = row.get('net_amount', 0) / 10000 if pd.notna(row.get('net_amount')) else 0
                result.append(f"| {rank} | {ts_code} | {name} | {close:.2f} | {change:.2f} | {net_amount:+.2f} |")
            result.append("")

        return "\n".join(result) if result else "未获取到沪深港通十大成交股数据"

    except Exception as e:
        return f"获取沪深港通十大成交股数据失败: {str(e)}"


def get_block_trade(stock_code: str, days: int = 30) -> str:
    """
    获取大宗交易数据

    Args:
        stock_code: 股票代码
        days: 获取天数

    Returns:
        格式化字符串，包含交易日期、成交价、折溢价率、买卖营业部
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')

        df = pro.block_trade(ts_code=ts_code, start_date=start_date, end_date=end_date)

        if df.empty:
            return f"股票 {stock_code} 近期无大宗交易记录"

        df = df.head(20)  # 最近20笔

        result = []
        result.append("# 大宗交易分析\n")
        result.append(f"## 近期大宗交易记录（{stock_code}）\n")
        result.append("| 日期 | 成交价 | 成交量(万股) | 成交额(万) | 折溢价(%) | 买方 | 卖方 |")
        result.append("|------|--------|------------|----------|----------|------|------|")

        total_vol = 0
        total_amount = 0
        discount_trades = 0

        for _, row in df.iterrows():
            trade_date = row.get('trade_date', 'N/A')
            price = row.get('price', 0)
            vol = row.get('vol', 0) / 10000 if pd.notna(row.get('vol')) else 0  # 股转万股
            amount = row.get('amount', 0) / 10000 if pd.notna(row.get('amount')) else 0  # 元转万元

            # 计算折溢价率（需要当日收盘价）
            # 简化处理：显示为N/A，或通过其他方式获取
            discount = "N/A"

            buyer = row.get('buyer', 'N/A')[:10] if row.get('buyer') else 'N/A'
            seller = row.get('seller', 'N/A')[:10] if row.get('seller') else 'N/A'

            result.append(f"| {trade_date} | {price:.2f} | {vol:.2f} | {amount:.2f} | {discount} | {buyer} | {seller} |")

            total_vol += vol
            total_amount += amount

        result.append("")
        result.append(f"**统计汇总**: 共{len(df)}笔大宗交易")
        result.append(f"**累计成交**: {total_vol:.2f}万股，{total_amount:.2f}万元")

        # 分析
        if len(df) >= 5:
            result.append("")
            result.append("**风险提示**: 近期大宗交易频繁，需关注是否存在减持压力")

        result.append("")
        return "\n".join(result)

    except Exception as e:
        return f"获取大宗交易数据失败: {str(e)}"


def get_pledge_stat(stock_code: str) -> str:
    """
    获取股权质押统计

    Args:
        stock_code: 股票代码

    Returns:
        格式化字符串，包含质押总股数、质押比例、风险提示
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        df = pro.pledge_stat(ts_code=ts_code)

        if df.empty:
            return f"未找到股票 {stock_code} 的股权质押数据"

        df = df.head(8)  # 最近8期

        result = []
        result.append("# 股权质押分析\n")
        result.append("## 股权质押统计\n")
        result.append("| 截止日期 | 质押次数 | 无限售质押(万股) | 限售质押(万股) | 总股本(万股) | 质押比例(%) |")
        result.append("|---------|---------|----------------|--------------|------------|------------|")

        latest_ratio = 0
        for _, row in df.iterrows():
            end_date = row.get('end_date', 'N/A')
            pledge_count = row.get('pledge_count', 0)
            unrest_pledge = row.get('unrest_pledge', 0) / 10000 if pd.notna(row.get('unrest_pledge')) else 0
            rest_pledge = row.get('rest_pledge', 0) / 10000 if pd.notna(row.get('rest_pledge')) else 0
            total_share = row.get('total_share', 0) / 10000 if pd.notna(row.get('total_share')) else 0
            pledge_ratio = row.get('pledge_ratio', 0) if pd.notna(row.get('pledge_ratio')) else 0

            if latest_ratio == 0:
                latest_ratio = pledge_ratio

            result.append(f"| {end_date} | {pledge_count} | {unrest_pledge:.2f} | {rest_pledge:.2f} | {total_share:.2f} | {pledge_ratio:.2f} |")

        result.append("")

        # 风险评估
        if latest_ratio > 50:
            risk_level = "【高风险】质押比例超过50%，存在重大平仓风险"
        elif latest_ratio > 30:
            risk_level = "【中风险】质押比例较高，需密切关注股价波动"
        elif latest_ratio > 10:
            risk_level = "【低风险】质押比例适中，风险可控"
        else:
            risk_level = "【安全】质押比例较低，无明显风险"

        result.append(f"**当前质押比例**: {latest_ratio:.2f}%")
        result.append(f"**风险评估**: {risk_level}")
        result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取股权质押数据失败: {str(e)}"


def get_share_float(stock_code: str) -> str:
    """
    获取限售解禁日历（精简版，只返回汇总和前20大股东）

    Args:
        stock_code: 股票代码

    Returns:
        格式化字符串，包含解禁汇总统计和前20大股东明细
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        df = pro.share_float(ts_code=ts_code)

        if df.empty:
            return f"未找到股票 {stock_code} 的解禁数据"

        # 筛选未来6个月的解禁
        today = datetime.now().strftime('%Y%m%d')
        future_date = (datetime.now() + timedelta(days=180)).strftime('%Y%m%d')

        # 过滤未来解禁
        df_future = df[(df['float_date'] >= today) & (df['float_date'] <= future_date)].copy()

        result = []
        result.append("# 限售解禁日历\n")

        if df_future.empty:
            result.append("## 未来6个月无重大解禁\n")
            result.append("该股票未来6个月内暂无限售股解禁安排。\n")
        else:
            # 计算汇总统计
            df_future['float_share_wan'] = df_future['float_share'].fillna(0) / 10000
            total_float = df_future['float_share_wan'].sum()
            total_ratio = df_future['float_ratio'].fillna(0).sum()
            total_holders = len(df_future)

            # 按解禁日期分组统计
            date_summary = df_future.groupby('float_date').agg({
                'float_share_wan': 'sum',
                'float_ratio': 'sum'
            }).reset_index()

            result.append("## 解禁汇总统计\n")
            result.append(f"- **未来6个月累计解禁**: {total_float:.2f}万股")
            result.append(f"- **占总股本比例**: {total_ratio:.2f}%")
            result.append(f"- **解禁股东数量**: {total_holders}个")
            result.append("")

            # 按日期汇总（最多显示5个日期）
            result.append("## 解禁日期分布\n")
            result.append("| 解禁日期 | 解禁数量(万股) | 占总股本(%) |")
            result.append("|---------|--------------|------------|")
            for _, row in date_summary.head(5).iterrows():
                result.append(f"| {row['float_date']} | {row['float_share_wan']:.2f} | {row['float_ratio']:.2f} |")
            if len(date_summary) > 5:
                result.append(f"| ... | 共{len(date_summary)}个解禁日期 | ... |")
            result.append("")

            # 只显示前20大股东（按解禁数量降序）
            df_top20 = df_future.nlargest(20, 'float_share_wan')

            result.append("## 前20大解禁股东\n")
            result.append("| 解禁日期 | 解禁数量(万股) | 占总股本(%) | 股东名称 | 解禁类型 |")
            result.append("|---------|--------------|------------|---------|---------|")

            for _, row in df_top20.iterrows():
                float_date = row.get('float_date', 'N/A')
                float_share = row['float_share_wan']
                float_ratio = row.get('float_ratio', 0) if pd.notna(row.get('float_ratio')) else 0
                holder_name = row.get('holder_name', 'N/A')[:20] if row.get('holder_name') else 'N/A'
                share_type = row.get('share_type', 'N/A')

                result.append(f"| {float_date} | {float_share:.2f} | {float_ratio:.2f} | {holder_name} | {share_type} |")

            if total_holders > 20:
                result.append(f"\n*注：共{total_holders}个股东，仅显示前20大*")

            result.append("")

            # 风险提示
            if total_float > 10000:  # 超过1亿股
                result.append("**风险提示**: 解禁规模较大，可能对股价形成压力")
            elif total_ratio > 10:  # 占比超过10%
                result.append("**风险提示**: 解禁占比较高，关注减持公告")

        result.append("")

        # 显示历史解禁情况（最多5条）
        df_past = df[df['float_date'] < today].head(5)
        if not df_past.empty:
            result.append("## 近期已解禁记录\n")
            result.append("| 解禁日期 | 解禁数量(万股) | 占总股本(%) |")
            result.append("|---------|--------------|------------|")
            for _, row in df_past.iterrows():
                float_date = row.get('float_date', 'N/A')
                float_share = row.get('float_share', 0) / 10000 if pd.notna(row.get('float_share')) else 0
                float_ratio = row.get('float_ratio', 0) if pd.notna(row.get('float_ratio')) else 0
                result.append(f"| {float_date} | {float_share:.2f} | {float_ratio:.2f} |")
            result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取解禁数据失败: {str(e)}"


def get_index_daily(index_code: str, days: int = 60) -> str:
    """
    获取指数日线行情

    Args:
        index_code: 指数代码（如 000300.SH 沪深300, 399006.SZ 创业板指, 399318.SZ 有色金属）
        days: 获取天数

    Returns:
        格式化字符串，包含指数收盘价、涨跌幅、成交额
    """
    try:
        pro = get_pro_api()

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')

        df = pro.index_daily(ts_code=index_code, start_date=start_date, end_date=end_date)

        if df.empty:
            return f"未找到指数 {index_code} 的行情数据"

        df = df.head(days)

        # 获取指数名称
        index_name_map = {
            '000300.SH': '沪深300',
            '399006.SZ': '创业板指',
            '399318.SZ': '国证有色',
            '000016.SH': '上证50',
            '399001.SZ': '深证成指',
            '000001.SH': '上证指数',
        }
        index_name = index_name_map.get(index_code, index_code)

        result = []
        result.append(f"# {index_name}({index_code}) 行情分析\n")
        result.append(f"## 近期走势（最近{min(len(df), 20)}个交易日）\n")
        result.append("| 日期 | 收盘 | 涨跌幅(%) | 成交额(亿) | 振幅(%) |")
        result.append("|------|------|----------|----------|--------|")

        for _, row in df.head(20).iterrows():
            trade_date = row.get('trade_date', 'N/A')
            close = row.get('close', 0)
            pct_chg = row.get('pct_chg', 0) if pd.notna(row.get('pct_chg')) else 0
            amount = row.get('amount', 0) / 100000 if pd.notna(row.get('amount')) else 0  # 千元转亿元

            # 计算振幅
            high = row.get('high', 0)
            low = row.get('low', 0)
            pre_close = row.get('pre_close', close)
            amplitude = (high - low) / pre_close * 100 if pre_close > 0 else 0

            result.append(f"| {trade_date} | {close:.2f} | {pct_chg:+.2f} | {amount:.2f} | {amplitude:.2f} |")

        result.append("")

        # 计算统计指标
        latest_close = df.iloc[0]['close']
        oldest_close = df.iloc[-1]['close']
        period_return = (latest_close - oldest_close) / oldest_close * 100

        result.append(f"**区间涨跌幅**: {period_return:+.2f}%（近{len(df)}个交易日）")
        result.append(f"**最新收盘**: {latest_close:.2f}")

        # 均值分析
        avg_amount = df['amount'].mean() / 100000
        result.append(f"**日均成交额**: {avg_amount:.2f}亿元")
        result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取指数行情数据失败: {str(e)}"


def get_sector_benchmark_data(stock_code: str, days: int = 60) -> str:
    """
    智能获取个股所属行业的板块指数数据。
    只需传入个股代码，自动查找其行业并返回对应行业指数走势。

    这是一个"傻瓜化"工具，Agent只需要传入股票代码，Python内部会自动：
    1. 查询股票所属行业
    2. 映射到对应的行业指数（采用三级fallback策略）
    3. 获取指数数据并返回

    三级 fallback 策略：
    - Level 1: 行业映射（根据行业名称匹配对应行业指数）
    - Level 2: 市场板块（科创板→科创50，创业板→创业板指）
    - Level 3: 默认兜底（沪深300）

    Args:
        stock_code: 股票代码，如 "601899", "000001", "300750"
        days: 获取天数，默认60天

    Returns:
        包含行业名称、指数代码、指数走势、相对强弱分析的完整报告
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        # 1. 获取个股行业 + 市场板块
        df_basic = pro.stock_basic(ts_code=ts_code, fields='ts_code,name,industry,market')
        if df_basic.empty:
            return f"[not_found] 无法获取股票 {stock_code} 的行业信息"

        stock_name = df_basic.iloc[0]['name']
        industry_name = df_basic.iloc[0]['industry']
        market = df_basic.iloc[0].get('market', '')  # 市场板块字段

        # 2. 三级 fallback 策略
        mapping = None
        fallback_source = "行业匹配"

        # 2.1 先尝试行业映射
        if industry_name in INDUSTRY_TO_INDEX:
            mapping = INDUSTRY_TO_INDEX[industry_name]
        else:
            # 2.2 行业无匹配，根据市场板块选择
            fallback_source = "市场板块"
            if market == "科创板" or ts_code.startswith("688"):
                mapping = {"index": "000688.SH", "index_name": "科创50", "futures": None}
            elif market == "创业板" or ts_code.startswith("300") or ts_code.startswith("301"):
                mapping = {"index": "399006.SZ", "index_name": "创业板指", "futures": None}
            else:
                # 2.3 默认 fallback
                mapping = INDUSTRY_TO_INDEX["_default"]
                fallback_source = "默认兜底"

        index_code = mapping["index"]
        index_name = mapping["index_name"]
        futures_codes = mapping.get("futures")

        # 3. 获取指数数据
        index_data = get_index_daily(index_code, days=days)

        # 4. 获取个股数据用于相对强弱对比
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')

        df_stock = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)

        relative_strength = ""
        if not df_stock.empty and len(df_stock) >= 2:
            df_stock = df_stock.head(days)
            stock_latest = df_stock.iloc[0]['close']
            stock_oldest = df_stock.iloc[-1]['close']
            stock_return = (stock_latest - stock_oldest) / stock_oldest * 100

            # 获取指数同期涨幅
            df_index = pro.index_daily(ts_code=index_code, start_date=start_date, end_date=end_date)
            if not df_index.empty and len(df_index) >= 2:
                df_index = df_index.head(days)
                index_latest = df_index.iloc[0]['close']
                index_oldest = df_index.iloc[-1]['close']
                index_return = (index_latest - index_oldest) / index_oldest * 100

                relative = stock_return - index_return
                strength_text = "强势（跑赢板块）" if relative > 0 else "弱势（跑输板块）"

                relative_strength = f"""
## 相对强弱分析

| 指标 | 个股 | 板块 | 差值 |
|------|------|------|------|
| 区间涨幅 | {stock_return:+.2f}% | {index_return:+.2f}% | {relative:+.2f}% |
| 判断 | - | - | **{strength_text}** |
"""

        # 5. 周期行业提示
        cyclic_hint = ""
        if is_cyclic_industry(industry_name):
            futures_str = ", ".join(futures_codes) if futures_codes else "无"
            cyclic_hint = f"""
## 周期行业提示

该股属于**周期资源行业**，建议同时分析商品期货走势：
- 相关期货代码: {futures_str}
- 请调用 `get_tushare_fut_daily` 获取期货数据进行联动分析
"""

        # 6. 格式化输出
        result = f"""
# {stock_name}({ts_code}) 板块对比分析

- **所属行业**: {industry_name}
- **对标指数**: {index_name}({index_code})
- **匹配方式**: {fallback_source}
- **周期属性**: {"是（需要期货联动分析）" if is_cyclic_industry(industry_name) else "否"}

{index_data}
{relative_strength}
{cyclic_hint}
"""
        return result

    except Exception as e:
        logger.error(f"获取板块数据失败 [{stock_code}]: {e}")
        return f"[error] 获取板块数据失败: {str(e)}"


def get_index_member(index_code: str = "399318.SZ") -> str:
    """
    获取指数成分股

    Args:
        index_code: 指数代码，默认为有色金属指数 399318.SZ

    Returns:
        格式化字符串，包含成分股列表
    """
    try:
        pro = get_pro_api()

        index_name_map = {
            '399318.SZ': '国证有色',
            '000300.SH': '沪深300',
            '399006.SZ': '创业板指',
            '000016.SH': '上证50',
            '000905.SH': '中证500',
            '399001.SZ': '深证成指',
            '000001.SH': '上证指数',
            '399673.SZ': '创业板50',
            '000688.SH': '科创50',
        }
        index_name = index_name_map.get(index_code, index_code)

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')

        df = pd.DataFrame()

        # 方法1: 使用 index_member API（主流指数）
        try:
            df = pro.index_member(index_code=index_code)
        except:
            pass

        # 方法2: 如果为空，尝试使用 index_weight API（获取权重数据）
        if df.empty:
            try:
                df_weight = pro.index_weight(index_code=index_code, start_date=start_date, end_date=end_date)
                if not df_weight.empty:
                    # 获取最新日期的权重数据
                    latest_date = df_weight['trade_date'].max()
                    df_latest = df_weight[df_weight['trade_date'] == latest_date].copy()

                    result = []
                    result.append(f"# {index_name}({index_code}) 成分股权重\n")
                    result.append(f"## 最新成分股列表（{latest_date}，共{len(df_latest)}只）\n")
                    result.append("| 代码 | 权重(%) |")
                    result.append("|------|--------|")

                    df_latest = df_latest.sort_values('weight', ascending=False)
                    for _, row in df_latest.head(30).iterrows():
                        con_code = row.get('con_code', 'N/A')
                        weight = row.get('weight', 0)
                        result.append(f"| {con_code} | {weight:.2f} |")

                    if len(df_latest) > 30:
                        result.append(f"\n*注：仅显示权重前30只成分股，共{len(df_latest)}只*")

                    result.append("")
                    return "\n".join(result)
            except:
                pass

        # 方法3: 对于国证系列指数，尝试使用 ths_member（同花顺概念板块）
        if df.empty and index_code.startswith('399'):
            try:
                # 尝试获取同花顺行业成分
                df_ths = pro.ths_member(ts_code=index_code)
                if not df_ths.empty:
                    result = []
                    result.append(f"# {index_name}({index_code}) 成分股\n")
                    result.append(f"## 同花顺板块成分（共{len(df_ths)}只）\n")
                    result.append("| 代码 | 名称 |")
                    result.append("|------|------|")

                    for _, row in df_ths.head(30).iterrows():
                        code = row.get('code', 'N/A')
                        name = row.get('name', 'N/A')
                        result.append(f"| {code} | {name} |")

                    if len(df_ths) > 30:
                        result.append(f"\n*注：仅显示前30只成分股，共{len(df_ths)}只*")
                    result.append("")
                    return "\n".join(result)
            except:
                pass

        # 方法4: 对于特定行业指数，返回行业说明
        if df.empty:
            # 国证系列行业指数可能没有成分股API，返回说明信息
            industry_indices = {
                '399318.SZ': '有色金属',
                '399395.SZ': '国证银行',
                '399396.SZ': '国证食品',
                '399441.SZ': '国证生科',
            }
            if index_code in industry_indices:
                industry = industry_indices[index_code]
                return (f"# {index_name}({index_code})\n\n"
                        f"该指数为国证系列{industry}行业指数，TuShare暂未提供成分股明细数据。\n\n"
                        f"**建议**: 使用 get_index_daily API 获取指数行情走势，与个股进行联动分析。\n\n"
                        f"*提示: 可通过国证指数官网查询完整成分股列表*")

            return f"未找到指数 {index_code} 的成分股数据（该指数可能不在TuShare数据覆盖范围内，建议使用沪深300/上证50等主流指数）"

        # 过滤当前有效的成分股（out_date为空或大于今天）
        today = datetime.now().strftime('%Y%m%d')
        df_valid = df[(df['out_date'].isna()) | (df['out_date'] > today)]

        result = []
        result.append(f"# {index_name}({index_code}) 成分股\n")
        result.append(f"## 当前成分股列表（共{len(df_valid)}只）\n")
        result.append("| 代码 | 名称 | 纳入日期 |")
        result.append("|------|------|---------|")

        for _, row in df_valid.head(30).iterrows():  # 最多显示30只
            con_code = row.get('con_code', 'N/A')
            con_name = row.get('con_name', 'N/A')
            in_date = row.get('in_date', 'N/A')
            result.append(f"| {con_code} | {con_name} | {in_date} |")

        if len(df_valid) > 30:
            result.append(f"\n*注：仅显示前30只成分股，共{len(df_valid)}只*")

        result.append("")
        return "\n".join(result)

    except Exception as e:
        return f"获取指数成分股数据失败: {str(e)}"


def get_stk_surv(stock_code: str) -> str:
    """
    获取机构调研数据

    Args:
        stock_code: 股票代码

    Returns:
        格式化字符串，包含近期调研记录
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        # 获取最近6个月的调研数据
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')

        df = pro.stk_surv(ts_code=ts_code, start_date=start_date, end_date=end_date)

        if df.empty:
            return f"股票 {stock_code} 近6个月无机构调研记录"

        # 注意：stk_surv API 每行返回一家机构的调研记录
        # 字段：surv_date(调研日期), rece_org(接待机构), org_type(机构类型), rece_mode(接待方式)
        # 需要按日期分组统计

        result = []
        result.append("# 机构调研分析\n")
        result.append(f"## 近期机构调研记录（{stock_code}）\n")

        # 按日期分组统计
        date_stats = {}
        org_type_stats = {}

        for _, row in df.iterrows():
            surv_date = row.get('surv_date', 'N/A')
            org_type = row.get('org_type', '其他')
            rece_mode = row.get('rece_mode', 'N/A')
            rece_org = row.get('rece_org', 'N/A')

            # 按日期统计
            if surv_date not in date_stats:
                date_stats[surv_date] = {'count': 0, 'modes': set(), 'orgs': []}
            date_stats[surv_date]['count'] += 1
            if rece_mode and rece_mode != 'N/A':
                date_stats[surv_date]['modes'].add(rece_mode.split(',')[0])  # 取第一个模式
            date_stats[surv_date]['orgs'].append(rece_org)

            # 按机构类型统计
            if org_type:
                org_type_stats[org_type] = org_type_stats.get(org_type, 0) + 1

        # 输出按日期的调研汇总（最近10个日期）
        result.append("| 调研日期 | 机构数量 | 调研形式 | 参与机构（部分） |")
        result.append("|---------|---------|---------|----------------|")

        sorted_dates = sorted(date_stats.keys(), reverse=True)[:10]
        for date in sorted_dates:
            stats = date_stats[date]
            modes = '/'.join(list(stats['modes'])[:2]) if stats['modes'] else 'N/A'
            orgs_preview = ', '.join(stats['orgs'][:3])
            if len(stats['orgs']) > 3:
                orgs_preview += f" 等{len(stats['orgs'])}家"
            result.append(f"| {date} | {stats['count']} | {modes} | {orgs_preview} |")

        result.append("")

        # 机构类型分布
        result.append("### 机构类型分布")
        result.append("| 机构类型 | 参与次数 |")
        result.append("|---------|---------|")
        for org_type, count in sorted(org_type_stats.items(), key=lambda x: x[1], reverse=True):
            result.append(f"| {org_type} | {count} |")

        result.append("")
        total_records = len(df)
        unique_dates = len(date_stats)
        result.append(f"**调研统计**: 近6个月共{unique_dates}次调研活动，累计{total_records}家机构参与")

        # 分析
        if unique_dates >= 5:
            result.append("")
            result.append("**调研密度分析**: 调研频繁，机构关注度较高")
        elif unique_dates >= 2:
            result.append("")
            result.append("**调研密度分析**: 调研活动正常，机构保持关注")

        result.append("")
        return "\n".join(result)

    except Exception as e:
        return f"获取机构调研数据失败: {str(e)}"


def get_report_rc(stock_code: str, days: int = 30) -> str:
    """
    获取券商研报数据

    Args:
        stock_code: 股票代码
        days: 获取天数

    Returns:
        格式化字符串，包含近期研报标题、评级、目标价
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')

        df = pro.report_rc(ts_code=ts_code, start_date=start_date, end_date=end_date)

        if df.empty:
            return f"股票 {stock_code} 近期无券商研报"

        df = df.head(15)  # 最近15篇

        result = []
        result.append("# 券商研报分析\n")
        result.append(f"## 近期券商研报（{stock_code}）\n")
        result.append("| 日期 | 机构 | 评级 | 目标价 | 研报标题 |")
        result.append("|------|------|------|--------|---------|")

        rating_count = {'买入': 0, '增持': 0, '持有': 0, '减持': 0, '卖出': 0, '其他': 0}
        target_prices = []

        for _, row in df.iterrows():
            report_date = row.get('report_date', 'N/A')
            organ_name = row.get('organ_name', 'N/A')[:8] if row.get('organ_name') else 'N/A'
            rating = row.get('rating', 'N/A')
            target_price = row.get('target_price', None)
            title = row.get('report_title', 'N/A')[:25] if row.get('report_title') else 'N/A'

            # 统计评级
            if rating in rating_count:
                rating_count[rating] += 1
            else:
                rating_count['其他'] += 1

            # 收集目标价
            if target_price and pd.notna(target_price) and target_price > 0:
                target_prices.append(target_price)

            tp_str = f"{target_price:.2f}" if target_price and pd.notna(target_price) and target_price > 0 else "-"
            result.append(f"| {report_date} | {organ_name} | {rating} | {tp_str} | {title} |")

        result.append("")

        # 评级统计
        result.append("## 评级统计\n")
        result.append(f"- **买入/增持**: {rating_count['买入'] + rating_count['增持']}家")
        result.append(f"- **持有**: {rating_count['持有']}家")
        result.append(f"- **减持/卖出**: {rating_count['减持'] + rating_count['卖出']}家")

        # 目标价统计
        if target_prices:
            avg_target = sum(target_prices) / len(target_prices)
            max_target = max(target_prices)
            min_target = min(target_prices)
            result.append("")
            result.append("## 目标价统计\n")
            result.append(f"- **平均目标价**: {avg_target:.2f}元")
            result.append(f"- **最高目标价**: {max_target:.2f}元")
            result.append(f"- **最低目标价**: {min_target:.2f}元")

        result.append("")
        return "\n".join(result)

    except Exception as e:
        return f"获取券商研报数据失败: {str(e)}"


def get_fut_daily(fut_code: str, days: int = 60) -> str:
    """
    获取期货日线数据（铜/金主力合约）

    Args:
        fut_code: 期货代码（如 CU.SHF 沪铜, AU.SHF 沪金）
                  常用代码: CU=铜, AU=黄金, AG=白银, AL=铝
        days: 获取天数

    Returns:
        格式化字符串，包含期货价格走势
    """
    try:
        pro = get_pro_api()

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')

        # 获取主力合约映射
        # 首先尝试获取主力合约代码
        df_mapping = pro.fut_mapping(ts_code=fut_code)
        if not df_mapping.empty:
            # 使用主力合约
            main_contract = df_mapping.iloc[0]['mapping_ts_code']
        else:
            main_contract = fut_code

        df = pro.fut_daily(ts_code=main_contract, start_date=start_date, end_date=end_date)

        if df.empty:
            return f"未找到期货 {fut_code} 的行情数据"

        df = df.head(days)

        # 期货名称映射
        fut_name_map = {
            'CU': '沪铜',
            'AU': '沪金',
            'AG': '沪银',
            'AL': '沪铝',
            'ZN': '沪锌',
            'PB': '沪铅',
            'NI': '沪镍',
            'SN': '沪锡',
        }
        fut_prefix = fut_code.split('.')[0][:2] if '.' in fut_code else fut_code[:2]
        fut_name = fut_name_map.get(fut_prefix, fut_code)

        result = []
        result.append(f"# {fut_name} 期货行情分析\n")
        result.append(f"## 主力合约走势（{main_contract}）\n")
        result.append("| 日期 | 收盘价 | 结算价 | 涨跌幅(%) | 成交量(手) | 持仓量(手) |")
        result.append("|------|--------|--------|----------|-----------|-----------|")

        for _, row in df.head(20).iterrows():
            trade_date = row.get('trade_date', 'N/A')
            close = row.get('close', 0)
            settle = row.get('settle', 0)
            # 计算涨跌幅
            pre_settle = row.get('pre_settle', settle)
            pct_chg = (close - pre_settle) / pre_settle * 100 if pre_settle > 0 else 0
            vol = row.get('vol', 0)
            oi = row.get('oi', 0)

            result.append(f"| {trade_date} | {close:.0f} | {settle:.0f} | {pct_chg:+.2f} | {vol:.0f} | {oi:.0f} |")

        result.append("")

        # 统计分析
        latest_close = df.iloc[0]['close']
        oldest_close = df.iloc[-1]['close']
        period_return = (latest_close - oldest_close) / oldest_close * 100

        result.append(f"**区间涨跌幅**: {period_return:+.2f}%（近{len(df)}个交易日）")
        result.append(f"**最新收盘价**: {latest_close:.0f}")

        # 趋势判断
        if period_return > 5:
            trend = "期货价格上涨趋势明显，对相关股票形成利好"
        elif period_return < -5:
            trend = "期货价格下跌趋势，可能影响相关股票盈利预期"
        else:
            trend = "期货价格震荡，短期影响有限"

        result.append(f"**趋势判断**: {trend}")
        result.append("")

        return "\n".join(result)

    except Exception as e:
        return f"获取期货行情数据失败: {str(e)}"


# ============= 扩展综合数据获取函数 =============


def get_china_stock_capital_deep(stock_code: str) -> str:
    """
    获取深度资金分析数据（整合大宗交易、股权质押、解禁日历等）

    注：北向资金持股明细(hk_hold)已移除，港交所自2024年8月20日起仅提供季度数据。
    外资态度可通过前十大股东中"香港中央结算"持股比例变化来判断。

    Args:
        stock_code: 股票代码

    Returns:
        深度资金分析数据的格式化字符串
    """
    result = []

    # 大宗交易
    result.append(get_block_trade(stock_code))

    # 股权质押
    result.append(get_pledge_stat(stock_code))

    # 解禁日历
    result.append(get_share_float(stock_code))

    return "\n".join(result)


def get_china_stock_institution(stock_code: str) -> str:
    """
    获取机构观点数据（整合调研、研报）

    Args:
        stock_code: 股票代码

    Returns:
        机构观点数据的格式化字符串
    """
    result = []

    # 机构调研
    result.append(get_stk_surv(stock_code))

    # 券商研报
    result.append(get_report_rc(stock_code))

    return "\n".join(result)


# ==================== 新闻数据接口 ====================

def get_cctv_news(date: str = None) -> str:
    """
    获取新闻联播文字稿

    Args:
        date: 日期，格式 YYYYMMDD，默认今天

    Returns:
        新闻联播内容的格式化字符串
    """
    try:
        pro = get_pro_api()
    except ValueError as e:
        return f"[数据获取失败] {str(e)}"

    try:
        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        df = pro.cctv_news(date=date)

        if df is None or df.empty:
            return f"[无数据] {date} 无新闻联播数据"

        result = [f"# 新闻联播 ({date})\n"]

        # 筛选经济相关新闻
        economic_keywords = ['经济', '金融', '股市', '投资', '贸易', '产业', '制造', '科技', '改革', '发展', '企业']

        for idx, row in df.iterrows():
            title = row.get('title', '')
            content = row.get('content', '')

            # 检查是否与经济相关
            is_economic = any(kw in title or kw in str(content)[:200] for kw in economic_keywords)

            if is_economic:
                result.append(f"## {title}\n")
                if content:
                    # 截断过长内容
                    content_preview = content[:500] + '...' if len(str(content)) > 500 else content
                    result.append(f"{content_preview}\n")

        if len(result) == 1:
            result.append("今日无经济相关重点新闻")

        return "\n".join(result)

    except Exception as e:
        return f"[数据获取失败] 获取新闻联播数据失败: {str(e)}"


def get_major_news(start_date: str = None, end_date: str = None, src: str = None) -> str:
    """
    获取重大新闻（需要单独开通权限）

    Args:
        start_date: 开始日期时间，格式 'YYYY-MM-DD HH:MM:SS'
        end_date: 结束日期时间，格式 'YYYY-MM-DD HH:MM:SS'
        src: 新闻来源，如 '新浪财经', '同花顺'

    Returns:
        重大新闻的格式化字符串
    """
    try:
        pro = get_pro_api()
    except ValueError as e:
        return f"[数据获取失败] {str(e)}"

    try:
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if start_date is None:
            # 默认获取最近24小时的新闻
            start_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

        params = {
            'start_date': start_date,
            'end_date': end_date,
        }
        if src:
            params['src'] = src

        df = pro.major_news(**params)

        if df is None or df.empty:
            return "[无数据] 无重大新闻数据（可能需要开通权限）"

        result = ["# 重大财经新闻\n"]

        for idx, row in df.head(20).iterrows():
            title = row.get('title', '')
            pub_time = row.get('pub_time', '')
            source = row.get('src', '')
            content = row.get('content', '')

            result.append(f"**[{pub_time}] [{source}]** {title}")
            if content:
                content_preview = content[:300] + '...' if len(str(content)) > 300 else content
                result.append(f"  {content_preview}")
            result.append("")

        return "\n".join(result)

    except Exception as e:
        error_msg = str(e)
        if '权限' in error_msg or 'permission' in error_msg.lower():
            return "[权限不足] 重大新闻接口需要单独开通权限，请联系 Tushare"
        return f"[数据获取失败] 获取重大新闻失败: {error_msg}"


def get_china_market_news_tushare(date: str = None) -> str:
    """
    获取中国财经市场新闻（Tushare 版本）

    整合新闻联播和重大新闻数据

    Args:
        date: 日期，格式 YYYY-MM-DD 或 YYYYMMDD

    Returns:
        格式化的市场新闻字符串
    """
    result_parts = ["# 中国财经市场新闻 (Tushare)\n"]

    # 格式化日期
    if date:
        date_clean = date.replace("-", "")
    else:
        date_clean = datetime.now().strftime("%Y%m%d")

    # 1. 获取新闻联播
    cctv_result = get_cctv_news(date_clean)
    if "[数据获取失败]" not in cctv_result and "[无数据]" not in cctv_result:
        result_parts.append(cctv_result)
        result_parts.append("\n---\n")

    # 2. 尝试获取重大新闻（可能没有权限）
    major_result = get_major_news()
    if "[权限不足]" not in major_result and "[数据获取失败]" not in major_result:
        result_parts.append(major_result)
    else:
        result_parts.append("## 财经快讯\n")
        result_parts.append("重大新闻接口暂不可用，请参考新闻联播内容或使用其他新闻源。\n")

    return "\n".join(result_parts)


# ============================================================================
# 全市场行情数据（用于排行榜，替代慢速的 akshare API）
# ============================================================================

import threading

# 全市场数据缓存
_market_data_cache = None
_market_data_cache_time = None
_market_data_cache_lock = threading.Lock()
_MARKET_DATA_CACHE_TTL = 1800  # 30分钟缓存


def get_all_stocks_daily(trade_date: str = None) -> pd.DataFrame:
    """
    获取全市场日线行情数据（带缓存）

    使用 tushare 的 daily + daily_basic 接口，比 akshare 快约 50 倍。

    Args:
        trade_date: 交易日期 YYYYMMDD，默认最近交易日

    Returns:
        DataFrame 包含: 代码, 名称, 最新价, 涨跌幅, 成交额, 换手率, 市值等
    """
    global _market_data_cache, _market_data_cache_time

    with _market_data_cache_lock:
        now = datetime.now()

        # 检查缓存
        if _market_data_cache is not None and _market_data_cache_time is not None:
            age = (now - _market_data_cache_time).total_seconds()
            if age < _MARKET_DATA_CACHE_TTL:
                logger.debug(f"[tushare] 使用缓存的全市场数据 (age={age:.0f}s)")
                return _market_data_cache.copy()

        # 获取新数据
        logger.info("[tushare] 获取全市场行情数据...")
        start_time = datetime.now()

        try:
            pro = get_pro_api()

            # 确定交易日期
            if not trade_date:
                # 使用最近的交易日
                today = datetime.now().strftime("%Y%m%d")
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
                day_before = (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")
                dates_to_try = [today, yesterday, day_before]
            else:
                dates_to_try = [trade_date]

            df_daily = None
            df_basic = None
            used_date = None

            for date in dates_to_try:
                try:
                    df_daily = pro.daily(trade_date=date)
                    if df_daily is not None and not df_daily.empty:
                        df_basic = pro.daily_basic(trade_date=date)
                        used_date = date
                        break
                except Exception:
                    continue

            if df_daily is None or df_daily.empty:
                logger.warning("[tushare] 无法获取日线数据")
                return pd.DataFrame()

            # 获取股票名称
            df_names = pro.stock_basic(
                exchange='',
                list_status='L',
                fields='ts_code,name'
            )

            # 合并数据
            df = df_daily.merge(df_names, on='ts_code', how='left')

            if df_basic is not None and not df_basic.empty:
                # 避免列名冲突
                df_basic_cols = ['ts_code', 'turnover_rate', 'volume_ratio', 'pe_ttm', 'pb', 'total_mv', 'circ_mv']
                df_basic_subset = df_basic[[c for c in df_basic_cols if c in df_basic.columns]]
                df = df.merge(df_basic_subset, on='ts_code', how='left')

            # 重命名列为中文（与 akshare 兼容）
            column_map = {
                'ts_code': '代码',
                'name': '名称',
                'close': '最新价',
                'pct_chg': '涨跌幅',
                'change': '涨跌额',
                'vol': '成交量',
                'amount': '成交额',  # 千元 → 需要转换
                'open': '今开',
                'high': '最高',
                'low': '最低',
                'pre_close': '昨收',
                'turnover_rate': '换手率',
                'volume_ratio': '量比',
                'pe_ttm': '市盈率-动态',
                'pb': '市净率',
                'total_mv': '总市值',  # 万元
                'circ_mv': '流通市值',  # 万元
            }
            df = df.rename(columns={k: v for k, v in column_map.items() if k in df.columns})

            # 转换成交额单位：千元 → 元
            if '成交额' in df.columns:
                df['成交额'] = df['成交额'] * 1000

            # 转换市值单位：万元 → 元
            if '总市值' in df.columns:
                df['总市值'] = df['总市值'] * 10000
            if '流通市值' in df.columns:
                df['流通市值'] = df['流通市值'] * 10000

            # 清理代码格式（去掉 .SH/.SZ 后缀）
            if '代码' in df.columns:
                df['代码'] = df['代码'].str.replace(r'\.(SH|SZ|BJ)$', '', regex=True)

            elapsed = (datetime.now() - start_time).total_seconds()
            logger.info(f"[tushare] 全市场数据获取完成: {len(df)} 只股票, 耗时 {elapsed:.1f}s")

            # 更新缓存
            _market_data_cache = df
            _market_data_cache_time = now

            return df.copy()

        except Exception as e:
            logger.error(f"[tushare] 获取全市场数据失败: {e}")
            # 如果有旧缓存，返回旧数据
            if _market_data_cache is not None:
                logger.warning("[tushare] 使用过期缓存数据")
                return _market_data_cache.copy()
            return pd.DataFrame()


# ============================================================
# 新增数据接口（2024-01 扩展）
# ============================================================

def get_repurchase(stock_code: str) -> str:
    """
    获取股票回购数据

    回购是管理层认为股价被低估时的重要信号，对投资决策有重要参考价值。

    Args:
        stock_code: 股票代码

    Returns:
        回购数据的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        # 获取回购数据
        df = pro.repurchase(ts_code=ts_code)

        if df is None or df.empty:
            return f"未找到股票 {stock_code} 的回购数据（可能暂无回购计划）"

        result = []
        result.append("# 股权回购分析\n")
        result.append(f"## {stock_code} 回购记录\n")

        # 按公告日期排序，最新的在前
        df = df.sort_values('ann_date', ascending=False)

        for _, row in df.head(5).iterrows():  # 最近5条
            result.append(f"### 公告日期: {row.get('ann_date', 'N/A')}")
            result.append(f"- **回购进度**: {row.get('proc', 'N/A')}")

            # 回购金额
            exp_amount = row.get('exp_amount', 0)
            if pd.notna(exp_amount) and exp_amount > 0:
                result.append(f"- **计划回购金额**: {exp_amount/10000:.2f}亿元")

            amount = row.get('amount', 0)
            if pd.notna(amount) and amount > 0:
                result.append(f"- **已回购金额**: {amount/10000:.2f}亿元")

            # 回购股数
            vol = row.get('vol', 0)
            if pd.notna(vol) and vol > 0:
                result.append(f"- **已回购股数**: {vol/10000:.2f}万股")

            # 回购价格
            high_limit = row.get('high_limit', 0)
            if pd.notna(high_limit) and high_limit > 0:
                result.append(f"- **回购价格上限**: {high_limit:.2f}元")

            # 回购目的
            purpose = row.get('purpose', '')
            if purpose:
                result.append(f"- **回购目的**: {purpose}")

            result.append("")

        # 投资提示
        result.append("## 投资提示")
        result.append("- 回购通常表明管理层认为股价被低估")
        result.append("- 注意回购进度和完成率")
        result.append("- 关注回购目的（注销/股权激励/员工持股）")

        return "\n".join(result)

    except Exception as e:
        return f"获取回购数据失败: {str(e)}"


def get_fund_shares(stock_code: str, period: str = None) -> str:
    """
    获取基金持股数据

    查询公募基金持有某只股票的情况（季度数据）。

    Args:
        stock_code: 股票代码（如 600036.SH 或 600036）
        period: 报告期，如 "20240930"，默认获取最新一期

    Returns:
        基金持股数据的格式化字符串
    """
    try:
        pro = get_pro_api()

        # fund_portfolio 接口使用 symbol 参数（带后缀的完整代码如 600036.SH）
        ts_code = convert_stock_code(stock_code)
        symbol = ts_code  # 使用完整代码

        # 如果没有指定期，尝试获取最近可用的季度数据
        # 基金持仓数据一般滞后1-2个季度发布
        if not period:
            # 尝试几个最近的季度末，直到找到有数据的
            now = datetime.now()
            quarters_to_try = []
            for i in range(6):  # 尝试最近6个季度
                # 计算往前推i个季度的季末日期
                month = now.month - (now.month - 1) % 3  # 当前季度首月
                quarter_date = datetime(now.year, month, 1) - timedelta(days=1 + 90*i)
                # 找到该季度的末日
                qe_month = ((quarter_date.month - 1) // 3 + 1) * 3
                qe_day = 31 if qe_month == 12 else (30 if qe_month in [6, 9] else 31)
                if qe_month == 3:
                    qe_day = 31
                qe = datetime(quarter_date.year, qe_month, qe_day)
                quarters_to_try.append(qe.strftime('%Y%m%d'))

            # 使用最近一个可能有数据的季度（通常是2-3个季度前）
            period = quarters_to_try[2] if len(quarters_to_try) > 2 else quarters_to_try[0]

        # 获取基金持股数据
        df = pro.fund_portfolio(symbol=symbol, period=period)

        if df is None or df.empty:
            return f"未找到股票 {stock_code} 在 {period} 期的基金持股数据"

        result = []
        result.append("# 基金持股分析\n")

        # 获取报告期
        report_period = df['end_date'].iloc[0] if 'end_date' in df.columns else period
        result.append(f"## 截至 {report_period} 基金持股情况\n")

        # 按持股数量排序
        if 'amount' in df.columns:
            df = df.sort_values('amount', ascending=False)

        result.append("| 基金代码 | 持股数量(万股) | 市值占比(%) | 流通股占比(%) |")
        result.append("|---------|--------------|------------|--------------|")

        for _, row in df.head(15).iterrows():
            fund_code = row.get('ts_code', 'N/A')

            amount = row.get('amount', 0)
            amount_str = f"{amount/10000:.2f}" if pd.notna(amount) else 'N/A'

            mkv_ratio = row.get('stk_mkv_ratio', 0)
            mkv_str = f"{mkv_ratio:.2f}" if pd.notna(mkv_ratio) else 'N/A'

            float_ratio = row.get('stk_float_ratio', 0)
            float_str = f"{float_ratio:.2f}" if pd.notna(float_ratio) else 'N/A'

            result.append(f"| {fund_code} | {amount_str} | {mkv_str} | {float_str} |")

        result.append("")

        # 汇总统计
        total_funds = len(df)
        total_amount = df['amount'].sum() if 'amount' in df.columns else 0
        avg_float_ratio = df['stk_float_ratio'].mean() if 'stk_float_ratio' in df.columns else 0

        result.append(f"**持股基金数量**: {total_funds} 只")
        result.append(f"**基金合计持股**: {total_amount/10000:.2f} 万股")
        result.append(f"**平均流通股占比**: {avg_float_ratio:.4f}%")

        result.append("\n## 投资提示")
        if total_funds > 100:
            result.append("- 基金扎堆持有，机构关注度高")
        elif total_funds > 50:
            result.append("- 基金持股较多，机构认可度较好")
        else:
            result.append("- 基金持股数量一般，机构关注度中等")

        return "\n".join(result)

    except Exception as e:
        return f"获取基金持股数据失败: {str(e)}"


def get_adj_factor(stock_code: str, start_date: str = None, end_date: str = None) -> str:
    """
    获取复权因子数据

    复权因子用于计算除权除息后的真实价格涨跌幅。

    Args:
        stock_code: 股票代码
        start_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD

    Returns:
        复权因子数据的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        # 默认获取最近一年数据
        if not end_date:
            end_date = datetime.now().strftime('%Y%m%d')
        if not start_date:
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')

        # 获取复权因子
        df = pro.adj_factor(ts_code=ts_code, start_date=start_date, end_date=end_date)

        if df is None or df.empty:
            return f"未找到股票 {stock_code} 的复权因子数据"

        result = []
        result.append("# 复权因子分析\n")
        result.append(f"## {stock_code} 复权因子 ({start_date} ~ {end_date})\n")

        # 按日期排序
        df = df.sort_values('trade_date', ascending=False)

        # 获取最新和最早的复权因子
        latest = df.iloc[0]
        earliest = df.iloc[-1]

        latest_adj = latest['adj_factor']
        earliest_adj = earliest['adj_factor']

        result.append(f"**最新复权因子**: {latest_adj:.4f} ({latest['trade_date']})")
        result.append(f"**期初复权因子**: {earliest_adj:.4f} ({earliest['trade_date']})")

        # 计算期间复权调整幅度
        if earliest_adj > 0:
            adj_change = (latest_adj / earliest_adj - 1) * 100
            result.append(f"**期间复权调整**: {adj_change:+.2f}%")
        result.append("")

        # 检测除权除息事件（复权因子突变）
        df['adj_change'] = df['adj_factor'].diff(-1)  # 与前一天比较
        events = df[df['adj_change'].abs() > 0.001]  # 变动超过0.1%

        if not events.empty:
            result.append("## 除权除息事件\n")
            result.append("| 日期 | 复权因子 | 变动幅度 |")
            result.append("|------|---------|---------|")

            for _, row in events.head(10).iterrows():
                change_pct = (row['adj_change'] / row['adj_factor']) * 100 if row['adj_factor'] > 0 else 0
                result.append(f"| {row['trade_date']} | {row['adj_factor']:.4f} | {change_pct:+.2f}% |")

        result.append("")
        result.append("## 使用说明")
        result.append("- 前复权价格 = 原始价格 × 复权因子 / 最新复权因子")
        result.append("- 复权因子变动表示有分红、配股、送股等事件")

        return "\n".join(result)

    except Exception as e:
        return f"获取复权因子失败: {str(e)}"


def get_concept(stock_code: str) -> str:
    """
    获取股票所属概念板块

    了解股票所属的热点概念，判断板块联动效应。

    Args:
        stock_code: 股票代码

    Returns:
        概念板块数据的格式化字符串
    """
    try:
        pro = get_pro_api()
        ts_code = convert_stock_code(stock_code)

        # 获取概念板块成分
        df = pro.concept_detail(ts_code=ts_code)

        if df is None or df.empty:
            return f"未找到股票 {stock_code} 的概念板块数据"

        result = []
        result.append("# 概念板块分析\n")
        result.append(f"## {stock_code} 所属概念板块\n")

        result.append("| 概念名称 | 概念代码 | 板块说明 |")
        result.append("|---------|---------|---------|")

        for _, row in df.iterrows():
            concept_name = row.get('concept_name', 'N/A')
            concept_code = row.get('id', row.get('concept_code', 'N/A'))

            # 尝试获取概念说明
            desc = row.get('concept_desc', '')
            if not desc and 'in_date' in row:
                desc = f"纳入日期: {row['in_date']}"
            if len(desc) > 30:
                desc = desc[:30] + '...'

            result.append(f"| {concept_name} | {concept_code} | {desc} |")

        result.append("")
        result.append(f"**所属概念数量**: {len(df)} 个")
        result.append("")

        # 投资提示
        result.append("## 投资提示")
        result.append("- 关注热点概念板块的轮动机会")
        result.append("- 同一概念板块内的股票可能存在联动效应")
        result.append("- 概念炒作需注意风险，关注基本面支撑")

        return "\n".join(result)

    except Exception as e:
        return f"获取概念板块失败: {str(e)}"


# ============================================================
# 行业TAM（Total Addressable Market）数据工具
# ============================================================

# 行业常数词典 - 用于TAM估算的兜底策略
INDUSTRY_CONSTANTS = {
    # 医疗服务行业
    "医疗服务": {
        "growth_type": "高增长",
        "growth_range": "15-25%",
        "penetration": "低",
        "logic": "连锁扩张、床位增长、专科复制",
        "cr5_estimate": 0.15,
        "comps": ["爱尔眼科", "通策医疗", "海吉亚医疗"],
        "valuation_method": "PS估值+期权估值",
        "key_metrics": ["床位数增长", "单店收入", "净利率提升空间"],
    },
    "医药生物": {
        "growth_type": "中高增长",
        "growth_range": "10-20%",
        "penetration": "中",
        "logic": "创新药管线、集采影响、出海逻辑",
        "cr5_estimate": 0.20,
        "comps": ["恒瑞医药", "药明康德", "迈瑞医疗"],
        "valuation_method": "DCF+管线估值",
        "key_metrics": ["研发投入", "管线进度", "海外收入占比"],
    },
    # 银行金融
    "银行": {
        "growth_type": "低增长",
        "growth_range": "5-10%",
        "penetration": "高",
        "logic": "息差管理、资产质量、分红稳定",
        "cr5_estimate": 0.45,
        "comps": ["工商银行", "建设银行", "招商银行"],
        "valuation_method": "PB估值+股息率",
        "key_metrics": ["净息差", "不良率", "拨备覆盖率", "分红率"],
    },
    "保险": {
        "growth_type": "中等增长",
        "growth_range": "8-15%",
        "penetration": "中",
        "logic": "保费增长、投资收益、新业务价值",
        "cr5_estimate": 0.70,
        "comps": ["中国平安", "中国人寿", "中国太保"],
        "valuation_method": "内含价值(EV)估值",
        "key_metrics": ["新业务价值", "内含价值", "综合成本率"],
    },
    "券商": {
        "growth_type": "周期波动",
        "growth_range": "-20%~+50%",
        "penetration": "高",
        "logic": "成交量弹性、财富管理转型、投行业务",
        "cr5_estimate": 0.35,
        "comps": ["中信证券", "华泰证券", "东方财富"],
        "valuation_method": "PB估值",
        "key_metrics": ["日均成交额", "两融余额", "资管规模"],
    },
    # 周期资源
    "有色金属": {
        "growth_type": "周期波动",
        "growth_range": "-20%~+50%",
        "penetration": "N/A",
        "logic": "商品价格弹性、产能周期、库存周期",
        "cr5_estimate": 0.35,
        "comps": ["紫金矿业", "洛阳钼业", "江西铜业"],
        "valuation_method": "周期调整PE+资源储量估值",
        "key_metrics": ["铜/金/锂价格", "资源储量", "产能利用率"],
        "commodity_link": ["沪铜", "沪金", "碳酸锂"],
    },
    "煤炭": {
        "growth_type": "周期波动",
        "growth_range": "-30%~+80%",
        "penetration": "N/A",
        "logic": "煤价弹性、产能约束、高分红",
        "cr5_estimate": 0.30,
        "comps": ["中国神华", "陕西煤业", "兖矿能源"],
        "valuation_method": "股息率估值+周期调整PE",
        "key_metrics": ["动力煤价格", "产能利用率", "分红率"],
        "commodity_link": ["动力煤期货", "焦煤期货"],
    },
    "钢铁": {
        "growth_type": "周期波动",
        "growth_range": "-40%~+60%",
        "penetration": "N/A",
        "logic": "钢价弹性、产能置换、特钢溢价",
        "cr5_estimate": 0.25,
        "comps": ["宝钢股份", "华菱钢铁", "中信特钢"],
        "valuation_method": "PB估值+周期调整PE",
        "key_metrics": ["螺纹钢价格", "吨钢毛利", "产能利用率"],
        "commodity_link": ["螺纹钢期货", "热卷期货"],
    },
    "化工": {
        "growth_type": "周期波动",
        "growth_range": "-25%~+40%",
        "penetration": "N/A",
        "logic": "产品价差、产能周期、一体化优势",
        "cr5_estimate": 0.20,
        "comps": ["万华化学", "恒力石化", "荣盛石化"],
        "valuation_method": "周期调整PE",
        "key_metrics": ["主要产品价差", "产能利用率", "成本优势"],
        "commodity_link": ["原油期货", "PTA期货"],
    },
    # 消费
    "白酒": {
        "growth_type": "中高增长",
        "growth_range": "10-20%",
        "penetration": "中",
        "logic": "价格带升级、渠道扩张、品牌溢价",
        "cr5_estimate": 0.55,
        "comps": ["贵州茅台", "五粮液", "泸州老窖"],
        "valuation_method": "PE估值",
        "key_metrics": ["批价", "库存周期", "经销商数量"],
    },
    "食品饮料": {
        "growth_type": "中等增长",
        "growth_range": "8-15%",
        "penetration": "高",
        "logic": "消费升级、渠道下沉、品类扩张",
        "cr5_estimate": 0.35,
        "comps": ["伊利股份", "海天味业", "农夫山泉"],
        "valuation_method": "PE估值",
        "key_metrics": ["营收增速", "毛利率", "渠道覆盖"],
    },
    "家电": {
        "growth_type": "低增长",
        "growth_range": "3-8%",
        "penetration": "高",
        "logic": "存量换新、高端化、出海",
        "cr5_estimate": 0.60,
        "comps": ["美的集团", "格力电器", "海尔智家"],
        "valuation_method": "PE估值+股息率",
        "key_metrics": ["内销/外销增速", "高端占比", "分红率"],
    },
    # 科技成长
    "新能源": {
        "growth_type": "高增长",
        "growth_range": "20-40%",
        "penetration": "中",
        "logic": "渗透率提升、技术迭代、产能扩张",
        "cr5_estimate": 0.40,
        "comps": ["宁德时代", "比亚迪", "隆基绿能"],
        "valuation_method": "PE+产能估值",
        "key_metrics": ["装机量", "渗透率", "产能利用率"],
    },
    "半导体": {
        "growth_type": "高增长",
        "growth_range": "15-30%",
        "penetration": "低",
        "logic": "国产替代、周期复苏、技术突破",
        "cr5_estimate": 0.25,
        "comps": ["中芯国际", "韦尔股份", "北方华创"],
        "valuation_method": "PS估值+周期调整PE",
        "key_metrics": ["晶圆代工价格", "设备订单", "国产化率"],
    },
    "互联网": {
        "growth_type": "中高增长",
        "growth_range": "10-25%",
        "penetration": "高",
        "logic": "用户变现、AI赋能、出海增量",
        "cr5_estimate": 0.70,
        "comps": ["腾讯控股", "阿里巴巴", "美团"],
        "valuation_method": "SOTP+PE估值",
        "key_metrics": ["MAU", "ARPU", "变现率"],
    },
    # 公用事业
    "电力": {
        "growth_type": "低增长",
        "growth_range": "3-8%",
        "penetration": "高",
        "logic": "电价市场化、绿电溢价、稳定分红",
        "cr5_estimate": 0.35,
        "comps": ["长江电力", "华能国际", "国电电力"],
        "valuation_method": "股息率估值",
        "key_metrics": ["上网电价", "利用小时数", "分红率"],
    },
    "燃气": {
        "growth_type": "中等增长",
        "growth_range": "8-15%",
        "penetration": "中",
        "logic": "气量增长、顺价机制、接驳费",
        "cr5_estimate": 0.25,
        "comps": ["新奥股份", "昆仑能源", "华润燃气"],
        "valuation_method": "PE估值",
        "key_metrics": ["售气量增速", "价差", "接驳户数"],
    },
    # 地产建筑
    "房地产": {
        "growth_type": "低增长/负增长",
        "growth_range": "-10%~+5%",
        "penetration": "高",
        "logic": "集中度提升、土储价值、政策边际改善",
        "cr5_estimate": 0.25,
        "comps": ["保利发展", "万科A", "招商蛇口"],
        "valuation_method": "NAV估值",
        "key_metrics": ["销售额", "土储货值", "融资成本"],
    },
    "建筑": {
        "growth_type": "低增长",
        "growth_range": "0-8%",
        "penetration": "高",
        "logic": "订单增长、现金流改善、一带一路",
        "cr5_estimate": 0.40,
        "comps": ["中国建筑", "中国中铁", "中国交建"],
        "valuation_method": "PE估值+订单估值",
        "key_metrics": ["新签订单", "营收确认进度", "经营现金流"],
    },
}

# 申万行业代码映射（用于获取行业成分股）
SHENWAN_INDUSTRY_CODES = {
    "银行": "801780.SI",
    "非银金融": "801790.SI",
    "房地产": "801180.SI",
    "建筑装饰": "801720.SI",
    "建筑材料": "801710.SI",
    "钢铁": "801040.SI",
    "有色金属": "801050.SI",
    "煤炭": "801020.SI",
    "石油石化": "801960.SI",
    "化工": "801030.SI",
    "电力设备": "801730.SI",
    "机械设备": "801890.SI",
    "国防军工": "801740.SI",
    "汽车": "801880.SI",
    "家用电器": "801110.SI",
    "食品饮料": "801120.SI",
    "纺织服饰": "801130.SI",
    "轻工制造": "801140.SI",
    "医药生物": "801150.SI",
    "公用事业": "801160.SI",
    "交通运输": "801170.SI",
    "商贸零售": "801200.SI",
    "社会服务": "801210.SI",
    "传媒": "801760.SI",
    "通信": "801770.SI",
    "计算机": "801750.SI",
    "电子": "801080.SI",
    "农林牧渔": "801010.SI",
    "综合": "801230.SI",
    "美容护理": "801980.SI",
    "环保": "801970.SI",
}

# ═══════════════════════════════════════════════════════════════
# 行业 → 指数/期货 映射表（用于板块对比和商品联动分析）
# ═══════════════════════════════════════════════════════════════
INDUSTRY_TO_INDEX = {
    # 周期资源行业（需要期货联动）- 申万一级
    "有色金属": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["CU.SHF", "AL.SHF", "AU.SHF", "AG.SHF"]},
    "煤炭": {"index": "399998.SZ", "index_name": "中证煤炭", "futures": ["ZC.ZCE", "JM.DCE"]},
    "钢铁": {"index": "399994.SZ", "index_name": "中证有色", "futures": ["RB.SHF", "HC.SHF"]},
    "化工": {"index": "399993.SZ", "index_name": "中证化工", "futures": ["MA.ZCE", "TA.ZCE", "PTA.ZCE"]},
    "基础化工": {"index": "399993.SZ", "index_name": "中证化工", "futures": ["MA.ZCE", "TA.ZCE"]},
    "石油石化": {"index": "399975.SZ", "index_name": "证券龙头", "futures": ["SC.INE", "FU.SHF"]},

    # 有色金属细分行业（Tushare返回的可能是细分行业名）
    "铜": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["CU.SHF"]},
    "金": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["AU.SHF"]},
    "黄金": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["AU.SHF"]},
    "银": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["AG.SHF"]},
    "白银": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["AG.SHF"]},
    "铝": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["AL.SHF"]},
    "锌": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["ZN.SHF"]},
    "铅": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["PB.SHF"]},
    "镍": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["NI.SHF"]},
    "锡": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["SN.SHF"]},
    "稀土": {"index": "399318.SZ", "index_name": "国证有色", "futures": None},
    "锂": {"index": "399318.SZ", "index_name": "国证有色", "futures": ["LC.GFE"]},
    "钴": {"index": "399318.SZ", "index_name": "国证有色", "futures": None},
    "钨": {"index": "399318.SZ", "index_name": "国证有色", "futures": None},
    "钼": {"index": "399318.SZ", "index_name": "国证有色", "futures": None},

    # 煤炭细分行业
    "煤炭开采": {"index": "399998.SZ", "index_name": "中证煤炭", "futures": ["ZC.ZCE", "JM.DCE"]},
    "焦炭": {"index": "399998.SZ", "index_name": "中证煤炭", "futures": ["J.DCE", "JM.DCE"]},

    # 钢铁细分行业
    "普钢": {"index": "399994.SZ", "index_name": "中证有色", "futures": ["RB.SHF", "HC.SHF"]},
    "特钢": {"index": "399994.SZ", "index_name": "中证有色", "futures": ["RB.SHF"]},

    # 金融行业
    "银行": {"index": "399986.SZ", "index_name": "中证银行", "futures": None},
    "非银金融": {"index": "399975.SZ", "index_name": "中证证券", "futures": None},
    "证券": {"index": "399975.SZ", "index_name": "中证证券", "futures": None},      # 东方财富、中信证券等
    "保险": {"index": "399986.SZ", "index_name": "中证银行", "futures": None},       # 保险与银行同属大金融
    "多元金融": {"index": "399975.SZ", "index_name": "中证证券", "futures": None},   # 信托、期货等

    # 成长行业
    "电子": {"index": "399678.SZ", "index_name": "深证电子", "futures": None},
    "计算机": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},
    "通信": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},
    "传媒": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},
    "医药生物": {"index": "399989.SZ", "index_name": "中证医药", "futures": None},

    # 半导体及集成电路（科技硬件）- 中芯国际等
    "半导体": {"index": "399976.SZ", "index_name": "中证半导", "futures": None},
    "集成电路": {"index": "399976.SZ", "index_name": "中证半导", "futures": None},
    "芯片": {"index": "399976.SZ", "index_name": "中证半导", "futures": None},
    "半导体材料": {"index": "399976.SZ", "index_name": "中证半导", "futures": None},
    "半导体设备": {"index": "399976.SZ", "index_name": "中证半导", "futures": None},

    # 电子元器件（消费电子）
    "元器件": {"index": "399978.SZ", "index_name": "中证元器件", "futures": None},
    "电子元器件": {"index": "399978.SZ", "index_name": "中证元器件", "futures": None},
    "PCB": {"index": "399978.SZ", "index_name": "中证元器件", "futures": None},
    "被动元件": {"index": "399978.SZ", "index_name": "中证元器件", "futures": None},

    # 通信设备细分
    "通信设备": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},
    "通信服务": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},
    "光通信": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},

    # 软件与IT服务
    "软件服务": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},
    "软件开发": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},
    "IT服务": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},
    "互联网服务": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},
    "云计算": {"index": "399996.SZ", "index_name": "中证信息", "futures": None},

    # 光学光电
    "光学光电子": {"index": "399678.SZ", "index_name": "深证电子", "futures": None},
    "消费电子": {"index": "399678.SZ", "index_name": "深证电子", "futures": None},
    "电力设备": {"index": "399808.SZ", "index_name": "中证新能", "futures": None},
    # 电力设备细分行业（Tushare返回的可能是不同名称）
    "电气设备": {"index": "399808.SZ", "index_name": "中证新能", "futures": None},  # 宁德时代等
    "电器仪表": {"index": "399808.SZ", "index_name": "中证新能", "futures": None},  # 兼容旧版分类
    "电源设备": {"index": "399808.SZ", "index_name": "中证新能", "futures": None},  # 细分
    "新能源": {"index": "399808.SZ", "index_name": "中证新能", "futures": None},    # 新能源整体
    "光伏设备": {"index": "399808.SZ", "index_name": "中证新能", "futures": None},  # 光伏细分
    "风电设备": {"index": "399808.SZ", "index_name": "中证新能", "futures": None},  # 风电细分
    "储能设备": {"index": "399808.SZ", "index_name": "中证新能", "futures": None},  # 储能细分
    "电池": {"index": "399808.SZ", "index_name": "中证新能", "futures": None},      # 电池细分

    # 消费行业
    "食品饮料": {"index": "399987.SZ", "index_name": "中证酒", "futures": None},
    "家用电器": {"index": "399987.SZ", "index_name": "中证酒", "futures": None},
    "汽车": {"index": "399971.SZ", "index_name": "中证汽车", "futures": None},
    "商贸零售": {"index": "399971.SZ", "index_name": "中证汽车", "futures": None},
    "社会服务": {"index": "399971.SZ", "index_name": "中证汽车", "futures": None},
    "纺织服饰": {"index": "399971.SZ", "index_name": "中证汽车", "futures": None},
    "美容护理": {"index": "399971.SZ", "index_name": "中证汽车", "futures": None},

    # 其他行业
    "房地产": {"index": "399393.SZ", "index_name": "国证地产", "futures": None},
    "建筑装饰": {"index": "399393.SZ", "index_name": "国证地产", "futures": None},
    "建筑材料": {"index": "399393.SZ", "index_name": "国证地产", "futures": None},
    "交通运输": {"index": "399106.SZ", "index_name": "深证综指", "futures": None},
    "公用事业": {"index": "399106.SZ", "index_name": "深证综指", "futures": None},
    "机械设备": {"index": "399106.SZ", "index_name": "深证综指", "futures": None},
    "国防军工": {"index": "399106.SZ", "index_name": "深证综指", "futures": None},
    "轻工制造": {"index": "399106.SZ", "index_name": "深证综指", "futures": None},
    "农林牧渔": {"index": "399106.SZ", "index_name": "深证综指", "futures": None},
    "环保": {"index": "399106.SZ", "index_name": "深证综指", "futures": None},
    "综合": {"index": "399106.SZ", "index_name": "深证综指", "futures": None},

    # 默认值
    "_default": {"index": "000300.SH", "index_name": "沪深300", "futures": None},
}

# 周期行业集合（用于判断是否需要期货联动分析）
# 包含申万一级行业和细分行业
CYCLIC_INDUSTRIES = {
    # 申万一级
    "有色金属", "煤炭", "钢铁", "化工", "基础化工", "石油石化",
    # 有色细分
    "铜", "金", "黄金", "银", "白银", "铝", "锌", "铅", "镍", "锡", "稀土", "锂", "钴", "钨", "钼",
    # 煤炭细分
    "煤炭开采", "焦炭",
    # 钢铁细分
    "普钢", "特钢",
}


def get_industry_index_code(industry: str) -> str:
    """根据行业名称获取对应的指数代码"""
    mapping = INDUSTRY_TO_INDEX.get(industry, INDUSTRY_TO_INDEX["_default"])
    return mapping["index"]


def get_industry_futures_codes(industry: str) -> list:
    """根据行业名称获取对应的期货代码列表"""
    mapping = INDUSTRY_TO_INDEX.get(industry, INDUSTRY_TO_INDEX["_default"])
    return mapping.get("futures") or []


def is_cyclic_industry(industry: str) -> bool:
    """判断是否为周期行业（需要期货联动分析）"""
    return industry in CYCLIC_INDUSTRIES


def get_industry_index_name(industry: str) -> str:
    """根据行业名称获取对应的指数名称"""
    mapping = INDUSTRY_TO_INDEX.get(industry, INDUSTRY_TO_INDEX["_default"])
    return mapping["index_name"]


def get_industry_tam_data(industry: str, stock_code: str = None) -> str:
    """
    获取行业TAM（Total Addressable Market）和市场格局数据

    采用三级降级策略：
    - Level 1: 精确TAM数据（如有行业研报数据）
    - Level 2: Top5营收估算 + 行业特征（使用Tushare数据）
    - Level 3: 行业常数词典描述（兜底方案）

    Args:
        industry: 行业名称（如"医疗服务"、"银行"、"有色金属"等）
        stock_code: 可选，股票代码，用于确定具体行业归属

    Returns:
        行业TAM估算、增长特征、竞争格局的格式化字符串
    """
    try:
        pro = get_pro_api()
        result = []
        result.append(f"# 行业TAM与市场格局分析\n")
        result.append(f"**目标行业**: {industry}\n")

        # 尝试匹配行业常数
        industry_info = None
        matched_industry = None

        # 精确匹配
        if industry in INDUSTRY_CONSTANTS:
            industry_info = INDUSTRY_CONSTANTS[industry]
            matched_industry = industry
        else:
            # 模糊匹配
            for key in INDUSTRY_CONSTANTS:
                if key in industry or industry in key:
                    industry_info = INDUSTRY_CONSTANTS[key]
                    matched_industry = key
                    break

        # Level 2: 尝试获取Top5数据进行TAM估算
        level2_success = False
        if matched_industry and matched_industry in SHENWAN_INDUSTRY_CODES:
            try:
                index_code = SHENWAN_INDUSTRY_CODES[matched_industry]

                # 获取行业成分股
                df_members = pro.index_member(index_code=index_code)
                if df_members is not None and not df_members.empty:
                    # 获取成分股的市值和营收数据
                    member_codes = df_members['con_code'].tolist()[:20]  # 取前20只计算

                    # 获取最新财务数据
                    total_revenue = 0
                    total_market_cap = 0
                    company_data = []

                    for code in member_codes[:10]:  # 取Top10
                        try:
                            # 获取市值数据
                            df_basic = pro.daily_basic(
                                ts_code=code,
                                fields='ts_code,total_mv,pe_ttm,pb'
                            )
                            if df_basic is not None and not df_basic.empty:
                                mv = df_basic.iloc[0].get('total_mv', 0)
                                if mv and mv > 0:
                                    total_market_cap += mv

                            # 获取最新年报营收
                            df_income = pro.income(
                                ts_code=code,
                                fields='ts_code,end_date,revenue,n_income'
                            )
                            if df_income is not None and not df_income.empty:
                                # 取最新一期
                                df_income = df_income.sort_values('end_date', ascending=False)
                                revenue = df_income.iloc[0].get('revenue', 0)
                                if revenue and revenue > 0:
                                    total_revenue += revenue
                                    company_data.append({
                                        'code': code,
                                        'revenue': revenue / 1e8,  # 转换为亿元
                                        'market_cap': mv / 1e4 if mv else 0  # 转换为亿元
                                    })
                        except Exception:
                            continue

                    if total_revenue > 0 and industry_info:
                        cr5 = industry_info.get('cr5_estimate', 0.3)
                        # 估算行业TAM
                        top10_revenue = total_revenue / 1e8  # 亿元
                        estimated_tam = top10_revenue / cr5 if cr5 > 0 else top10_revenue * 3

                        result.append("## Level 2: Top企业估算\n")
                        result.append(f"**数据来源**: Tushare行业成分股财务数据\n")
                        result.append(f"**采样范围**: {matched_industry}行业Top10上市公司\n")
                        result.append(f"**Top10合计营收**: {top10_revenue:.1f} 亿元\n")
                        result.append(f"**行业集中度假设(CR5)**: {cr5*100:.0f}%\n")
                        result.append(f"**估算行业TAM**: {estimated_tam:.0f} 亿元\n")
                        result.append(f"**Top10合计市值**: {total_market_cap/1e4:.0f} 亿元\n")
                        result.append("")

                        # Top5详情
                        if company_data:
                            company_data.sort(key=lambda x: x['revenue'], reverse=True)
                            result.append("### Top5企业营收")
                            result.append("| 排名 | 代码 | 营收(亿) | 市值(亿) |")
                            result.append("|-----|------|---------|---------|")
                            for i, c in enumerate(company_data[:5]):
                                result.append(f"| {i+1} | {c['code']} | {c['revenue']:.1f} | {c['market_cap']:.0f} |")
                            result.append("")

                        level2_success = True

            except Exception as e:
                result.append(f"*Level 2数据获取异常: {str(e)[:50]}*\n")

        # Level 3: 行业常数词典（兜底或补充）
        if industry_info:
            result.append("## 行业特征画像\n")
            result.append(f"**增长类型**: {industry_info.get('growth_type', 'N/A')}\n")
            result.append(f"**增速区间**: {industry_info.get('growth_range', 'N/A')}\n")
            result.append(f"**渗透率水平**: {industry_info.get('penetration', 'N/A')}\n")
            result.append(f"**核心逻辑**: {industry_info.get('logic', 'N/A')}\n")
            result.append(f"**推荐估值方法**: {industry_info.get('valuation_method', 'N/A')}\n")
            result.append("")

            # 可比公司
            comps = industry_info.get('comps', [])
            if comps:
                result.append(f"**行业龙头**: {', '.join(comps)}\n")

            # 关键指标
            key_metrics = industry_info.get('key_metrics', [])
            if key_metrics:
                result.append(f"**关键跟踪指标**: {', '.join(key_metrics)}\n")

            # 商品联动（周期股）
            commodity_link = industry_info.get('commodity_link', [])
            if commodity_link:
                result.append(f"**商品价格联动**: {', '.join(commodity_link)}\n")

            result.append("")

            # 多头策略提示
            result.append("## 多头策略适用性\n")
            growth_type = industry_info.get('growth_type', '')
            if '高增长' in growth_type:
                result.append("**适用策略**: 成长股终局思维\n")
                result.append("- TAM倒推法：市场规模 × 份额假设 = 未来营收\n")
                result.append("- PS对标法：对比可比公司扩张期PS\n")
                result.append("- 期权估值：基础业务 + 技术/产能期权\n")
            elif '周期' in growth_type:
                result.append("**适用策略**: 周期股逆向布局\n")
                result.append("- 周期悖论：高PE=盈利底部=买入信号\n")
                result.append("- 产能出清：竞争对手退出=龙头红利\n")
                result.append("- 商品弹性：价格回升=利润高弹性\n")
            else:
                result.append("**适用策略**: 价值股时间复利\n")
                result.append("- 股息复利：股息再投资的长期增值\n")
                result.append("- 均值回归：历史分位的均值回归机会\n")
                result.append("- 资产重估：PB破净时的隐藏价值\n")

        else:
            # 未匹配到行业常数
            result.append("## 行业数据状态\n")
            result.append(f"**注意**: 未在预设行业库中找到「{industry}」的精确匹配\n")
            result.append("建议使用基本面报告中的行业判断，或指定更具体的行业名称\n")
            result.append("")
            result.append("**可用行业**: " + ", ".join(list(INDUSTRY_CONSTANTS.keys())[:10]) + "...\n")

        # 数据时效性提示
        result.append("\n---")
        result.append("*数据说明: TAM估算基于上市公司公开财务数据，仅供参考*")

        return "\n".join(result)

    except Exception as e:
        return f"获取行业TAM数据失败: {str(e)}"
