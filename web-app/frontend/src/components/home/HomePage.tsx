/**
 * 智能选股 Agent 控制台
 */
import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { analysisApi } from '../../api/client';
import type { HistoryItem } from '../../api/client';
import './HomePage.css';

export const HomePage: React.FC = () => {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [history, setHistory] = useState<HistoryItem[]>([]);

  useEffect(() => {
    analysisApi.getHistory(5).then(setHistory).catch(() => {});
  }, []);

  const getDecisionColor = (decision?: string) => {
    if (!decision) return '#666';
    const d = decision.toLowerCase();
    if (d.includes('买') || d.includes('buy')) return '#22c55e';
    if (d.includes('卖') || d.includes('sell')) return '#ef4444';
    return '#f59e0b';
  };

  return (
    <div className="home-page">
      <header className="home-header">
        <div className="home-header-inner">
          <div className="home-header-text">
            <h1>智能选股 Agent 控制台</h1>
            <p className="welcome-text">欢迎回来，{user?.name}</p>
          </div>
          <div className="header-actions">
            <button className="logout-btn" onClick={logout}>
              退出
            </button>
          </div>
        </div>
      </header>

      <main className="home-content">
        <div className="mode-card analysis-mode" onClick={() => navigate('/analysis')}>
          <div className="mode-icon">析</div>
          <div className="mode-info">
            <h2>多 Agent 选股分析</h2>
            <p className="mode-desc">分析师、研究员、风控节点协作</p>
            <ul className="mode-features">
              <li>行情与技术面分析</li>
              <li>新闻与情绪辅助判断</li>
              <li>基本面信息整理</li>
              <li>输出候选观察报告</li>
            </ul>
            <p className="mode-time">课堂稳定模式约 5-10 分钟</p>
          </div>
          <button className="mode-btn primary">开始分析</button>
        </div>

        <div className="mode-card chat-mode" onClick={() => navigate('/chat')}>
          <div className="mode-icon">问</div>
          <div className="mode-info">
            <h2>Agent 问答</h2>
            <p className="mode-desc">用自然语言了解股票和系统结果</p>
            <ul className="mode-features">
              <li>查询行情和估值线索</li>
              <li>解释分析报告</li>
              <li>支持多轮追问</li>
            </ul>
          </div>
          <button className="mode-btn">进入问答</button>
        </div>

        <div className="mode-card radar-mode" onClick={() => navigate('/market-radar')}>
          <div className="mode-icon">热</div>
          <div className="mode-info">
            <h2>市场热点雷达</h2>
            <p className="mode-desc">辅助观察舆情与市场关注点</p>
            <ul className="mode-features">
              <li>多平台热点聚合</li>
              <li>关键词筛选</li>
              <li>热点趋势辅助分析</li>
            </ul>
          </div>
          <button className="mode-btn">查看热点</button>
        </div>
      </main>

      <section className="history-section">
        <div className="history-header">
          <h3>历史报告 ({history.length}份)</h3>
          <button className="view-all-btn" onClick={() => navigate('/history')}>
            查看全部 →
          </button>
        </div>
        {history.length > 0 ? (
          <ul className="history-list">
            {history.map((item) => (
              <li
                key={item.task_id}
                className="history-item"
                onClick={() => {
                  if (item.status === 'completed') {
                    navigate(`/analysis/result/${item.task_id}`);
                  } else if (item.status === 'running' || item.status === 'pending') {
                    navigate(`/analysis/${item.task_id}`);
                  }
                }}
              >
                <span className="history-ticker">
                  {item.ticker.split('.')[0]}
                </span>
                <span className="history-name">{item.ticker_name}</span>
                <span
                  className="history-decision"
                  style={{ color: getDecisionColor(item.decision) }}
                >
                  {item.decision || item.status}
                </span>
                <span className="history-date">
                  {item.date.substring(5)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="history-empty">
            <span>暂无本次会话的分析记录</span>
          </div>
        )}
      </section>
    </div>
  );
};
