/**
 * 智能选股 Agent 登录页
 */
import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import './LoginPage.css';

export const LoginPage: React.FC = () => {
  const [accessCode, setAccessCode] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  const { login } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setIsLoading(true);

    try {
      const response = await login(accessCode);
      if (response.success) {
        navigate('/guide', { replace: true });
      } else {
        setError(response.message || '访问码错误');
      }
    } catch {
      setError('网络错误，请重试');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="dev-portal">
      <header className="portal-header">
        <div className="logo">Stock Agent Lab</div>
        <div className="header-badge">课堂演示版</div>
      </header>

      <section className="hero">
        <h1>智能选股 Agent</h1>
        <p>A 股多 Agent 分析演示系统</p>
      </section>

      <div className="tools-grid stock-agent-login-grid">
        <div className="tool-card expanded primary-tool">
          <div className="tool-header">
            <span className="tool-icon stock-agent-icon">SA</span>
            <h2>课堂展示入口</h2>
            <span className="beta-tag">Demo</span>
          </div>
          <p className="tool-desc">
            输入访问码后进入智能选股 Agent，体验前端页面、后端服务、LangGraph 多 Agent 编排和 DeepSeek 分析报告的完整流程。
          </p>
          <div className="tool-tags">
            <span>LangGraph</span>
            <span>DeepSeek</span>
            <span>A 股分析</span>
            <span>课堂演示</span>
          </div>

          <form className="login-form" onSubmit={handleSubmit}>
            <div className="input-wrapper">
              <span className="input-icon">码</span>
              <input
                type="password"
                value={accessCode}
                onChange={(e) => setAccessCode(e.target.value)}
                placeholder="输入访问码"
                disabled={isLoading}
                autoFocus
              />
            </div>
            {error && <div className="error-msg">{error}</div>}
            <button type="submit" disabled={isLoading || !accessCode}>
              {isLoading ? '验证中...' : '进入系统'}
            </button>
          </form>
        </div>
      </div>

      <footer className="portal-footer">
        <p>教学演示项目 · 不做实盘交易 · 不构成投资建议</p>
      </footer>
    </div>
  );
};
