"""配置系统。

设计要点：
- 任务级 provider 指定（map / reduce / qa 各自可选不同模型），
  并支持 profile 一键整体切换（local / hybrid / quality）。
- API Key 用 ${ENV_VAR} 占位从环境变量读取，绝不写进配置文件。
- 配置缺失时给出可用的默认值，保证零配置也能跑起来（走本地 ollama 或提示）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .compat.fsio import ENCODING

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """递归展开 ${VAR} 占位符。未设置的环境变量展开为 None，便于后续校验提示。"""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), "")

        expanded = _ENV_PATTERN.sub(repl, value)
        return expanded or None
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class Price(BaseModel):
    input: float = 0.0
    output: float = 0.0


# 明确需要 API Key 的云服务类型。
# 其他类型（本地部署、自定义 provider）不做强校验——
# 否则会误伤 vLLM / LM Studio 这类无需认证的 OpenAI 兼容端点。
_KEY_REQUIRED_TYPES = {"openai_compat", "anthropic"}
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal")


class ProviderConfig(BaseModel):
    type: str  # openai_compat | anthropic | ollama
    base_url: str | None = None
    api_key: str | None = None
    models: dict[str, str] = Field(default_factory=dict)
    price_per_1m: Price = Field(default_factory=Price)
    timeout: int = 120

    @property
    def usable(self) -> bool:
        """判断该 provider 当前是否可用。

        规则：非云服务类型直接放行；云服务类型需要 key，
        但指向本地端点时视为无需认证（自建代理、vLLM、LM Studio）。
        """
        if self.type not in _KEY_REQUIRED_TYPES:
            return True
        if self.api_key:
            return True
        if self.base_url and any(h in self.base_url for h in _LOCAL_HOSTS):
            return True
        return False


class TaskConfig(BaseModel):
    provider: str
    model: str = "chat"
    temperature: float = 0.2
    max_tokens: int | None = None


class DistillSections(BaseModel):
    tldr: bool = True
    outline: bool = True
    concepts: bool = True
    quotes: bool = True
    actions: bool = True
    open_questions: bool = True
    contrarian: bool = True


class DistillConfig(BaseModel):
    chunk_size: int = 2000
    chunk_overlap: int = 200
    language: str = "zh"
    sections: DistillSections = Field(default_factory=DistillSections)
    # 密度低于此分只出 TL;DR，不浪费算力细读
    skip_detail_below: float = 20.0
    # 标题本身是问句时（「为什么X？」「如何Y？」）走问答 schema：
    # 产出「一句话直答 + 要点 + 引申问题」而非通用摘要。
    # 问答体内容用摘要结构装，读者拿到的是片段而不是答案。
    qa_mode: bool = True


class PlatformConfig(BaseModel):
    """单个平台的开关与参数覆盖。

    为什么需要：平台之间的风控强度差异很大，全局一套参数不够用。
    小红书需要 cookie 且必须慢，B 站不配 cookie 就拿不到字幕，
    而普通网页什么都不需要。
    """

    enabled: bool = True
    # 覆盖全局同名项；None 表示沿用全局
    cookies_from_browser: str | None = None
    proxy: str | None = None
    user_agent: str | None = None
    timeout: int | None = None
    retry: int | None = None
    # 同平台连续请求的最小间隔（秒）。风控严的平台调大
    rate_limit_sec: float = 0.0
    # 附加请求头，如 Referer
    headers: dict[str, str] = Field(default_factory=dict)


class SiteRule(BaseModel):
    """声明式站点规则：不写代码就能加一个**普通 HTML 站点**。

    适用范围要说清楚——只覆盖「正文在固定 DOM 节点里」的站点
    （个人博客、新闻、文档站）。需要签名、动态渲染或私有 API 的平台
    （B 站字幕、小红书、抖音）无法用选择器表达，必须写采集器。
    """

    name: str
    # 命中任一即生效，按子串匹配域名
    match: list[str] = Field(default_factory=list)
    # CSS 选择器
    title: str | None = None
    content: str | None = None
    author: str | None = None
    date: str | None = None
    # 抽取前先移除的噪声节点（导航、评论、推荐位）
    remove: list[str] = Field(default_factory=list)
    enabled: bool = True


class IngestConfig(BaseModel):
    cookies_from_browser: str | None = None
    proxy: str | None = None
    timeout: int = 30
    retry: int = 2
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    # 正文字数低于此值时判定为「可能是图片型文章」，触发 OCR
    ocr_trigger_chars: int = 200
    ocr_engine: str = "auto"  # auto | rapidocr | ocrmac | off

    # 每平台开关与参数。键为采集器名（bilibili/wechat/zhihu/xhs/web）
    platforms: dict[str, PlatformConfig] = Field(default_factory=dict)
    # 声明式站点规则
    sites: list[SiteRule] = Field(default_factory=list)

    def platform(self, name: str) -> PlatformConfig:
        """取某平台的有效配置。未配置则返回默认值（启用、无覆盖）。"""
        return self.platforms.get(name) or PlatformConfig()

    def is_enabled(self, name: str) -> bool:
        return self.platform(name).enabled

    def effective(self, name: str) -> "IngestConfig":
        """把平台级覆盖合并进全局，生成该平台实际使用的配置。

        采集器只看这一份，不必自己处理「有没有覆盖」的逻辑。
        """
        p = self.platform(name)
        merged = self.model_copy(deep=True)
        for field in ("cookies_from_browser", "proxy", "user_agent", "timeout", "retry"):
            value = getattr(p, field)
            if value is not None:
                setattr(merged, field, value)
        return merged


class StorageConfig(BaseModel):
    data_dir: str | None = None       # None = 项目内 ./data
    use_system_dir: bool = False
    keep_audio: bool = False


class TranscribeConfig(BaseModel):
    enabled: bool = True
    prefer_subtitle: bool = True      # 有字幕就跳过 ASR（快约 50 倍且免费）
    engine: str = "faster_whisper"    # 基准实现，三平台通用
    model: str = "large-v3"           # 中文场景不建议降级，medium 错字率明显更高
    language: str | None = None       # None = 自动检测
    compute_type: str = "auto"        # auto 按平台探测
    vad: bool = True                  # 切静音：省算力 + 防幻觉重复
    diarize: bool = False             # 说话人分离（最慢的一环，访谈类才开）
    hotwords: list[str] = Field(default_factory=list)


class RetrievalConfig(BaseModel):
    # 默认小模型：加载快、体积小。中文效果要求更高可换 BAAI/bge-m3（约 2.2GB）
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    backend: str = "sqlite"           # sqlite（默认，零额外依赖）| lancedb
    hybrid: bool = True               # 向量 + BM25 混合召回
    top_k: int = 30                   # 各路召回数量
    top_n: int = 6                    # 最终送入 LLM 的片段数
    chunk_size: int = 600             # 检索用块比浓缩用块更小，定位更精确
    chunk_overlap: int = 80
    # 消化后自动建索引。关掉可加快消化，之后用 `distill index` 批量补建
    auto_index: bool = True


class PaperConfig(BaseModel):
    """论文模块（M3）。

    数据源分工基于实测（见 DECISIONS.md）：
    arXiv 最稳定作主力，S2 供引文图谱，OpenAlex 仅单篇查询。
    """

    fulltext: bool = True             # 下载 PDF 取全文；关掉则只用摘要
    cache_ttl_days: int = 30          # 引文关系变化极慢，长缓存收益高
    # 进 OpenAlex/Crossref 的 polite pool，响应更稳定
    mailto: str | None = None
    similar_per_group: int = 5        # 相似论文每组展示数量
    lineage_limit: int = 8            # 阅读路径最多几步


class KnowledgeConfig(BaseModel):
    """知识关联（M3.5）。

    关联必须自动化：靠用户手动打标签/双链的成本高到必然放弃，
    这是所有笔记工具沦为「写入即遗忘」的根因（见 KNOWLEDGE.md）。
    """

    auto_link: bool = True            # 入库时自动建立关联
    extract_entities: bool = True
    extract_claims: bool = True
    # 只对密度达标的内容抽 claim——水文没有可用的原子知识
    claim_min_density: float = 35.0
    # 只提示高置信度关系，且每篇上限 N 条，否则提示会变成骚扰
    min_relation_confidence: float = 0.7
    max_relations_per_source: int = 5
    # 实体入图谱的门槛，防止垃圾实体淹没图谱
    entity_min_mentions: int = 2
    max_entities_per_source: int = 25
    # 用户自定义领域规则，会与内置合并
    domains: dict[str, dict] = Field(default_factory=dict)


class ReviewConfig(BaseModel):
    """重温与沉淀（M4）。

    **不是所有内容都值得重温**——全部入队只会让重温变成负担，
    最后必然放弃（所有 PKM 工具的通病）。因此入队要筛选。
    """

    auto_enqueue: bool = True         # 消化后自动判定是否加入重温队列
    min_density: float = 45.0         # 密度门槛：水文不占用重温时间
    # 要求有「可执行项/疑问/关联」之一才入队，避免纯资讯类内容堆积
    require_value: bool = True
    daily_limit: int = 8              # 每次 review 最多推几条
    brief_days: int = 7               # 简报统计区间
    # Obsidian 导出目录；None 则需在命令行指定
    vault_dir: str | None = None


class DomainAgentConfig(BaseModel):
    """领域级 agent 规则（M6）。

    理财领域是本设计的主要驱动场景：金融知识会过期，
    而自媒体观点被当成客观事实是最常见的亏钱方式。
    """

    extract_numeric: bool = True        # 强制抽取数字与单位，供 agent 计算
    require_as_of: bool = False         # 无时间标注的 claim 降低可信度
    # 来源权威度加权。理财场景下自媒体应大幅降权
    authority_weights: dict[str, float] = Field(default_factory=dict)
    # 超过这个天数的结论默认标记为陈旧
    default_max_staleness_days: int | None = None
    disclaimer: bool = False
    disclaimer_text: str | None = None


class AgentConfig(BaseModel):
    """Agent 接口（M6）。

    默认全部关闭且仅绑本机：这是给外部程序访问知识库的入口，
    不该在用户没有明确开启时就监听端口。
    """

    enabled: bool = True
    host: str = "127.0.0.1"             # 默认只监听本机
    port: int = 8787
    # 读写 token 分离：写接口风险远高于读接口
    read_token: str | None = None
    write_token: str | None = None
    allow_write: bool = False           # 写接口默认关闭
    default_token_budget: int | None = None
    # 语义检索的相关性门槛。低于此值视为不相关，避免「库里没有的主题」
    # 也返回无关 claim 导致 agent 硬答。长文本库可适当下调。
    semantic_min_sim: float = 0.50
    domains: dict[str, DomainAgentConfig] = Field(default_factory=dict)


class SubscribeConfig(BaseModel):
    """订阅自动化（M6）。

    默认值偏保守：自动化最怕的是没人看着时烧光预算。
    """

    default_interval_hours: int = 24
    default_max_items: int = 5        # 单次上限
    default_min_density: float | None = 45.0
    digest_dir: str | None = None


class LimitsConfig(BaseModel):
    monthly_budget_usd: float = 10.0
    warn_per_task_usd: float = 0.3


class Config(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    tasks: dict[str, TaskConfig] = Field(default_factory=dict)
    profiles: dict[str, dict[str, str]] = Field(default_factory=dict)
    distill: DistillConfig = Field(default_factory=DistillConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    subscribe: SubscribeConfig = Field(default_factory=SubscribeConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    def task(self, name: str, profile: str | None = None) -> TaskConfig:
        """解析某任务实际使用的模型配置。

        profile 会覆盖 tasks 里的 provider 指定，用于 `--profile local` 这类临时切换。
        """
        cfg = self.tasks.get(name)
        if cfg is None:
            # 无显式配置时回退到第一个可用 provider，保证开箱能跑
            available = next(
                (n for n, p in self.providers.items() if p.usable),
                None,
            )
            if available is None:
                raise ConfigError(
                    "没有可用的 LLM provider。请在 config.yaml 配置 API Key "
                    "（如 DEEPSEEK_API_KEY 环境变量），或启动本地 Ollama。"
                )
            cfg = TaskConfig(provider=available)

        if profile:
            mapping = self.profiles.get(profile)
            if mapping is None:
                raise ConfigError(
                    f"未定义的 profile: {profile}（可用: {', '.join(self.profiles) or '无'}）"
                )
            if name in mapping:
                cfg = cfg.model_copy(update={"provider": mapping[name]})

        if cfg.provider not in self.providers:
            raise ConfigError(f"任务 {name} 指定了未定义的 provider: {cfg.provider}")
        return cfg

    def resolve_model(self, task: TaskConfig) -> str:
        provider = self.providers[task.provider]
        return provider.models.get(task.model, task.model)


class ConfigError(RuntimeError):
    pass


DEFAULT_CONFIG: dict[str, Any] = {
    "providers": {
        "deepseek": {
            "type": "openai_compat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "${DEEPSEEK_API_KEY}",
            "models": {"chat": "deepseek-chat", "reason": "deepseek-reasoner"},
            "price_per_1m": {"input": 0.14, "output": 0.28},
        },
        "ollama": {
            "type": "ollama",
            "base_url": "http://localhost:11434",
            "models": {"chat": "qwen3:14b"},
        },
    },
    "tasks": {
        "map": {"provider": "deepseek", "model": "chat", "temperature": 0.1},
        "reduce": {"provider": "deepseek", "model": "chat", "temperature": 0.3},
        "qa": {"provider": "deepseek", "model": "chat", "temperature": 0.2},
    },    "profiles": {
        "local": {"map": "ollama", "reduce": "ollama", "qa": "ollama"},
        "hybrid": {"map": "ollama", "reduce": "deepseek", "qa": "deepseek"},
    },
}


def find_config_file(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    project_root = Path(__file__).resolve().parents[2]
    for name in ("config.yaml", "config.yml"):
        candidate = project_root / name
        if candidate.exists():
            return candidate
    return None


def load_config(path: str | Path | None = None) -> Config:
    """加载配置。找不到配置文件时使用内置默认值，保证开箱可用。"""
    cfg_file = find_config_file(path)
    if cfg_file is None:
        raw = DEFAULT_CONFIG
    else:
        with open(cfg_file, encoding=ENCODING) as f:
            raw = yaml.safe_load(f) or {}
        # 用户配置与默认值浅合并，避免用户漏写 providers 就整体不可用
        for key, value in DEFAULT_CONFIG.items():
            if key not in raw:
                raw[key] = value

    return Config.model_validate(_expand_env(raw))
