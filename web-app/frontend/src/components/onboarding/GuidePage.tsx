/**
 * 引导页面 - 展示多智能体团队架构
 */
import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import './GuidePage.css';

// 团队成员接口
interface TeamMember {
  icon: string;
  role: string;
  action: string;
  color?: string;
  highlight?: boolean;
}

interface FinalDecision {
  signal: 'BUY' | 'SELL' | 'HOLD';
  confidence: number;
}

interface DemoStep {
  title: string;
  subtitle: string;
  teamMembers: TeamMember[];
  finalDecision?: FinalDecision;
  tags: string[];
}

// 演示步骤数据 - 展示多 Agent 选股流程
const DEMO_STEPS: DemoStep[] = [
  {
    title: '数据采集',
    subtitle: '4位分析师并行工作',
    teamMembers: [
      { icon: '📊', role: '市场分析师', action: '正在分析行情数据...' },
      { icon: '📰', role: '新闻分析师', action: '正在抓取财经新闻...' },
      { icon: '💬', role: '舆情分析师', action: '正在扫描社交媒体...' },
      { icon: '📈', role: '基本面分析师', action: '正在获取财报数据...' },
    ],
    tags: ['实时行情', '财经新闻', '社交舆情', '财务报表']
  },
  {
    title: '多空研判',
    subtitle: '研究团队辩论会',
    teamMembers: [
      { icon: '多', role: '多头研究员', action: '整理积极信号', color: 'green' },
      { icon: '空', role: '空头研究员', action: '提出主要风险点', color: 'red' },
      { icon: '👔', role: '研究主管', action: '综合评估: 偏多', highlight: true },
    ],
    tags: ['多头观点', '空头观点', '辩证分析']
  },
  {
    title: '风控决策',
    subtitle: '风险管理决策',
    teamMembers: [
      { icon: '进', role: '进取风控', action: '评估观察价值', color: 'orange' },
      { icon: '稳', role: '稳健风控', action: '检查风险阈值', color: 'blue' },
      { icon: '守', role: '保守风控', action: '给出风险提示', color: 'gray' },
    ],
    finalDecision: { signal: 'HOLD', confidence: 78 },
    tags: ['候选池', '风险评估', '策略结论']
  }
];

export const GuidePage: React.FC = () => {
  const navigate = useNavigate();
  const { setFirstLoginComplete } = useAuth();
  const [step, setStep] = useState(0);
  const [visibleMembers, setVisibleMembers] = useState(0);
  const [showDecision, setShowDecision] = useState(false);

  const currentDemo = DEMO_STEPS[step];

  // 成员依次显示动画
  useEffect(() => {
    setVisibleMembers(0);
    setShowDecision(false);

    const totalMembers = currentDemo.teamMembers.length;
    let count = 0;

    const showInterval = setInterval(() => {
      count++;
      setVisibleMembers(count);
      if (count >= totalMembers) {
        clearInterval(showInterval);
        // 如果有最终决策，延迟显示
        if (currentDemo.finalDecision) {
          setTimeout(() => setShowDecision(true), 400);
        }
      }
    }, 200);

    return () => clearInterval(showInterval);
  }, [step, currentDemo]);

  const handleContinue = () => {
    setFirstLoginComplete();
    navigate('/home', { replace: true });
  };

  const nextStep = () => {
    if (step < DEMO_STEPS.length - 1) {
      setStep(step + 1);
    } else {
      handleContinue();
    }
  };

  // 渲染团队成员
  const renderTeamMembers = () => (
    <div className="team-card">
      {currentDemo.teamMembers.map((member, index) => (
        <div
          key={index}
          className={`team-member ${index < visibleMembers ? 'visible' : ''} ${member.highlight ? 'highlight' : ''}`}
          style={{ animationDelay: `${index * 0.1}s` }}
        >
          <span className="member-icon">{member.icon}</span>
          <span className="member-role">{member.role}</span>
          <span className={`member-action ${member.color || ''}`}>{member.action}</span>
        </div>
      ))}
    </div>
  );

  // 渲染最终决策
  const renderFinalDecision = () => {
    if (!currentDemo.finalDecision) return null;
    const { signal, confidence } = currentDemo.finalDecision;
    const signalClass = signal.toLowerCase();

    return (
      <div className={`final-decision ${showDecision ? 'visible' : ''}`}>
        <div className="decision-divider"></div>
        <div className="decision-content">
          <span className="decision-icon">🎯</span>
          <span className="decision-label">筛选结论</span>
          <span className={`signal-badge ${signalClass}`}>{signal}</span>
          <div className="confidence-wrapper">
            <span className="confidence-label">置信度</span>
            <div className="confidence-bar">
              <div
                className={`confidence-fill ${signalClass}`}
                style={{ width: showDecision ? `${confidence}%` : '0%' }}
              ></div>
            </div>
            <span className="confidence-value">{confidence}%</span>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="guide-page-v2">
      <div className="guide-container-v2">
        <h1>欢迎使用 智能选股 Agent</h1>
        <p className="subtitle">多 Agent 协作完成筛选流程</p>

        {/* 演示区域 */}
        <div className="demo-area">
          <div className="demo-step-header">
            <div className="demo-step-title">{currentDemo.title}</div>
            <div className="demo-step-subtitle">{currentDemo.subtitle}</div>
          </div>

          {/* 团队成员展示 */}
          {renderTeamMembers()}

          {/* 最终决策 */}
          {renderFinalDecision()}
        </div>

        {/* 进度指示 */}
        <div className="progress-dots">
          {DEMO_STEPS.map((_, i) => (
            <span
              key={i}
              className={`dot ${i === step ? 'active' : ''} ${i < step ? 'done' : ''}`}
              onClick={() => setStep(i)}
            />
          ))}
          <span className="step-text">步骤 {step + 1}/{DEMO_STEPS.length}</span>
        </div>

        {/* 数据来源标签 */}
        <div className="quick-tags">
          <span className="tags-label">数据来源:</span>
          {currentDemo.tags.map((tag, i) => (
            <span key={i} className="tag">{tag}</span>
          ))}
        </div>

        {/* 按钮区 */}
        <div className="btn-group">
          <button className="skip-btn" onClick={handleContinue}>跳过</button>
          <button className="next-btn" onClick={nextStep}>
            {step < DEMO_STEPS.length - 1 ? '下一步' : '开始使用'} →
          </button>
        </div>
      </div>
    </div>
  );
};
