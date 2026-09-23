"""P2.4：口径守卫 + 空结果判定（纯函数，可单测、不连库、不调 LLM）。

口径守卫的依据是 docs/metrics.md（项目唯一口径事实源）。这里把其中可机器校验的
高价值口径固化为规则：只对「问题里出现的意图」做检查，命中意图才校验对应 SQL 特征。
设计取舍：规则用关键词 + 子串匹配，够用且可逐行讲；不引入 SQL 解析器（MVP 不划算）。
"""
from __future__ import annotations

from chatbi.executor import SqlResult

# 每条规则 = (意图关键词列表, 判定函数 sql_lower->是否合规, 违规时给出的口径文本)
# 判定函数返回 True=命中口径；返回 False=违规，触发重写并把口径文本回喂 LLM。
METRIC_RULES = [
    (
        ["gmv", "成交额", "销售额", "总金额", "交易额", "卖了多少钱"],
        lambda s: ("price" in s) and ("canceled" in s or "unavailable" in s),
        "GMV 口径=SUM(olist_order_items.price) 且排除 canceled/unavailable 订单（运费不计入）",
    ),
    (
        ["复购", "回头客", "多少个客户", "多少客户", "客户数", "人数", "自然人", "多少个用户"],
        lambda s: "customer_unique_id" in s,
        "客户数/复购口径=用 customer_unique_id（自然人），不是每单唯一的 customer_id",
    ),
    (
        ["类目", "品类", "category", "商品类别"],
        lambda s: ("product_category_name_english" in s) or ("category_translation" in s),
        "类目口径=经 olist_product_category_translation 表转英语类目",
    ),
    (
        ["黑五", "黑色星期五", "black friday", "black-friday"],
        lambda s: "2017-11" in s,
        "黑五口径=order_purchase_timestamp 落在 2017-11（对比 2017-10）",
    ),
    (
        ["客单价", "aov", "每单均价", "平均订单金额"],
        lambda s: ("price" in s) and ("count(distinct" in s.replace("  ", " ")),
        "客单价口径=GMV ÷ 去重订单数 COUNT(DISTINCT order_id)，非 GMV÷人数",
    ),
]


def check_caliber(question: str, sql: str) -> list[str]:
    """返回违反的口径规则文本列表；空列表 = 全部命中（或问题不涉及任何已固化口径）。"""
    q = (question or "").lower()
    s = (sql or "").lower()
    violations: list[str] = []
    for keywords, ok_fn, rule in METRIC_RULES:
        if any(k.lower() in q for k in keywords):
            try:
                if not ok_fn(s):
                    violations.append(rule)
            except Exception:
                violations.append(rule)   # 判定函数异常按违规处理，保守起见触发重写
    return violations


def is_empty_result(result: SqlResult) -> bool:
    """执行成功但 0 行 —— 触发空结果守卫（注意：0 也可能是合法答案，守卫只重试 1 轮）。"""
    return bool(result.ok) and result.row_count == 0
