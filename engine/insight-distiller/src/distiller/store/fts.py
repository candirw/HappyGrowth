"""中文全文检索的分词处理。

**索引侧与查询侧必须共用这里的函数。**
两边各写一份是这个模块存在的原因——原先查询侧做了二元组切分、
索引侧没做，导致中文 BM25 完全失效（查「山西」返回 0 条），
而且不报错、只是静默少了一路召回，极难察觉。

为什么需要切分：SQLite 的 unicode61 分词器把连续中文当作**一个 token**。
「这一次我们去了一趟山西」整串是一个 token，查「山西」匹配不到。

为什么不用 trigram 分词器：它要求查询至少 3 个字符，
而「山西」「折叠」「降息」这类 2 字词是中文检索里最常见的形态。
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 查询侧展开上限。过多 OR 子句会让 BM25 打分失真且拖慢检索。
_MAX_QUERY_TERMS = 24


def _is_cjk(token: str) -> bool:
    return bool(_CJK_RE.search(token))


def _bigrams(token: str) -> list[str]:
    """把中文词切成相邻二元组：「山西美食」→ 山西 西美 美食。"""
    return [token[i : i + 2] for i in range(len(token) - 1)]


def index_text(text: str) -> str:
    """索引侧：把原文转成 FTS 实际存储的 token 串。

    中文切二元组，英文与数字保留原样（它们本就被 unicode61 正确切分）。
    """
    if not text:
        return ""
    out: list[str] = []
    for token in _TOKEN_RE.findall(text):
        if _is_cjk(token):
            # 单字也要保留，否则「我」「你」这类查询无法命中
            out.extend(_bigrams(token) if len(token) > 1 else [token])
        elif len(token) > 1:
            out.append(token.lower())
    return " ".join(out)


def query_text(query: str) -> str:
    """查询侧：转成 FTS5 MATCH 表达式。

    用 OR 连接以提高召回；FTS5 对引号/星号/括号敏感，
    因此每个 term 都加引号转义。
    """
    if not query:
        return ""
    terms: list[str] = []
    for token in _TOKEN_RE.findall(query):
        if _is_cjk(token):
            terms.extend(_bigrams(token) if len(token) > 1 else [token])
        elif len(token) > 1:
            terms.append(token.lower())

    uniq = list(dict.fromkeys(terms))[:_MAX_QUERY_TERMS]
    # 转义内部引号，避免构造出非法的 MATCH 表达式
    return " OR ".join(f'"{t}"' for t in (x.replace('"', "") for x in uniq) if t)


__all__ = ["index_text", "query_text"]
