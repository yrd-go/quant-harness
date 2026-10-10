# Mermaid 渲染兼容性测试

这是一个**临时测试文件**，用于定位哪种 Mermaid 语法能在 GitHub 上正常渲染。
请告诉我哪些变体渲染成了图、哪些显示 `Unable to render rich display`。

## 变体 1: subgraph + ID标签 + 中文（当前版本）

```mermaid
flowchart TB
    subgraph L1["第一层 全市场海选"]
        A1["全市场快照 5571 只"] --> A2["硬性剔除 ST / 北交所 / 停牌"]
    end
    subgraph L2["第二层 宏观过滤"]
        B1["财新新闻 100 条"] --> B2["目标池 15 只"]
    end
    A2 --> B1
    B2 --> C1["买入 / 持有 / 卖出"]
```

## 变体 2: 无 subgraph + 中文标签

```mermaid
flowchart TB
    A1["全市场快照 5571 只"] --> A2["硬性剔除 ST / 北交所 / 停牌"]
    A2 --> A3["成交额 Top 300"]
    A3 --> A4["20 日动量 Top 100"]
    A4 --> B1["财新宏观新闻 100 条摘要"]
    B1 --> B3["合并为单份提示词"]
    B3 --> B4{"1 次大模型调用"}
    B4 --> B5["目标池 15 只"]
    B5 --> C1["基本面分析师"]
    B5 --> C2["技术面分析师"]
    B5 --> C3["风险管理员"]
    C1 --> C4["投资组合经理"]
    C2 --> C4
    C3 --> C4
    C4 --> E1["买入 / 持有 / 卖出"]
```

## 变体 3: 无 subgraph + 纯英文标签

```mermaid
flowchart TB
    A1["Market snapshot 5571"] --> A2["Hard filter ST / BSE / suspended"]
    A2 --> A3["Top 300 by turnover"]
    A3 --> A4["Top 100 by momentum"]
    A4 --> B1["Macro news 100 items"]
    B1 --> B3["Merge into one prompt"]
    B3 --> B4{"1 LLM call"}
    B4 --> B5["Target pool 15"]
    B5 --> C4["Portfolio manager"]
    C4 --> E1["Buy / Hold / Sell"]
```

## 变体 4: 极简

```mermaid
flowchart TB
    A["5571 stocks"] --> B["300 stocks"]
    B --> C["100 stocks"]
    C --> D["15 stocks"]
    D --> E["Buy / Hold / Sell"]
```
