# Stock Agent MCP Server

This directory contains an optional MCP server wrapper for local stock analysis tools.
It exposes a small set of commands so MCP-compatible clients can request stock reports
from the same backend analysis pipeline used by the web application.

## Tools

- `analyze_stock`: run a complete stock analysis.
- `resolve_ticker`: convert common stock names to symbols.
- `check_analysis_env`: verify that required runtime settings are available.
- `read_stock_report`: read a generated report.
- `list_analysis_history`: list existing analysis results.

## Install

```bash
cd /path/to/stock-agent/mcp-server
pip install mcp
```

Set only the keys you actually use. Do not commit API keys into the repository.

```bash
export DEEPSEEK_API_KEY="your_deepseek_key"
export OPENAI_API_KEY="your_openai_compatible_key"
export TUSHARE_TOKEN="optional_tushare_token"
```

## Client Config Example

```json
{
  "mcpServers": {
    "stock-agent": {
      "command": "python",
      "args": ["/path/to/stock-agent/mcp-server/server.py"],
      "env": {
        "DEEPSEEK_API_KEY": "your_deepseek_key"
      }
    }
  }
}
```

## Report Output

Generated reports are written under:

```text
results/{symbol}/{date}/reports/
```

Typical files include:

- `market_report.md`
- `sentiment_report.md`
- `news_report.md`
- `fundamentals_report.md`
- `trader_investment_plan.md`
- `final_trade_decision.md`
- `consolidation_report.md`

## Runtime Flow

```text
MCP client
  -> mcp-server/server.py
  -> multi-agent analysis graph
  -> analyst reports, debate results, risk review
  -> final investment decision
```
