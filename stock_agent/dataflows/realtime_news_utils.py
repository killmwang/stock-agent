#!/usr/bin/env python3
"""
实时新闻数据获取工具
解决新闻滞后性问题
"""

import requests
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import time
import os
from dataclasses import dataclass


@dataclass
class NewsItem:
    """新闻项目数据结构"""
    title: str
    content: str
    source: str
    publish_time: datetime
    url: str
    urgency: str  # high, medium, low
    relevance_score: float


class RealtimeNewsAggregator:
    """实时新闻聚合器"""
    
    def __init__(self):
        self.headers = {
            'User-Agent': 'Stock-Agent/1.0'
        }
        
        # API密钥配置
        self.finnhub_key = os.getenv('FINNHUB_API_KEY')
        self.alpha_vantage_key = os.getenv('ALPHA_VANTAGE_API_KEY')
        self.newsapi_key = os.getenv('NEWSAPI_KEY')
        
    def get_realtime_stock_news(self, ticker: str, hours_back: int = 6) -> List[NewsItem]:
        """
        获取实时股票新闻
        优先级：专业API > 新闻API > 搜索引擎
        """
        all_news = []
        
        # 1. FinnHub实时新闻 (最高优先级)
        finnhub_news = self._get_finnhub_realtime_news(ticker, hours_back)
        all_news.extend(finnhub_news)
        
        # 2. Alpha Vantage新闻
        av_news = self._get_alpha_vantage_news(ticker, hours_back)
        all_news.extend(av_news)
        
        # 3. NewsAPI (如果配置了)
        if self.newsapi_key:
            newsapi_news = self._get_newsapi_news(ticker, hours_back)
            all_news.extend(newsapi_news)
        
        # 4. 中文财经新闻源
        chinese_news = self._get_chinese_finance_news(ticker, hours_back)
        all_news.extend(chinese_news)
        
        # 去重和排序
        unique_news = self._deduplicate_news(all_news)
        return sorted(unique_news, key=lambda x: x.publish_time, reverse=True)
    
    def _get_finnhub_realtime_news(self, ticker: str, hours_back: int) -> List[NewsItem]:
        """获取FinnHub实时新闻"""
        if not self.finnhub_key:
            return []
        
        try:
            # 计算时间范围
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=hours_back)
            
            # FinnHub API调用
            url = "https://finnhub.io/api/v1/company-news"
            params = {
                'symbol': ticker,
                'from': start_time.strftime('%Y-%m-%d'),
                'to': end_time.strftime('%Y-%m-%d'),
                'token': self.finnhub_key
            }
            
            response = requests.get(url, params=params, headers=self.headers)
            response.raise_for_status()
            
            news_data = response.json()
            news_items = []
            
            for item in news_data:
                # 检查新闻时效性
                publish_time = datetime.fromtimestamp(item.get('datetime', 0))
                if publish_time < start_time:
                    continue
                
                # 评估紧急程度
                urgency = self._assess_news_urgency(item.get('headline', ''), item.get('summary', ''))
                
                news_items.append(NewsItem(
                    title=item.get('headline', ''),
                    content=item.get('summary', ''),
                    source=item.get('source', 'FinnHub'),
                    publish_time=publish_time,
                    url=item.get('url', ''),
                    urgency=urgency,
                    relevance_score=self._calculate_relevance(item.get('headline', ''), ticker)
                ))
            
            return news_items
            
        except Exception as e:
            print(f"FinnHub新闻获取失败: {e}")
            return []
    
    def _get_alpha_vantage_news(self, ticker: str, hours_back: int) -> List[NewsItem]:
        """获取Alpha Vantage新闻"""
        if not self.alpha_vantage_key:
            return []
        
        try:
            url = "https://www.alphavantage.co/query"
            params = {
                'function': 'NEWS_SENTIMENT',
                'tickers': ticker,
                'apikey': self.alpha_vantage_key,
                'limit': 50
            }
            
            response = requests.get(url, params=params, headers=self.headers)
            response.raise_for_status()
            
            data = response.json()
            news_items = []
            
            if 'feed' in data:
                for item in data['feed']:
                    # 解析时间
                    time_str = item.get('time_published', '')
                    try:
                        publish_time = datetime.strptime(time_str, '%Y%m%dT%H%M%S')
                    except:
                        continue
                    
                    # 检查时效性
                    if publish_time < datetime.now() - timedelta(hours=hours_back):
                        continue
                    
                    urgency = self._assess_news_urgency(item.get('title', ''), item.get('summary', ''))
                    
                    news_items.append(NewsItem(
                        title=item.get('title', ''),
                        content=item.get('summary', ''),
                        source=item.get('source', 'Alpha Vantage'),
                        publish_time=publish_time,
                        url=item.get('url', ''),
                        urgency=urgency,
                        relevance_score=self._calculate_relevance(item.get('title', ''), ticker)
                    ))
            
            return news_items
            
        except Exception as e:
            print(f"Alpha Vantage新闻获取失败: {e}")
            return []
    
    def _get_newsapi_news(self, ticker: str, hours_back: int) -> List[NewsItem]:
        """获取NewsAPI新闻"""
        try:
            # 构建搜索查询
            company_names = {
                'AAPL': 'Apple',
                'TSLA': 'Tesla', 
                'NVDA': 'NVIDIA',
                'MSFT': 'Microsoft',
                'GOOGL': 'Google'
            }
            
            query = f"{ticker} OR {company_names.get(ticker, ticker)}"
            
            url = "https://newsapi.org/v2/everything"
            params = {
                'q': query,
                'language': 'en',
                'sortBy': 'publishedAt',
                'from': (datetime.now() - timedelta(hours=hours_back)).isoformat(),
                'apiKey': self.newsapi_key
            }
            
            response = requests.get(url, params=params, headers=self.headers)
            response.raise_for_status()
            
            data = response.json()
            news_items = []
            
            for item in data.get('articles', []):
                # 解析时间
                time_str = item.get('publishedAt', '')
                try:
                    publish_time = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                except:
                    continue
                
                urgency = self._assess_news_urgency(item.get('title', ''), item.get('description', ''))
                
                news_items.append(NewsItem(
                    title=item.get('title', ''),
                    content=item.get('description', ''),
                    source=item.get('source', {}).get('name', 'NewsAPI'),
                    publish_time=publish_time,
                    url=item.get('url', ''),
                    urgency=urgency,
                    relevance_score=self._calculate_relevance(item.get('title', ''), ticker)
                ))
            
            return news_items
            
        except Exception as e:
            print(f"NewsAPI新闻获取失败: {e}")
            return []
    
    def _get_chinese_finance_news(self, ticker: str, hours_back: int) -> List[NewsItem]:
        """获取中文财经新闻"""
        # 这里可以集成中文财经新闻API
        # 例如：财联社、新浪财经、东方财富等
        
        try:
            # 示例：集成财联社API (需要申请)
            # 或者使用RSS源
            news_items = []
            
            # 财联社RSS (如果可用)
            rss_sources = [
                "https://www.cls.cn/api/sw?app=CailianpressWeb&os=web&sv=7.7.5",
                # 可以添加更多RSS源
            ]
            
            for rss_url in rss_sources:
                try:
                    items = self._parse_rss_feed(rss_url, ticker, hours_back)
                    news_items.extend(items)
                except:
                    continue
            
            return news_items
            
        except Exception as e:
            print(f"中文财经新闻获取失败: {e}")
            return []
    
    def _parse_rss_feed(self, rss_url: str, ticker: str, hours_back: int) -> List[NewsItem]:
        """解析RSS源"""
        # 简化实现，实际需要使用feedparser库
        return []
    
    def _assess_news_urgency(self, title: str, content: str) -> str:
        """评估新闻紧急程度"""
        text = (title + ' ' + content).lower()
        
        # 高紧急度关键词
        high_urgency_keywords = [
            'breaking', 'urgent', 'alert', 'emergency', 'halt', 'suspend',
            '突发', '紧急', '暂停', '停牌', '重大'
        ]
        
        # 中等紧急度关键词
        medium_urgency_keywords = [
            'earnings', 'report', 'announce', 'launch', 'merger', 'acquisition',
            '财报', '发布', '宣布', '并购', '收购'
        ]
        
        if any(keyword in text for keyword in high_urgency_keywords):
            return 'high'
        elif any(keyword in text for keyword in medium_urgency_keywords):
            return 'medium'
        else:
            return 'low'
    
    def _calculate_relevance(self, title: str, ticker: str) -> float:
        """计算新闻相关性分数"""
        text = title.lower()
        ticker_lower = ticker.lower()
        
        # 基础相关性
        if ticker_lower in text:
            return 1.0
        
        # 公司名称匹配
        company_names = {
            'aapl': ['apple', 'iphone', 'ipad', 'mac'],
            'tsla': ['tesla', 'elon musk', 'electric vehicle'],
            'nvda': ['nvidia', 'gpu', 'ai chip'],
            'msft': ['microsoft', 'windows', 'azure'],
            'googl': ['google', 'alphabet', 'search']
        }
        
        if ticker_lower in company_names:
            for name in company_names[ticker_lower]:
                if name in text:
                    return 0.8
        
        return 0.3  # 默认相关性
    
    def _deduplicate_news(self, news_items: List[NewsItem]) -> List[NewsItem]:
        """去重新闻"""
        seen_titles = set()
        unique_news = []
        
        for item in news_items:
            # 简单的标题去重
            title_key = item.title.lower().strip()
            if title_key not in seen_titles and len(title_key) > 10:
                seen_titles.add(title_key)
                unique_news.append(item)
        
        return unique_news
    
    def format_news_report(self, news_items: List[NewsItem], ticker: str) -> str:
        """格式化新闻报告"""
        if not news_items:
            return f"未获取到{ticker}的实时新闻数据。"
        
        # 按紧急程度分组
        high_urgency = [n for n in news_items if n.urgency == 'high']
        medium_urgency = [n for n in news_items if n.urgency == 'medium']
        low_urgency = [n for n in news_items if n.urgency == 'low']
        
        report = f"# {ticker} 实时新闻分析报告\n\n"
        report += f"📅 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        report += f"📊 新闻总数: {len(news_items)}条\n\n"
        
        if high_urgency:
            report += "## 🚨 紧急新闻\n\n"
            for news in high_urgency[:3]:  # 最多显示3条
                report += f"### {news.title}\n"
                report += f"**来源**: {news.source} | **时间**: {news.publish_time.strftime('%H:%M')}\n"
                report += f"{news.content}\n\n"
        
        if medium_urgency:
            report += "## 📢 重要新闻\n\n"
            for news in medium_urgency[:5]:  # 最多显示5条
                report += f"### {news.title}\n"
                report += f"**来源**: {news.source} | **时间**: {news.publish_time.strftime('%H:%M')}\n"
                report += f"{news.content}\n\n"
        
        # 添加时效性说明
        latest_news = max(news_items, key=lambda x: x.publish_time)
        time_diff = datetime.now() - latest_news.publish_time
        
        report += f"\n## ⏰ 数据时效性\n"
        report += f"最新新闻发布于: {time_diff.total_seconds() / 60:.0f}分钟前\n"
        
        if time_diff.total_seconds() < 1800:  # 30分钟内
            report += "🟢 数据时效性: 优秀 (30分钟内)\n"
        elif time_diff.total_seconds() < 3600:  # 1小时内
            report += "🟡 数据时效性: 良好 (1小时内)\n"
        else:
            report += "🔴 数据时效性: 一般 (超过1小时)\n"
        
        return report


def get_realtime_stock_news(ticker: str, curr_date: str, hours_back: int = 6) -> str:
    """
    获取实时股票新闻的主要接口函数
    """
    aggregator = RealtimeNewsAggregator()
    
    try:
        # 获取实时新闻
        news_items = aggregator.get_realtime_stock_news(ticker, hours_back)
        
        # 格式化报告
        report = aggregator.format_news_report(news_items, ticker)
        
        return report
        
    except Exception as e:
        return f"""
实时新闻获取失败 - {ticker}
分析日期: {curr_date}

❌ 错误信息: {str(e)}

💡 备用建议:
1. 检查API密钥配置 (FINNHUB_API_KEY, NEWSAPI_KEY)
2. 使用基础新闻分析作为备选
3. 关注官方财经媒体的最新报道
4. 考虑使用专业金融终端获取实时新闻

注: 实时新闻获取依赖外部API服务的可用性。
"""
