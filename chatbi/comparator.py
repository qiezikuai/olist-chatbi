"""P3.2：执行结果比对器——行级规范化比对（非字符串比对）。

口径（PLAN_v2 第 7 节）：分母=30，分子=「执行成功且结果一致」的题数；SQL 文本相似度不参与判定。
比对的是**结果集**，不是 SQL 文本，也不是单元格的字符串形态。

规范化规则：
  - 数值（int/float/Decimal/纯数字字符串）→ round(float, 2)，即 2 位小数容差
    （比 PLAN 的"取整容差"更细，更保守，避免 137.4/137.6 被误判相等）
  - datetime/date → ISO 字符串（消除时间对象与文本的形态差异）
  - 其他字符串 → strip + casefold（消除空白与大小写差异，如 'SP ' == 'sp'）
  - None 保留
  - 行级：排序无关化（同一结果集换序仍判一致）
  - 列：只比**值**、忽略列名/别名；但**列数与列序须一致**（位置对齐比对）

已知的保守取舍（忠实 PLAN 严格口径，刻意不做"魔法"语义对齐）：
  表示级差异即使语义相同也判**不一致**，例如月份 `DATE_FORMAT→'2018-01'`（字符串）
  vs `MONTH()→1`（整数）。这类会让准确率被低估而非高估，可接受；P3.3 报告会标注归因。
"""
from __future__ import annotations

import datetime
import re
from decimal import Decimal

_NUM_STR = re.compile(r"^-?\d+(?:\.\d+)?$")


def normalize_cell(c):
    """把单个单元格归一为可比较的标量。"""
    if c is None:
        return None
    if isinstance(c, bool):                      # bool 先于 int 判定（True 是 int 子类）
        return float(c)
    if isinstance(c, (int, float, Decimal)):
        return round(float(c), 2)
    if isinstance(c, datetime.datetime):         # 须先于 date 判定（datetime 是 date 子类）
        # 午夜整点的 datetime 与同日期 date 视为同一值，消除 DATE vs DATETIME 形态差异
        if (c.hour, c.minute, c.second, c.microsecond) == (0, 0, 0, 0):
            return c.date().isoformat()
        return c.isoformat()
    if isinstance(c, datetime.date):
        return c.isoformat()
    if isinstance(c, (bytes, bytearray)):
        c = c.decode("utf-8", "replace")
    s = str(c).strip()
    if _NUM_STR.match(s):                        # 纯数字字符串按数值比（消除 '100' vs 100）
        return round(float(s), 2)
    return s.casefold()


def normalize_row(row) -> tuple:
    return tuple(normalize_cell(c) for c in row)


def _sort_key(row: tuple):
    # 混合类型安全排序键：None 最前，其余按字符串
    return tuple((0, "") if x is None else (1, str(x)) for x in row)


def normalize_result(rows) -> list[tuple]:
    """行级规范化：逐行归一 + 行排序无关化。"""
    return sorted((normalize_row(r) for r in rows), key=_sort_key)


def results_match(ref_rows, gen_rows) -> tuple[bool, str]:
    """比对参考结果集与生成结果集是否一致。返回 (是否一致, 原因)。

    一致条件：行数相同、每行列数相同、按位置逐格归一后相等（行序无关）。
    """
    a = normalize_result(ref_rows)
    b = normalize_result(gen_rows)
    if len(a) != len(b):
        return False, f"行数不同：参考 {len(a)} 行 vs 生成 {len(b)} 行"
    for i, (ra, rb) in enumerate(zip(a, b)):
        if len(ra) != len(rb):
            return False, f"第 {i} 行列数不同：参考 {len(ra)} vs 生成 {len(rb)}"
        if ra != rb:
            return False, f"第 {i} 行不一致：参考 {ra} vs 生成 {rb}"
    return True, "一致"
