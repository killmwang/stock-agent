/**
 * 聊天页面 - 对话模式
 */
import React, { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { API_BASE_URL } from '../../api/config';
import './ChatPage.css';

// 获取 token
const getToken = () => localStorage.getItem('token');

interface Message {
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
}

interface ThinkingStep {
  type: 'thinking' | 'tool' | 'error';
  content: string;
}

// 热门提示
const QUICK_PROMPTS = [
  { icon: '📈', text: '贵州茅台今天行情怎么样？' },
  { icon: '💰', text: '比亚迪的估值分析' },
  { icon: '📊', text: '宁德时代的资金流向' },
  { icon: '🔍', text: '帮我查一下招商银行的基本面' },
  { icon: '🔥', text: '今日成交额前10' },
  { icon: '💹', text: '今日涨幅榜前20' },
];

export const ChatPage: React.FC = () => {
  const navigate = useNavigate();
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'assistant',
      content:
        '您好！我是智能选股 Agent 助手，可以帮您查询股票信息、解释分析报告、梳理风险点。请问有什么可以帮您的？',
    },
  ]);
  const [input, setInput] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [thinkingSteps, setThinkingSteps] = useState<ThinkingStep[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, thinkingSteps]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMessage = input.trim();
    setInput('');

    // 添加用户消息
    setMessages((prev) => [...prev, { role: 'user', content: userMessage }]);
    setIsLoading(true);
    setThinkingSteps([]);

    try {
      // 使用 SSE 流式请求
      const response = await fetch(`${API_BASE_URL}/api/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${getToken()}`,
        },
        body: JSON.stringify({
          message: userMessage,
          conversation_id: conversationId,
        }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();

      if (!reader) {
        throw new Error('No reader available');
      }

      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // 解析 SSE 事件
        const lines = buffer.split('\n');
        buffer = lines.pop() || ''; // 保留未完成的行

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const event = JSON.parse(line.slice(6));

              if (event.type === 'done') {
                // 完成，添加最终回复
                setMessages((prev) => [
                  ...prev,
                  { role: 'assistant', content: event.content },
                ]);
                if (event.conversation_id) {
                  setConversationId(event.conversation_id);
                }
                setThinkingSteps([]);
              } else if (event.type === 'error') {
                // 错误
                setMessages((prev) => [
                  ...prev,
                  { role: 'assistant', content: `抱歉，出现错误: ${event.content}` },
                ]);
                setThinkingSteps([]);
              } else {
                // 思考步骤
                setThinkingSteps((prev) => [...prev, event]);
              }
            } catch (parseError) {
              console.error('Failed to parse SSE event:', parseError);
            }
          }
        }
      }
    } catch (error) {
      console.error('Chat error:', error);
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: '抱歉，出现了网络错误，请重试。' },
      ]);
      setThinkingSteps([]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleNewChat = () => {
    setMessages([
      {
        role: 'assistant',
        content:
          '您好！我是智能选股 Agent 助手，可以帮您查询股票信息、解释分析报告、梳理风险点。请问有什么可以帮您的？',
      },
    ]);
    setConversationId(undefined);
    setThinkingSteps([]);
  };

  const getStepIcon = (type: string) => {
    switch (type) {
      case 'thinking':
        return '💭';
      case 'tool':
        return '🔧';
      case 'error':
        return '❌';
      default:
        return '•';
    }
  };

  // 点击快捷提示
  const handleQuickPrompt = (text: string) => {
    setInput(text);
    inputRef.current?.focus();
  };

  // 是否显示快捷提示（只在初始欢迎消息时显示）
  const showQuickPrompts = messages.length === 1 && messages[0].role === 'assistant';

  return (
    <div className="chat-page">
      <header className="chat-header">
        <button className="back-btn" onClick={() => navigate('/home')}>
          ←
        </button>
        <h1>智能对话</h1>
        <button className="new-chat-btn" onClick={handleNewChat}>
          新对话
        </button>
      </header>

      <div className="messages-container">
        {messages.map((msg, idx) => (
          <div key={idx} className={`message ${msg.role}`}>
            <div className="message-content">
              {msg.role === 'assistant' ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
              ) : (
                msg.content
              )}
            </div>
          </div>
        ))}

        {/* 快捷提示气泡 */}
        {showQuickPrompts && (
          <div className="quick-prompts">
            <p className="quick-prompts-title">试试这些问题：</p>
            <div className="quick-prompts-grid">
              {QUICK_PROMPTS.map((prompt, idx) => (
                <button
                  key={idx}
                  className="quick-prompt-btn"
                  onClick={() => handleQuickPrompt(prompt.text)}
                >
                  <span className="prompt-icon">{prompt.icon}</span>
                  <span className="prompt-text">{prompt.text}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* 思考过程进度 - 只显示最新状态 */}
        {isLoading && thinkingSteps.length > 0 && (
          <div className="message assistant">
            <div className="thinking-box">
              <div className={`thinking-step ${thinkingSteps[thinkingSteps.length - 1].type}`}>
                <span className="step-icon">{getStepIcon(thinkingSteps[thinkingSteps.length - 1].type)}</span>
                <span className="step-content">{thinkingSteps[thinkingSteps.length - 1].content}</span>
              </div>
            </div>
          </div>
        )}

        {/* 等待中但没有进度步骤时显示加载动画 */}
        {isLoading && thinkingSteps.length === 0 && (
          <div className="message assistant">
            <div className="message-content loading">
              <span className="dot"></span>
              <span className="dot"></span>
              <span className="dot"></span>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      <form className="input-container" onSubmit={handleSubmit}>
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="输入问题..."
          disabled={isLoading}
        />
        <button type="submit" disabled={isLoading || !input.trim()}>
          发送
        </button>
      </form>
    </div>
  );
};
