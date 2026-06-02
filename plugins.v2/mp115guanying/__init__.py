import base64
import hashlib
import json
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlencode, urljoin, urlsplit, urlunsplit

import requests
from requests.cookies import RequestsCookieJar

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, MediaType

try:
    from app.helper.subscribe import SubscribeHelper
except ModuleNotFoundError:
    SubscribeHelper = None


class _SearchDomainUnavailable(RuntimeError):
    pass


class MP115guanying(_PluginBase):
    plugin_name = "高端玩家115云下载接管"
    plugin_desc = "订阅下载前搜索非公开搜索页，成功提交到 115 离线任务后拦截原下载，失败自动回落 MoviePilot 正常流程。"
    plugin_icon = ""
    plugin_version = "1.0.15"
    plugin_author = "Codex"
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    plugin_config_prefix = "mp115guanying_"
    plugin_order = 6
    auth_level = 1

    _DEFAULT_SEARCH_URL = "https://www.xn--kivn76b41nnhi.com/search?q={keyword}&type=&mode=1"
    _DEFAULT_SEARCH_LOGIN_URL = "https://www.xn--kivn76b41nnhi.com/user/login"
    _DEFAULT_SEARCH_PRIMARY_DOMAIN = "https://www.xn--kivn76b41nnhi.com"
    _DEFAULT_SEARCH_DOMAINS = "\n".join([
        "https://www.xn--kivn76b41nnhi.com",
        "https://www.xn--wcv59z.com",
        "https://www.xn--10vr61a3xc5x3b.com",
        "https://www.xn--74qz10cqsltibh40akss.com",
        "https://www.xn--vcsx1ip8b8w4i.com",
    ])
    _DEFAULT_SEARCH_USERNAME = ""
    _DEFAULT_SEARCH_PASSWORD = ""

    _enabled = False
    _only_subscribe = True
    _cancel_mp_download = True
    _mark_subscribe_done = True
    _dry_run = False
    _notify = True
    _timeout = 15
    _search_url = _DEFAULT_SEARCH_URL
    _search_method = "GET"
    _search_primary_domain = _DEFAULT_SEARCH_PRIMARY_DOMAIN
    _search_domains = _DEFAULT_SEARCH_DOMAINS
    _post_field = "q"
    _search_headers = ""
    _search_cookie = ""
    _search_login_url = _DEFAULT_SEARCH_LOGIN_URL
    _search_username = _DEFAULT_SEARCH_USERNAME
    _search_password = _DEFAULT_SEARCH_PASSWORD
    _search_proxy = ""
    _resolve_overrides = ""
    _query_template = "{title}"
    _results_path = "data"
    _magnet_path = "magnet"
    _title_path = "title"
    _seeders_path = "seeders"
    _min_seeders = 0
    _min_score = 0.45
    _max_candidates = 10
    _accept_untitled = False
    _require_chinese_subtitle = True
    _detail_page_enabled = True
    _detail_max_pages = 5
    _fallback_subscribe_enabled = False
    _fallback_interval_minutes = 60
    _fallback_scan_state = "R"
    _fallback_max_subscribes = 5
    _recent_submit_ttl = 600
    _priority_keywords = ""
    _reject_keywords = ""
    _cookie = ""
    _cookies_file = ""
    _wp_path_id = ""
    _savepath = ""
    _recent_guard_lock = threading.RLock()
    _recent_submissions_global: Dict[str, Any] = {}

    _magnet_regex = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]{32,40}[^\"'<>\\\s]*", re.I)
    _browser_verify_json_regex = re.compile(r"const\s+json\s*=\s*(\{.*?\})\s*;\s*const\s+jss\s*=", re.S)
    _btih_hash_regex = re.compile(
        r"(?:btih|info[_-]?hash|torrent[_-]?hash|magnet[_-]?hash|hash)[\"'\s:=]+"
        r"([A-Fa-f0-9]{40}|[A-Z2-7]{32})",
        re.I,
    )
    _base64_candidate_regex = re.compile(r"[A-Za-z0-9+/=_-]{24,}")

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._only_subscribe = bool(config.get("only_subscribe", True))
        self._cancel_mp_download = bool(config.get("cancel_mp_download", True))
        self._mark_subscribe_done = bool(config.get("mark_subscribe_done", True))
        self._dry_run = bool(config.get("dry_run", False))
        self._notify = bool(config.get("notify", True))
        self._timeout = self._to_int(config.get("timeout"), 15, 3, 120)
        self._search_url = (config.get("search_url") or self._DEFAULT_SEARCH_URL).strip()
        self._search_method = (config.get("search_method") or "GET").strip().upper()
        self._search_primary_domain = (
            config.get("search_primary_domain") or self._DEFAULT_SEARCH_PRIMARY_DOMAIN
        ).strip()
        self._search_domains = (config.get("search_domains") or self._DEFAULT_SEARCH_DOMAINS).strip()
        self._post_field = (config.get("post_field") or "q").strip()
        self._search_headers = config.get("search_headers") or ""
        self._search_cookie = (config.get("search_cookie") or "").strip()
        self._search_login_url = (config.get("search_login_url") or self._DEFAULT_SEARCH_LOGIN_URL).strip()
        username_value = config.get("search_username")
        password_value = config.get("search_password")
        self._search_username = (
            self._DEFAULT_SEARCH_USERNAME if username_value is None else str(username_value)
        ).strip()
        self._search_password = (
            self._DEFAULT_SEARCH_PASSWORD if password_value is None else str(password_value)
        ).strip()
        self._search_proxy = (config.get("search_proxy") or "").strip()
        self._resolve_overrides = config.get("resolve_overrides") or ""
        self._query_template = (config.get("query_template") or "{title}").strip()
        self._results_path = (config.get("results_path") or "data").strip()
        self._magnet_path = (config.get("magnet_path") or "magnet").strip()
        self._title_path = (config.get("title_path") or "title").strip()
        self._seeders_path = (config.get("seeders_path") or "seeders").strip()
        self._min_seeders = self._to_int(config.get("min_seeders"), 0, 0, 100000)
        self._min_score = self._to_float(config.get("min_score"), 0.45, 0, 1)
        self._max_candidates = self._to_int(config.get("max_candidates"), 10, 1, 50)
        self._accept_untitled = bool(config.get("accept_untitled", False))
        self._require_chinese_subtitle = bool(config.get("require_chinese_subtitle", True))
        self._detail_page_enabled = bool(config.get("detail_page_enabled", True))
        self._detail_max_pages = self._to_int(config.get("detail_max_pages"), 5, 0, 30)
        self._fallback_subscribe_enabled = bool(config.get("fallback_subscribe_enabled", False))
        self._fallback_interval_minutes = self._to_int(config.get("fallback_interval_minutes"), 60, 5, 1440)
        self._fallback_scan_state = (config.get("fallback_scan_state") or "R").strip().upper()
        if self._fallback_scan_state not in ("R", "N", "ALL"):
            self._fallback_scan_state = "R"
        self._fallback_max_subscribes = self._to_int(config.get("fallback_max_subscribes"), 5, 1, 100)
        self._priority_keywords = config.get("priority_keywords") or self._default_priority_keywords()
        self._reject_keywords = config.get("reject_keywords") or self._default_reject_keywords()
        self._cookie = (config.get("cookie") or "").strip()
        self._cookies_file = (config.get("cookies_file") or "").strip()
        self._wp_path_id = str(config.get("wp_path_id") or "").strip()
        self._savepath = (config.get("savepath") or "").strip()
        logger.info(f"[MP115guanying] 插件初始化完成: version={self.plugin_version}")

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/mp115guanying_fallback",
            "event": EventType.PluginAction,
            "desc": "执行高端玩家115订阅兜底扫描",
            "category": "订阅",
            "data": {"action": "mp115guanying_fallback"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/health", "endpoint": self.api_health, "methods": ["GET"], "summary": "检查插件配置状态"},
            {"path": "/search", "endpoint": self.api_search, "methods": ["GET"], "summary": "测试磁力搜索"},
            {"path": "/submit", "endpoint": self.api_submit, "methods": ["POST"], "summary": "测试提交 115 离线任务"},
            {"path": "/fallback_scan", "endpoint": self.api_fallback_scan, "methods": ["POST"], "summary": "兜底扫描订阅并提交 115"},
            {"path": "/fallback_subscribe", "endpoint": self.api_fallback_subscribe, "methods": ["POST"], "summary": "按订阅 ID 立即兜底提交 115"},
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._fallback_subscribe_enabled:
            return []
        return [{
            "id": "MP115guanyingFallbackSubscribe",
            "name": "高端玩家115订阅兜底扫描",
            "trigger": "interval",
            "func": self.fallback_subscribe_scan,
            "kwargs": {
                "minutes": self._fallback_interval_minutes,
                "next_run_time": datetime.now() + timedelta(seconds=15),
            },
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "only_subscribe", "label": "仅处理订阅"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "cancel_mp_download", "label": "成功后拦截原下载"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "dry_run", "label": "演练模式"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "mark_subscribe_done", "label": "成功后更新订阅状态"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "notify", "label": "发送站内通知"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "timeout", "label": "请求超时(秒)", "type": "number"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "search_url",
                                    "label": "搜索页/首页 URL",
                                    "placeholder": "https://www.xn--kivn76b41nnhi.com/search?q={keyword}&type=&mode=1"
                                }}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSelect", "props": {"model": "search_method", "label": "搜索方法", "items": ["GET", "POST"]}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSelect", "props": {
                                    "model": "search_primary_domain",
                                    "label": "常用镜像域名",
                                    "items": MP115guanying._default_search_domain_items()
                                }}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "search_domains",
                                    "label": "搜索站点镜像域名池(每行一个)",
                                    "rows": 5,
                                    "placeholder": "https://www.xn--kivn76b41nnhi.com"
                                }}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "search_login_url",
                                    "label": "搜索站点登录 URL",
                                    "placeholder": "https://www.xn--kivn76b41nnhi.com/user/login"
                                }}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "search_username",
                                    "label": "搜索站点账号",
                                    "placeholder": "填你的搜索站点账号"
                                }}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "search_password",
                                    "label": "搜索站点密码",
                                    "type": "password"
                                }}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "search_headers",
                                    "label": "搜索页请求头(JSON 或每行 Key: Value)",
                                    "rows": 3
                                }}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "post_field",
                                    "label": "POST 搜索字段名",
                                    "placeholder": "keyword"
                                }}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "search_proxy",
                                    "label": "搜索站点代理 URL(可空)",
                                    "placeholder": "http://192.168.31.18:7890 或 socks5h://192.168.31.18:7890"
                                }}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "resolve_overrides",
                                    "label": "DNS/IP 覆盖(每行 host=ip，可空)",
                                    "rows": 2,
                                    "placeholder": "www.example.test=192.168.31.10"
                                }}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "search_cookie",
                                    "label": "搜索站点 Cookie",
                                    "rows": 3,
                                    "placeholder": "只填搜索站点自己的 Cookie；115 Cookie 填在下面"
                                }}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {
                                    "model": "query_template",
                                    "label": "搜索关键词模板",
                                    "placeholder": "{title}"
                                }}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "results_path", "label": "结果列表路径"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "magnet_path", "label": "JSON 磁链字段路径"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "title_path", "label": "JSON 标题字段路径"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "seeders_path", "label": "JSON 做种数字段路径"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [
                                {"component": "VTextField", "props": {"model": "min_seeders", "label": "最少做种", "type": "number"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [
                                {"component": "VTextField", "props": {"model": "min_score", "label": "匹配阈值", "type": "number", "step": "0.05"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 2}, "content": [
                                {"component": "VTextField", "props": {"model": "max_candidates", "label": "候选上限", "type": "number"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "accept_untitled", "label": "允许无标题结果"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "require_chinese_subtitle", "label": "必须包含中文字幕"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "detail_page_enabled", "label": "进入详情页抓磁链"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "detail_max_pages", "label": "最多详情页数", "type": "number"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSwitch", "props": {"model": "fallback_subscribe_enabled", "label": "启用订阅兜底扫描"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "fallback_interval_minutes", "label": "兜底扫描间隔(分钟)", "type": "number"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSelect", "props": {
                                    "model": "fallback_scan_state",
                                    "label": "兜底扫描订阅状态",
                                    "items": [
                                        {"title": "已搜索未完成(R)", "value": "R"},
                                        {"title": "未搜索(N)", "value": "N"},
                                        {"title": "全部未完成", "value": "ALL"},
                                    ]
                                }}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "fallback_max_subscribes", "label": "每次最多扫描订阅数", "type": "number"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "priority_keywords",
                                    "label": "优先关键词(每行 关键词:分数)",
                                    "rows": 8
                                }}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "reject_keywords",
                                    "label": "排除关键词(每行一个)",
                                    "rows": 8
                                }}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 12}, "content": [
                                {"component": "VTextarea", "props": {
                                    "model": "cookie",
                                    "label": "115 Cookie",
                                    "rows": 3,
                                    "placeholder": "浏览器登录 115.com 后复制 Cookie；也可只填下面的 Cookie 文件路径"
                                }}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "cookies_file", "label": "115 Cookie 文件路径"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "wp_path_id", "label": "115 目标目录 ID"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "savepath", "label": "115 保存路径(可空)"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "默认按媒体标题搜索影片页，再从影片详情页筛选资源条目，最后进入资源磁力页提取真实 magnet。找不到磁链或提交 115 失败时，插件不会拦截 MoviePilot 原流程。"
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "only_subscribe": True,
            "cancel_mp_download": True,
            "mark_subscribe_done": True,
            "dry_run": False,
            "notify": True,
            "timeout": 15,
            "search_url": MP115guanying._DEFAULT_SEARCH_URL,
            "search_method": "GET",
            "search_primary_domain": MP115guanying._DEFAULT_SEARCH_PRIMARY_DOMAIN,
            "search_domains": MP115guanying._DEFAULT_SEARCH_DOMAINS,
            "post_field": "q",
            "search_headers": "",
            "search_cookie": "",
            "search_login_url": MP115guanying._DEFAULT_SEARCH_LOGIN_URL,
            "search_username": MP115guanying._DEFAULT_SEARCH_USERNAME,
            "search_password": MP115guanying._DEFAULT_SEARCH_PASSWORD,
            "search_proxy": "",
            "resolve_overrides": "",
            "query_template": "{title}",
            "results_path": "data",
            "magnet_path": "magnet",
            "title_path": "title",
            "seeders_path": "seeders",
            "min_seeders": 0,
            "min_score": 0.45,
            "max_candidates": 10,
            "accept_untitled": False,
            "require_chinese_subtitle": True,
            "detail_page_enabled": True,
            "detail_max_pages": 5,
            "fallback_subscribe_enabled": False,
            "fallback_interval_minutes": 60,
            "fallback_scan_state": "R",
            "fallback_max_subscribes": 5,
            "priority_keywords": MP115guanying._default_priority_keywords(),
            "reject_keywords": MP115guanying._default_reject_keywords(),
            "cookie": "",
            "cookies_file": "",
            "wp_path_id": "",
            "savepath": "",
        }

    def get_page(self) -> List[dict]:
        records = self.get_data("latest_records") or []
        content = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "可用 /plugin/MP115guanying/health、/search?keyword=xxx、/fallback_scan 和 /fallback_subscribe 测试配置。成功记录会写入插件数据 latest_records。"
                },
            },
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VTextField", "props": {
                            "model": "subscribe_id",
                            "label": "订阅 ID",
                            "type": "number",
                            "placeholder": "例如 123"
                        }}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VTextField", "props": {
                            "model": "keyword",
                            "label": "关键词兜底(可空)",
                            "placeholder": "不填订阅 ID 时使用"
                        }}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                        {"component": "VBtn", "props": {"color": "primary", "variant": "tonal"}, "text": "按订阅 ID/关键词提交 115", "events": {
                            "click": {
                                "api": f"plugin/{self.__class__.__name__}/fallback_subscribe?apikey={settings.API_TOKEN}",
                                "method": "post",
                                "params": {"subscribe_id": "{{subscribe_id}}", "keyword": "{{keyword}}"},
                            }
                        }}
                    ]},
                ],
            },
            {
                "component": "VBtn",
                "props": {"color": "secondary", "variant": "tonal"},
                "text": "立即执行订阅兜底扫描",
                "events": {
                    "click": {
                        "api": f"plugin/{self.__class__.__name__}/fallback_scan?apikey={settings.API_TOKEN}",
                        "method": "post",
                        "params": {"state": self._fallback_scan_state, "limit": self._fallback_max_subscribes},
                    }
                },
            },
        ]
        if records:
            content.append({"component": "VDivider", "props": {"class": "my-4"}})
            content.append({"component": "div", "props": {"class": "text-subtitle-1 mb-2"}, "text": "最近提交记录"})
            for record in records[:10]:
                selected = record.get("selected") or {}
                content.append({
                    "component": "VAlert",
                    "props": {"type": "success", "variant": "tonal", "class": "mb-2"},
                    "text": f"{record.get('time', '')} · {selected.get('title') or record.get('keyword') or ''} · {record.get('message') or ''}",
                })
        return content

    def stop_service(self):
        pass

    def api_health(self) -> Dict[str, Any]:
        cookie_ok, cookie_msg = self._check_cookie()
        return {
            "success": self._enabled,
            "data": {
                "enabled": self._enabled,
                "version": self.plugin_version,
                "search_url_configured": bool(self._search_url),
                "search_primary_domain": self._normalize_search_origin(self._search_primary_domain),
                "search_domain_count": len(self._search_domain_pool()),
                "search_login_url_configured": bool(self._search_login_url),
                "search_username_configured": bool(self._search_username),
                "search_proxy_configured": bool(self._search_proxy),
                "cookie_configured": bool(self._cookie or self._cookies_file),
                "cookie_ok": cookie_ok,
                "cookie_message": cookie_msg,
                "require_chinese_subtitle": self._require_chinese_subtitle,
                "dry_run": self._dry_run,
                "fallback_subscribe_enabled": self._fallback_subscribe_enabled,
                "fallback_interval_minutes": self._fallback_interval_minutes,
                "fallback_scan_state": self._fallback_scan_state,
                "fallback_max_subscribes": self._fallback_max_subscribes,
            },
        }

    def api_search(self, keyword: str = "") -> Dict[str, Any]:
        keyword = (keyword or "").strip()
        if not keyword:
            return {"success": False, "message": "keyword 不能为空", "data": []}
        results, message = self._search_magnets(keyword=keyword, target_title=keyword)
        return {"success": bool(results), "message": message, "data": results}

    def api_submit(self, magnet: str = "") -> Dict[str, Any]:
        magnet = (magnet or "").strip()
        if not magnet:
            return {"success": False, "message": "magnet 不能为空"}
        if self._dry_run:
            return {"success": True, "message": "演练模式：未提交 115", "data": {"magnet": magnet}}
        ok, message, result = self._submit_115(magnet)
        return {"success": ok, "message": message, "data": result}

    def api_fallback_scan(self, state: str = "", limit: Any = None) -> Dict[str, Any]:
        state = (state or self._fallback_scan_state or "R").strip().upper()
        if state not in ("R", "N", "ALL"):
            state = "R"
        max_count = self._to_int(limit, self._fallback_max_subscribes, 1, 100)
        return self.fallback_subscribe_scan(state=state, limit=max_count, manual=True)

    def api_fallback_subscribe(self, subscribe_id: Any = None, keyword: str = "") -> Dict[str, Any]:
        subscribe_id = self._to_int(subscribe_id, 0, 0, 999999999)
        keyword = (keyword or "").strip()
        if subscribe_id <= 0 and not keyword:
            return {"success": False, "message": "subscribe_id 或 keyword 至少填写一个", "data": {}}
        if subscribe_id > 0:
            return self._fallback_submit_subscribe_id(subscribe_id, manual=True)
        return self._fallback_submit_keyword(keyword, manual=True)

    def fallback_subscribe_scan(
        self,
        state: str = "",
        limit: Any = None,
        manual: bool = False,
    ) -> Dict[str, Any]:
        state = (state or self._fallback_scan_state or "R").strip().upper()
        if state not in ("R", "N", "ALL"):
            state = "R"
        max_count = self._to_int(limit, self._fallback_max_subscribes, 1, 100)
        result = {"checked": 0, "submitted": 0, "skipped": 0, "failed": 0, "items": []}
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": result}
        if not self._search_url:
            return {"success": False, "message": "未配置搜索 URL", "data": result}
        if not self._cookie and not self._cookies_file and not self._dry_run:
            return {"success": False, "message": "未配置 115 Cookie", "data": result}
        try:
            subscribe_oper = SubscribeOper()
            scan_state = "N,R,P" if state == "ALL" else state
            subscribes = subscribe_oper.list(scan_state) or []
        except Exception as exc:
            return {"success": False, "message": f"读取订阅列表失败: {exc}", "data": result}
        for subscribe in subscribes[:max_count]:
            result["checked"] += 1
            item = self._fallback_submit_subscribe(subscribe, manual=manual)
            result["items"].append(item)
            if item.get("success"):
                result["submitted"] += 1
            elif item.get("skipped"):
                result["skipped"] += 1
            else:
                result["failed"] += 1
        message = f"订阅兜底扫描完成: 检查 {result['checked']}，提交 {result['submitted']}，跳过 {result['skipped']}，失败 {result['failed']}"
        logger.info(f"[MP115guanying] {message}")
        return {"success": result["submitted"] > 0, "message": message, "data": result}

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        if not event:
            return
        event_data = event.event_data or {}
        if not isinstance(event_data, dict):
            return
        action = event_data.get("action")
        if action == "mp115guanying_fallback":
            self.fallback_subscribe_scan(manual=True)
        elif action == "mp115guanying_fallback_subscribe":
            self.api_fallback_subscribe(
                subscribe_id=event_data.get("subscribe_id"),
                keyword=event_data.get("keyword") or "",
            )

    def _fallback_submit_subscribe_id(self, subscribe_id: int, manual: bool = False) -> Dict[str, Any]:
        try:
            subscribe = SubscribeOper().get(int(subscribe_id))
        except Exception as exc:
            return {"success": False, "subscribe_id": subscribe_id, "message": f"读取订阅失败: {exc}"}
        if not subscribe:
            return {"success": False, "subscribe_id": subscribe_id, "message": "订阅不存在"}
        return self._fallback_submit_subscribe(subscribe, manual=manual)

    def _fallback_submit_subscribe(self, subscribe: Any, manual: bool = False) -> Dict[str, Any]:
        subscribe_id = getattr(subscribe, "id", None)
        context = self._search_context_from_subscribe(subscribe)
        title = context.get("title") or context.get("media_title") or getattr(subscribe, "name", "") or ""
        if self._is_tv_context(context) and not self._target_season_number(context):
            return {
                "success": False,
                "skipped": True,
                "subscribe_id": subscribe_id,
                "title": title,
                "message": "电视剧未识别到目标季",
            }
        origin = self._subscribe_origin(subscribe)
        lookup_keys = self._submission_guard_lookup_keys(context, origin)
        if self._is_recent_submission(lookup_keys):
            return {
                "success": False,
                "skipped": True,
                "subscribe_id": subscribe_id,
                "title": title,
                "message": "近期已提交同一订阅目标",
            }
        keyword = self._render_template(self._query_template, context).strip() or title
        if not keyword:
            return {
                "success": False,
                "skipped": True,
                "subscribe_id": subscribe_id,
                "title": title,
                "message": "搜索关键词为空",
            }
        if not self._claim_recent_submission(lookup_keys, ttl=120):
            return {
                "success": False,
                "skipped": True,
                "subscribe_id": subscribe_id,
                "title": title,
                "message": "近期已提交同一订阅目标",
            }
        logger.info(f"[MP115guanying] 订阅兜底开始搜索资源站: id={subscribe_id}, keyword={keyword}")
        candidates, search_message = self._search_magnets(
            keyword=keyword,
            target_title=context.get("target_title") or keyword,
            template_values=context,
        )
        if not candidates:
            logger.info(f"[MP115guanying] 订阅兜底未找到可用磁链: id={subscribe_id}, {search_message}")
            self._forget_recent_submission(lookup_keys)
            return {
                "success": False,
                "subscribe_id": subscribe_id,
                "title": title,
                "message": search_message,
            }
        selected = candidates[0]
        magnet = selected.get("magnet")
        resource_title = selected.get("title") or title or keyword
        magnet_key = self._magnet_submission_key(magnet)
        if magnet_key and not self._claim_recent_submission(magnet_key, ttl=120):
            store_keys = self._submission_guard_store_keys(context, origin) + [magnet_key]
            self._remember_recent_submission(
                self._submission_success_keys(store_keys),
                context,
                selected,
            )
            return {
                "success": False,
                "skipped": True,
                "subscribe_id": subscribe_id,
                "title": resource_title,
                "message": "近期已提交同一磁链",
            }
        if self._dry_run:
            ok, submit_message, submit_result = True, "演练模式：未提交 115", {}
        else:
            ok, submit_message, submit_result = self._submit_115(magnet)
        if not ok:
            logger.warning(f"[MP115guanying] 订阅兜底 115 提交失败: id={subscribe_id}, {submit_message}")
            self._forget_recent_submission(lookup_keys)
            self._forget_recent_submission(magnet_key)
            if manual:
                self._notify_message("高端玩家115订阅兜底失败", f"{resource_title}\n{submit_message}")
            return {
                "success": False,
                "subscribe_id": subscribe_id,
                "title": resource_title,
                "message": submit_message,
            }
        logger.info(f"[MP115guanying] 订阅兜底已提交 115 离线任务: id={subscribe_id}, title={resource_title}")
        store_keys = self._submission_guard_store_keys(context, origin) + ([magnet_key] if magnet_key else [])
        self._remember_recent_submission(
            self._submission_success_keys(store_keys),
            context,
            selected,
        )
        self._save_record(context, selected, submit_message, submit_result)
        self._notify_message("高端玩家115订阅兜底成功", f"{resource_title}\n{submit_message}")
        if self._mark_subscribe_done:
            self._update_subscribe_state_from_subscribe(subscribe)
        return {
            "success": True,
            "subscribe_id": subscribe_id,
            "title": resource_title,
            "message": submit_message,
            "selected": selected,
            "result": submit_result,
        }

    def _fallback_submit_keyword(self, keyword: str, manual: bool = False) -> Dict[str, Any]:
        keyword = (keyword or "").strip()
        context = {
            "keyword": keyword,
            "raw_keyword": keyword,
            "title": keyword,
            "media_title": keyword,
            "search_title": keyword,
            "target_title": keyword,
            "torrent_title": "",
            "year": "",
            "media_type": "",
            "is_tv": "",
            "season": "",
            "episode": "",
            "tmdbid": "",
            "imdbid": "",
            "doubanid": "",
        }
        if self._is_recent_submission(self._submission_guard_lookup_keys(context, "")):
            return {"success": False, "skipped": True, "title": keyword, "message": "近期已提交同一关键词"}
        logger.info(f"[MP115guanying] 关键词兜底开始搜索资源站: {keyword}")
        candidates, search_message = self._search_magnets(
            keyword=keyword,
            target_title=keyword,
            template_values=context,
        )
        if not candidates:
            return {"success": False, "title": keyword, "message": search_message}
        selected = candidates[0]
        title = selected.get("title") or keyword
        magnet_key = self._magnet_submission_key(selected.get("magnet"))
        if magnet_key and not self._claim_recent_submission(magnet_key, ttl=120):
            store_keys = self._submission_guard_store_keys(context, "") + [magnet_key]
            self._remember_recent_submission(
                self._submission_success_keys(store_keys),
                context,
                selected,
            )
            return {"success": False, "skipped": True, "title": title, "message": "近期已提交同一磁链"}
        if self._dry_run:
            ok, submit_message, submit_result = True, "演练模式：未提交 115", {}
        else:
            ok, submit_message, submit_result = self._submit_115(selected.get("magnet"))
        if not ok:
            self._forget_recent_submission(magnet_key)
            if manual:
                self._notify_message("高端玩家115关键词兜底失败", f"{title}\n{submit_message}")
            return {"success": False, "title": title, "message": submit_message}
        store_keys = self._submission_guard_store_keys(context, "") + ([magnet_key] if magnet_key else [])
        self._remember_recent_submission(self._submission_success_keys(store_keys), context, selected)
        self._save_record(context, selected, submit_message, submit_result)
        self._notify_message("高端玩家115关键词兜底成功", f"{title}\n{submit_message}")
        return {"success": True, "title": title, "message": submit_message, "selected": selected, "result": submit_result}

    @eventmanager.register(ChainEventType.ResourceDownload, priority=1)
    def on_resource_download(self, event: Event):
        if not self._enabled:
            return
        event_data = event.event_data
        if not event_data or getattr(event_data, "cancel", False):
            return
        origin = getattr(event_data, "origin", "") or ""
        if self._only_subscribe and not str(origin).startswith("Subscribe|"):
            return
        context = getattr(event_data, "context", None)
        if not context:
            return
        if not self._search_url:
            logger.warning("[MP115guanying] 未配置搜索 URL，跳过")
            return
        if not self._cookie and not self._cookies_file and not self._dry_run:
            logger.warning("[MP115guanying] 未配置 115 Cookie，跳过")
            return

        search_context = self._build_search_context(event_data)
        submission_keys = self._submission_guard_lookup_keys(search_context, origin)
        if self._is_recent_submission(submission_keys):
            logger.info(f"[MP115guanying] 近期已提交同一订阅目标，跳过重复提交: {search_context.get('title')}")
            if self._cancel_mp_download:
                event_data.cancel = True
                event_data.source = self.plugin_name
                event_data.reason = "近期已提交同一订阅目标到 115，拦截重复下载"
            return
        keyword = self._render_template(self._query_template, search_context).strip()
        if not keyword:
            logger.warning("[MP115guanying] 搜索关键词为空，跳过")
            return
        if self._is_tv_context(search_context) and not self._target_season_number(search_context):
            logger.info(f"[MP115guanying] 电视剧未识别到目标季，跳过接管并回落 MoviePilot 原流程: {keyword}")
            return
        if not self._claim_recent_submission(submission_keys, ttl=120):
            logger.info(f"[MP115guanying] 近期已提交同一订阅目标，跳过重复提交: {search_context.get('title')}")
            if self._cancel_mp_download:
                event_data.cancel = True
                event_data.source = self.plugin_name
                event_data.reason = "近期已提交同一订阅目标到 115，拦截重复下载"
            return

        logger.info(f"[MP115guanying] 开始搜索非公开搜索页: {keyword}")
        candidates, search_message = self._search_magnets(
            keyword=keyword,
            target_title=search_context.get("target_title") or keyword,
            template_values=search_context,
        )
        if not candidates:
            if self._has_recent_success_context(search_context):
                logger.info(f"[MP115guanying] 近期已提交同一订阅目标，停止后续搜索: {search_context.get('title')}")
                if self._cancel_mp_download:
                    event_data.cancel = True
                    event_data.source = self.plugin_name
                    event_data.reason = "近期已提交同一订阅目标到 115，拦截重复下载"
                return
            logger.info(f"[MP115guanying] 未找到可用磁链，回落 MoviePilot 原流程: {search_message}")
            self._forget_recent_submission(submission_keys)
            return

        selected = candidates[0]
        magnet = selected.get("magnet")
        title = selected.get("title") or keyword
        magnet_key = self._magnet_submission_key(magnet)
        if magnet_key and not self._claim_recent_submission(magnet_key, ttl=120):
            logger.info(f"[MP115guanying] 近期已提交同一磁链，跳过重复提交: {title}")
            store_keys = self._submission_guard_store_keys(search_context, origin) + [magnet_key]
            self._remember_recent_submission(
                self._submission_success_keys(store_keys),
                search_context,
                selected,
            )
            if self._cancel_mp_download:
                event_data.cancel = True
                event_data.source = self.plugin_name
                event_data.reason = "近期已提交同一磁链到 115，拦截重复下载"
            return
        if self._dry_run:
            ok, submit_message, submit_result = True, "演练模式：未提交 115", {}
        else:
            ok, submit_message, submit_result = self._submit_115(magnet)

        if not ok:
            logger.warning(f"[MP115guanying] 115 提交失败，回落 MoviePilot 原流程: {submit_message}")
            self._forget_recent_submission(submission_keys)
            self._forget_recent_submission(magnet_key)
            self._notify_message("高端玩家115云下载接管失败", f"{title}\n{submit_message}")
            return

        logger.info(f"[MP115guanying] 已提交 115 离线任务: {title}")
        store_keys = self._submission_guard_store_keys(search_context, origin) + ([magnet_key] if magnet_key else [])
        self._remember_recent_submission(
            self._submission_success_keys(store_keys),
            search_context,
            selected,
        )
        self._save_record(search_context, selected, submit_message, submit_result)
        self._notify_message("高端玩家115云下载接管成功", f"{title}\n{submit_message}")
        if self._mark_subscribe_done:
            self._update_subscribe_state(origin, context, event_data)
        if self._cancel_mp_download:
            event_data.cancel = True
            event_data.source = self.plugin_name
            event_data.reason = "已提交到 115 离线任务，拦截 MoviePilot 原下载"

    def _search_magnets(
        self,
        keyword: str,
        target_title: str,
        template_values: Optional[Dict[str, Any]] = None,
        allow_keyword_fallback: bool = True,
    ) -> Tuple[List[Dict[str, Any]], str]:
        search_templates = self._search_url_templates_for_domain_pool()
        if not search_templates:
            search_templates = [self._search_url]
        messages = []
        for index, search_template in enumerate(search_templates, 1):
            preview_values = self._search_template_values(keyword, template_values)
            preview_url = self._render_template(search_template, preview_values)
            origin = self._origin_url(preview_url) or search_template
            if len(search_templates) > 1:
                logger.info(f"[MP115guanying] 尝试搜索镜像域名 {index}/{len(search_templates)}: {origin}")
            try:
                candidates, message = self._search_magnets_once(
                    keyword=keyword,
                    target_title=target_title,
                    template_values=template_values,
                    allow_keyword_fallback=allow_keyword_fallback,
                    search_url_template=search_template,
                )
            except _SearchDomainUnavailable as exc:
                messages.append(f"{origin}: {exc}")
                logger.warning(f"[MP115guanying] 搜索镜像域名不可用，切换下一个: {origin} - {exc}")
                continue
            if candidates:
                if len(search_templates) > 1:
                    logger.info(f"[MP115guanying] 搜索镜像域名命中: {origin}")
                return candidates, f"{origin}: {message}" if len(search_templates) > 1 else message
            return [], message
        return [], f"所有搜索镜像域名均不可用: {'; '.join(messages) or '无可用域名'}"

    def _search_url_templates_for_domain_pool(self) -> List[str]:
        base_template = self._search_url or self._DEFAULT_SEARCH_URL
        domains = self._search_domain_pool()
        if not domains:
            return [base_template]
        return self._dedupe_title_values([
            self._replace_search_url_origin(base_template, domain)
            for domain in domains
        ])

    def _search_domain_pool(self) -> List[str]:
        domains = []
        primary = self._normalize_search_origin(self._search_primary_domain)
        if primary:
            domains.append(primary)
        domains.extend(self._parse_search_domains(self._search_domains))
        search_origin = self._normalize_search_origin(self._origin_url(self._search_url))
        if search_origin:
            domains.append(search_origin)
        return self._dedupe_title_values([domain for domain in domains if domain])

    @classmethod
    def _parse_search_domains(cls, value: Any) -> List[str]:
        domains = []
        for item in re.split(r"[\n,，\s]+", str(value or "")):
            origin = cls._normalize_search_origin(item)
            if origin:
                domains.append(origin)
        return cls._dedupe_title_values(domains)

    @staticmethod
    def _normalize_search_origin(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if "://" not in text:
            text = f"https://{text}"
        parsed = urlsplit(text)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @staticmethod
    def _replace_search_url_origin(url_template: str, origin: str) -> str:
        origin_parts = urlsplit(origin or "")
        if not origin_parts.scheme or not origin_parts.netloc:
            return url_template
        parsed = urlsplit(url_template or "")
        if parsed.scheme and parsed.netloc:
            return urlunsplit((
                origin_parts.scheme,
                origin_parts.netloc,
                parsed.path,
                parsed.query,
                parsed.fragment,
            ))
        return urljoin(origin.rstrip("/") + "/", str(url_template or "").lstrip("/"))

    @staticmethod
    def _default_search_domain_items() -> List[Dict[str, str]]:
        return [
            {"title": "星际穿越.com", "value": "https://www.xn--kivn76b41nnhi.com"},
            {"title": "教父.com", "value": "https://www.xn--wcv59z.com"},
            {"title": "盗梦空间.com", "value": "https://www.xn--10vr61a3xc5x3b.com"},
            {"title": "肖申克的救赎.com", "value": "https://www.xn--74qz10cqsltibh40akss.com"},
            {"title": "黑客帝国.com", "value": "https://www.xn--vcsx1ip8b8w4i.com"},
        ]

    @staticmethod
    def _is_search_domain_exception(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return any(marker in text for marker in (
            "connection",
            "connect",
            "timeout",
            "timed out",
            "max retries",
            "name or service",
            "temporary failure",
            "ssl",
            "403",
            "forbidden",
            "未登录",
            "访问受限",
            "搜索入口全部失败",
            "浏览器计算验证",
        ))

    def _search_magnets_once(
        self,
        keyword: str,
        target_title: str,
        template_values: Optional[Dict[str, Any]] = None,
        allow_keyword_fallback: bool = True,
        search_url_template: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        tv_season_pack_only = self._is_tv_context(template_values)
        target_season = self._target_season_number(template_values)
        if tv_season_pack_only and not target_season:
            return [], "电视剧未识别到目标季，回落 MoviePilot 原流程"
        try:
            values = self._search_template_values(keyword, template_values)
            url = self._render_template(search_url_template or self._search_url, values)
            logger.info(f"[MP115guanying] 搜索请求准备: {self._describe_url_network(url)}")
            if self._search_proxy:
                logger.info(f"[MP115guanying] 搜索请求使用代理: {self._mask_proxy_url(self._search_proxy)}")
            headers = self._build_search_headers(url)
            cookies = self._build_search_cookiejar(headers)
            self._ensure_search_site_login(url=url, headers=headers, cookies=cookies)
            response = self._request_search_page(url=url, keyword=keyword, headers=headers, cookies=cookies)
            response.raise_for_status()
        except _SearchDomainUnavailable:
            raise
        except Exception as exc:
            if self._is_search_domain_exception(exc):
                raise _SearchDomainUnavailable(f"搜索请求失败: {exc}") from exc
            if allow_keyword_fallback:
                fallback_message = str(exc)
                for fallback_keyword in self._search_keyword_fallbacks(keyword, template_values):
                    if self._has_recent_success_context(template_values):
                        return [], "近期已提交同一订阅目标，停止降级搜索"
                    logger.info(f"[MP115guanying] 搜索请求失败，尝试降级搜索关键词: {fallback_keyword}")
                    fallback_candidates, fallback_result = self._search_magnets_once(
                        keyword=fallback_keyword,
                        target_title=target_title,
                        template_values=self._fallback_template_values(template_values, fallback_keyword),
                        allow_keyword_fallback=False,
                        search_url_template=search_url_template,
                    )
                    if fallback_candidates:
                        return fallback_candidates, f"搜索请求失败后降级关键词 {fallback_keyword}: {fallback_result}"
                    fallback_message = fallback_result or fallback_message
                return [], f"搜索请求失败且降级关键词均无可用结果: {fallback_message}"
            return [], f"搜索请求失败: {exc}"

        text = response.text or ""
        status_code = getattr(response, "status_code", "")
        final_url = getattr(response, "url", url)
        content_type = (getattr(response, "headers", {}) or {}).get("Content-Type", "")
        logger.info(
            f"[MP115guanying] 搜索页已返回: HTTP {status_code or 'unknown'}, "
            f"内容 {len(text)} 字符, 类型 {content_type or 'unknown'}, URL {final_url}"
        )
        if self._search_site_login_required(text):
            raise _SearchDomainUnavailable(f"搜索页未登录或访问受限: HTTP {status_code or 'unknown'}")
        candidates: List[Dict[str, Any]] = []
        try:
            payload = response.json()
            rows = self._get_by_path(payload, self._results_path) if self._results_path else payload
            if isinstance(rows, dict):
                rows = [rows]
            if isinstance(rows, list):
                for row in rows:
                    candidates.extend(self._candidate_from_row(row))
            else:
                candidates.extend(self._candidate_from_text(text, base_url=final_url))
        except Exception:
            candidates.extend(self._candidate_from_text(text, base_url=final_url))

        if not any(item.get("magnet") or item.get("detail_url") for item in candidates):
            suggest_candidates = self._candidate_from_search_suggest(
                keyword=keyword,
                base_url=final_url,
                headers=headers,
                cookies=cookies,
            )
            if suggest_candidates:
                logger.info(f"[MP115guanying] 搜索联想接口解析: 详情链接 {len(suggest_candidates)} 条")
                candidates.extend(suggest_candidates)

        direct_count = len([item for item in candidates if item.get("magnet")])
        detail_count = len([item for item in candidates if item.get("detail_url")])
        logger.info(f"[MP115guanying] 搜索页解析: 直接磁链 {direct_count} 条，详情链接 {detail_count} 条")
        matched_search_candidate = self._has_matching_search_candidate(
            candidates,
            final_url,
            target_title,
            tv_season_pack_only=tv_season_pack_only,
            target_season=target_season,
        )
        if self._has_recent_success_context(template_values):
            return [], "近期已提交同一订阅目标，停止后续搜索"
        if self._detail_page_enabled and self._detail_max_pages > 0:
            candidates.extend(self._candidate_from_detail_pages(
                items=candidates,
                base_url=final_url,
                headers=headers,
                cookies=cookies,
                target_title=target_title,
                tv_season_pack_only=tv_season_pack_only,
                target_season=target_season,
            ))

        deduped = []
        seen = set()
        for item in candidates:
            magnet = self._normalize_magnet(item.get("magnet"))
            if not magnet or magnet in seen:
                continue
            seen.add(magnet)
            title = (item.get("title") or "").strip()
            quality_title = (item.get("quality_title") or title).strip()
            seeders = self._to_int(item.get("seeders"), 0, 0, 1000000)
            if seeders < self._min_seeders:
                continue
            if self._is_rejected(title):
                continue
            reject_reason = self._title_reject_reason(target_title, title)
            if reject_reason:
                logger.info(f"[MP115guanying] 跳过标题冲突候选: {reject_reason}, title={title}")
                continue
            score = self._score_title(target_title, title)
            quality_score, quality_hits = self._quality_score(quality_title)
            trusted_detail = bool(item.get("trusted_detail"))
            context_score = self._to_float(item.get("context_score"), 0, 0, 1)
            if title:
                if score < self._min_score and not (trusted_detail and context_score >= self._min_score):
                    continue
                if not self._resource_quality_allowed(quality_title):
                    label = item.get("quality_label") or ""
                    suffix = f", quality={label}" if label else ""
                    logger.info(f"[MP115guanying] 跳过未满足字幕/清晰度规则的资源: title={title}{suffix}")
                    continue
                if tv_season_pack_only and not self._tv_season_pack_allowed(quality_title, target_season):
                    logger.info(f"[MP115guanying] 跳过非目标季整季资源: title={title}")
                    continue
            elif not self._accept_untitled or not self._resource_quality_allowed(quality_title):
                continue
            elif tv_season_pack_only:
                continue
            deduped.append({
                "title": title,
                "magnet": magnet,
                "seeders": seeders,
                "score": round(score, 4),
                "context_score": round(context_score, 4),
                "quality_score": quality_score,
                "quality_hits": quality_hits,
                "trusted_detail": trusted_detail,
            })
        deduped.sort(
            key=lambda x: (
                1 if x.get("trusted_detail") else 0,
                x.get("quality_score", 0),
                x.get("score", 0),
                x.get("context_score", 0),
                x.get("seeders", 0),
            ),
            reverse=True,
        )
        if not deduped:
            if matched_search_candidate:
                return [], (
                    f"精确搜索已命中匹配影片/资源入口，但未解析到可用磁链: "
                    f"HTTP {status_code or 'unknown'}, 内容 {len(text)} 字符"
            )
            if allow_keyword_fallback:
                for fallback_keyword in self._search_keyword_fallbacks(keyword, template_values):
                    if self._has_recent_success_context(template_values):
                        return [], "近期已提交同一订阅目标，停止降级搜索"
                    logger.info(f"[MP115guanying] 精确关键词未命中，尝试降级搜索关键词: {fallback_keyword}")
                    fallback_candidates, fallback_message = self._search_magnets_once(
                        keyword=fallback_keyword,
                        target_title=target_title,
                        template_values=self._fallback_template_values(template_values, fallback_keyword),
                        allow_keyword_fallback=False,
                        search_url_template=search_url_template,
                    )
                    if fallback_candidates:
                        return fallback_candidates, f"降级关键词 {fallback_keyword}: {fallback_message}"
            return [], f"页面已返回但未解析到可用磁链: HTTP {status_code or 'unknown'}, 内容 {len(text)} 字符"
        return deduped[:self._max_candidates], f"候选 {len(deduped)} 条"

    def _has_matching_search_candidate(
        self,
        items: List[Dict[str, Any]],
        base_url: str,
        target_title: str,
        tv_season_pack_only: bool = False,
        target_season: int = 0,
    ) -> bool:
        threshold = max(0.2, self._min_score - 0.2)
        for item in items or []:
            if not item.get("magnet") and not self._normalize_detail_url(item.get("detail_url"), base_url):
                continue
            title = (item.get("title") or "").strip()
            if not title or self._is_noise_detail_title(title):
                continue
            if tv_season_pack_only and not self._tv_title_matches_target_season(title, target_season):
                continue
            if self._title_reject_reason(target_title, title):
                continue
            if self._score_title(target_title, title) >= threshold:
                return True
            if self._has_title_evidence(target_title, title):
                return True
        return False

    def _candidate_from_row(self, row: Any) -> List[Dict[str, Any]]:
        magnet = self._get_by_path(row, self._magnet_path) if self._magnet_path else None
        title = self._get_by_path(row, self._title_path) if self._title_path else None
        seeders = self._get_by_path(row, self._seeders_path) if self._seeders_path else None
        if magnet:
            return [{"magnet": str(magnet), "title": str(title or ""), "seeders": seeders}]
        items = []
        for value in self._walk_values(row):
            if isinstance(value, str):
                for found in self._extract_magnets_from_text(value):
                    items.append({"magnet": found, "title": str(title or ""), "seeders": seeders})
        return items

    def _candidate_from_text(self, text: str, base_url: str = "", include_links: bool = True) -> List[Dict[str, Any]]:
        parser = _MagnetHTMLParser(self._extract_magnets_from_text)
        try:
            parser.feed(text or "")
        except Exception:
            pass
        candidates = list(parser.candidates)
        if include_links:
            for link in parser.links:
                detail_url = self._normalize_detail_url(link.get("detail_url"), base_url)
                if detail_url:
                    candidates.append({
                        "detail_url": detail_url,
                        "title": link.get("title") or "",
                        "seeders": link.get("seeders") or 0,
                    })
        parsed = {self._normalize_magnet(item.get("magnet")) for item in candidates}
        for found in self._extract_magnets_from_text(text or ""):
            if self._normalize_magnet(found) in parsed:
                continue
            candidates.append({"magnet": found, "title": "", "seeders": 0})
        return candidates

    def _candidate_from_search_suggest(
        self,
        keyword: str,
        base_url: str,
        headers: Dict[str, str],
        cookies: RequestsCookieJar,
    ) -> List[Dict[str, Any]]:
        origin = self._origin_url(base_url)
        if not origin or not keyword:
            return []
        suggest_url = self._append_query(urljoin(origin + "/", "/res/search_suggest"), {"q": keyword})
        suggest_headers = self._with_referer(headers, base_url)
        suggest_headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        try:
            response = self._search_http_request(
                "GET",
                suggest_url,
                headers=suggest_headers,
                cookies=cookies,
                attempts=2,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.debug(f"[MP115guanying] 搜索联想接口解析失败: {suggest_url} - {exc}")
            return []
        rows = payload
        if isinstance(payload, dict):
            rows = payload.get("data") or payload.get("list") or payload.get("results") or []
        if not isinstance(rows, list):
            return []
        candidates = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            detail_url = self._site_suggest_detail_url(row, origin)
            if not detail_url:
                continue
            title_parts = [
                row.get("title"),
                f"({row.get('year')})" if row.get("year") else "",
                row.get("ename"),
                row.get("type"),
            ]
            title = " ".join(str(item).strip() for item in title_parts if str(item or "").strip())
            candidates.append({
                "detail_url": detail_url,
                "title": title,
                "seeders": 0,
            })
        return candidates

    @staticmethod
    def _site_suggest_detail_url(row: Dict[str, Any], origin: str) -> str:
        detail = row.get("url") or row.get("href") or row.get("link")
        if detail:
            return urljoin(origin + "/", str(detail))
        media_dir = str(row.get("dir") or "").strip().strip("/")
        media_id = str(row.get("id") or row.get("bid") or row.get("vod_id") or "").strip()
        if not media_dir or not media_id:
            return ""
        if media_dir not in {"mv", "tv", "ac", "zy", "dsj", "dm"}:
            return ""
        return urljoin(origin + "/", f"/{media_dir}/{media_id}")

    def _candidate_from_detail_pages(
        self,
        items: List[Dict[str, Any]],
        base_url: str,
        headers: Dict[str, str],
        cookies: RequestsCookieJar,
        target_title: str,
        tv_season_pack_only: bool = False,
        target_season: int = 0,
    ) -> List[Dict[str, Any]]:
        links = self._rank_detail_links(
            items,
            base_url,
            target_title,
            allow_quality_fallback=False,
            tv_season_pack_only=tv_season_pack_only,
            target_season=target_season,
        )
        selected_links = links[:self._detail_max_pages]
        if selected_links:
            logger.info(f"[MP115guanying] 准备进入影片详情页: {len(selected_links)} / {len(links)}")

        candidates = []
        for index, link in enumerate(selected_links, 1):
            detail_url = link.get("detail_url")
            title = link.get("title") or ""
            logger.info(
                f"[MP115guanying] 影片详情页候选 {index}: score={link.get('score', 0):.3f}, "
                f"title={title or '(无标题)'}, url={detail_url}"
            )
            try:
                response = self._search_http_request(
                    "GET",
                    detail_url,
                    headers=self._with_referer(headers, base_url),
                    cookies=cookies,
                )
                response.raise_for_status()
            except Exception as exc:
                logger.warning(f"[MP115guanying] 详情页请求失败: {detail_url} - {exc}")
                continue
            text = response.text or ""
            final_url = getattr(response, "url", detail_url)
            status_code = getattr(response, "status_code", "")
            logger.info(
                f"[MP115guanying] 影片详情页已返回: HTTP {status_code or 'unknown'}, "
                f"内容 {len(text)} 字符, URL {final_url}"
            )
            if self._search_site_login_required(text):
                raise _SearchDomainUnavailable(f"详情页未登录或访问受限: {final_url}")
            if not text.strip():
                raise _SearchDomainUnavailable(f"详情页返回空内容: {final_url}")
            detail_candidates = self._candidate_from_text(text, base_url=final_url, include_links=True)
            magnet_count = 0
            for candidate in detail_candidates:
                magnet = self._normalize_magnet(candidate.get("magnet"))
                if not magnet:
                    continue
                magnet_count += 1
                candidates.append({
                    "magnet": magnet,
                    "title": candidate.get("title") or title,
                    "seeders": candidate.get("seeders") or link.get("seeders") or 0,
                    "detail_url": final_url,
                    "context_score": link.get("score") or 0,
                    "trusted_detail": True,
                })
            logger.info(f"[MP115guanying] 影片详情页直接磁链解析: {magnet_count} 条")
            if magnet_count:
                logger.info("[MP115guanying] 当前影片详情页已解析到磁链，停止进入后续影片页候选")
                return candidates
            api_candidates = self._candidate_from_site_downurl_api(
                media_url=final_url,
                media_title=title,
                headers=headers,
                cookies=cookies,
                context_score=link.get("score") or 0,
            )
            if api_candidates:
                candidates.extend(api_candidates)
                logger.info("[MP115guanying] 当前影片详情资源接口已解析到磁链，停止进入后续影片页候选")
                return candidates
            resource_candidates = self._candidate_from_resource_pages(
                items=detail_candidates,
                media_url=final_url,
                headers=headers,
                cookies=cookies,
                target_title=target_title,
                media_title=title,
                tv_season_pack_only=tv_season_pack_only,
                target_season=target_season,
            )
            if resource_candidates:
                candidates.extend(resource_candidates)
                logger.info("[MP115guanying] 当前影片详情页已解析到资源磁链，停止进入后续影片页候选")
                return candidates
        return candidates

    def _candidate_from_site_downurl_api(
        self,
        media_url: str,
        media_title: str,
        headers: Dict[str, str],
        cookies: RequestsCookieJar,
        context_score: float = 0,
    ) -> List[Dict[str, Any]]:
        api_url = self._site_downurl_api_url(media_url)
        if not api_url:
            return []
        api_headers = self._with_referer(headers, media_url)
        api_headers.update({
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        })
        try:
            response = self._search_http_request(
                "GET",
                api_url,
                headers=api_headers,
                cookies=cookies,
                attempts=3,
            )
            response.raise_for_status()
            text = response.text or ""
            if self._search_site_login_required(text):
                raise _SearchDomainUnavailable(f"影片详情资源接口未登录或访问受限: {api_url}")
            if not text.strip():
                raise _SearchDomainUnavailable(f"影片详情资源接口返回空内容: {api_url}")
            payload = response.json()
        except Exception as exc:
            if isinstance(exc, _SearchDomainUnavailable):
                raise
            logger.info(f"[MP115guanying] 影片详情资源接口解析失败: {api_url} - {exc}")
            return []
        if str(payload.get("code")) != "200":
            if self._search_site_login_required(str(payload.get("msg") or "")):
                raise _SearchDomainUnavailable(f"影片详情资源接口访问受限: {api_url}")
            logger.info(
                f"[MP115guanying] 影片详情资源接口返回非成功状态: "
                f"code={payload.get('code')}, msg={payload.get('msg') or ''}"
            )
            return []
        downlist = payload.get("downlist") or {}
        items = self._site_downlist_candidates(downlist, media_url, media_title, context_score)
        logger.info(f"[MP115guanying] 影片详情资源接口磁链解析: {len(items)} 条")
        return items

    def _site_downlist_candidates(
        self,
        downlist: Dict[str, Any],
        media_url: str,
        media_title: str,
        context_score: float = 0,
    ) -> List[Dict[str, Any]]:
        if not isinstance(downlist, dict):
            return []
        resource_list = downlist.get("list") or {}
        if not isinstance(resource_list, dict):
            return []
        hashes = resource_list.get("m") or []
        titles = resource_list.get("t") or []
        seeders = resource_list.get("e") or []
        kinds = resource_list.get("k") or []
        if not isinstance(hashes, list):
            return []
        candidates = []
        for index, raw_hash in enumerate(hashes):
            kind = self._list_value(kinds, index, "")
            title = str(self._list_value(titles, index, "") or media_title or "").strip()
            quality_label = self._site_downlist_quality_label(downlist, resource_list, index, len(hashes), kind)
            quality_title = " ".join(part for part in (title, quality_label) if part).strip()
            info_hash = self._site_downlist_info_hash(raw_hash, downlist)
            magnet = self._site_magnet_from_hash(info_hash, title)
            if not magnet:
                continue
            candidates.append({
                "magnet": magnet,
                "title": title,
                "quality_title": quality_title,
                "quality_label": quality_label,
                "seeders": self._to_int(self._list_value(seeders, index, 0), 0, 0, 1000000),
                "detail_url": media_url,
                "context_score": context_score,
                "trusted_detail": True,
            })
        if candidates and self._site_downlist_has_type_labels(downlist) and not any(
            item.get("quality_label") for item in candidates
        ):
            logger.info(
                "[MP115guanying] 资源接口分类诊断: "
                f"type={self._site_downlist_type_summary(downlist)}, "
                f"list_keys={','.join(list(resource_list.keys())[:20])}, "
                f"samples={self._site_downlist_field_samples(downlist, resource_list)}"
            )
        return candidates

    @staticmethod
    def _site_downlist_quality_label(
        downlist: Dict[str, Any],
        resource_list: Dict[str, Any],
        index: int,
        total: int,
        kind: Any = "",
    ) -> str:
        labels: List[str] = []
        type_pairs = MP115guanying._site_downlist_type_pairs(downlist)
        type_map = {}
        for label, code in type_pairs:
            if label:
                type_map[str(label).strip()] = str(label).strip()
            if code:
                type_map[str(code).strip()] = str(label).strip()
        kind_text = str(kind or "").strip()
        if kind_text:
            mapped = type_map.get(kind_text)
            if mapped:
                labels.append(mapped)
        for label, code in type_pairs:
            if not label or not code:
                continue
            if MP115guanying._site_downlist_category_matches(resource_list.get(code), index, total):
                labels.append(label)
        if kind_text and kind_text not in {"0", "0.0"} and not labels:
            try:
                numeric_index = int(kind_text)
            except Exception:
                numeric_index = -1
            if 0 <= numeric_index < len(type_pairs):
                labels.append(type_pairs[numeric_index][0])
            elif 1 <= numeric_index <= len(type_pairs):
                labels.append(type_pairs[numeric_index - 1][0])
        for key, values in (resource_list or {}).items():
            if key in {"m", "t", "e", "k"}:
                continue
            value = MP115guanying._list_value(values, index, None)
            if value is None:
                continue
            for token in MP115guanying._text_decode_variants(str(value)):
                mapped = type_map.get(token.strip())
                if mapped:
                    labels.append(mapped)
                elif MP115guanying._site_downlist_label_like_quality(token):
                    labels.append(token.strip())
        clean = []
        seen = set()
        for label in labels:
            label = str(label or "").strip()
            if label and label not in seen:
                seen.add(label)
                clean.append(label)
        return " ".join(clean)

    @staticmethod
    def _site_downlist_type_pairs(downlist: Dict[str, Any]) -> List[Tuple[str, str]]:
        type_info = downlist.get("type") or {}
        if not isinstance(type_info, dict):
            return []
        labels = type_info.get("a") or []
        codes = type_info.get("b") or []
        if not isinstance(labels, list):
            labels = []
        if not isinstance(codes, list):
            codes = []
        return [
            (
                str(labels[index] if index < len(labels) else "").strip(),
                str(codes[index] if index < len(codes) else "").strip(),
            )
            for index in range(max(len(labels), len(codes)))
        ]

    @staticmethod
    def _site_downlist_category_matches(value: Any, index: int, total: int) -> bool:
        if value is None:
            return False
        if isinstance(value, dict):
            for key in (index, str(index), index + 1, str(index + 1)):
                marker = value.get(key)
                if MP115guanying._site_truthy_marker(marker):
                    return True
            return False
        if isinstance(value, list):
            if len(value) == total:
                return MP115guanying._site_truthy_marker(MP115guanying._list_value(value, index, None))
            indexes = {str(item).strip() for item in value}
            return str(index) in indexes or str(index + 1) in indexes
        if isinstance(value, (str, int, float)):
            text = str(value or "").strip()
            if not text:
                return False
            indexes = {item for item in re.split(r"[\s,|;/]+", text) if item}
            return str(index) in indexes or str(index + 1) in indexes
        return False

    @staticmethod
    def _site_truthy_marker(value: Any) -> bool:
        if value is None or value is False:
            return False
        text = str(value).strip().lower()
        return text not in {"", "0", "false", "none", "null", "no"}

    @staticmethod
    def _site_downlist_label_like_quality(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return bool(
            re.search(r"(?i)(?:4k|2160p|1080p|720p|uhd|fhd)", text)
            or any(marker in text for marker in ("中字", "中文字幕", "简中", "繁中", "简繁", "中英"))
        )

    @staticmethod
    def _site_downlist_has_type_labels(downlist: Dict[str, Any]) -> bool:
        return any(label for label, _ in MP115guanying._site_downlist_type_pairs(downlist))

    @staticmethod
    def _site_downlist_type_summary(downlist: Dict[str, Any]) -> str:
        pairs = MP115guanying._site_downlist_type_pairs(downlist)
        return ",".join(f"{label}:{code}" if code else label for label, code in pairs if label or code)[:300]

    @staticmethod
    def _site_downlist_field_samples(downlist: Dict[str, Any], resource_list: Dict[str, Any]) -> str:
        keys = ["k"]
        keys.extend(code for _, code in MP115guanying._site_downlist_type_pairs(downlist) if code)
        samples = {}
        for key in keys:
            if not key or key not in resource_list:
                continue
            value = resource_list.get(key)
            if isinstance(value, list):
                samples[key] = value[:8]
            elif isinstance(value, dict):
                samples[key] = dict(list(value.items())[:8])
            else:
                samples[key] = str(value)[:80]
        try:
            return json.dumps(samples, ensure_ascii=False)[:500]
        except Exception:
            return str(samples)[:500]

    @staticmethod
    def _site_downurl_api_url(media_url: str) -> str:
        parsed = urlsplit(media_url or "")
        if not parsed.scheme or not parsed.netloc:
            return ""
        parts = [part for part in (parsed.path or "").split("/") if part]
        if len(parts) < 2:
            return ""
        media_dir, media_id = parts[0], parts[1]
        if media_dir not in {"mv", "tv", "ac", "zy", "dsj", "dm"} or not media_id:
            return ""
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        return urljoin(origin + "/", f"/res/downurl/{media_dir}/{media_id}")

    @classmethod
    def _site_downlist_info_hash(cls, raw_hash: Any, downlist: Dict[str, Any]) -> str:
        text = str(raw_hash or "").strip()
        if not text:
            return ""
        if re.fullmatch(r"[A-Fa-f0-9]{40}|[A-Z2-7]{32}", text):
            return text
        key = str(downlist.get("key") or "")
        order = downlist.get("sort") or []
        if not key or not isinstance(order, list):
            return ""
        try:
            decoded = base64.b64decode(text).decode("latin1")
            chars = [
                chr(ord(char) ^ ord(key[index % len(key)]))
                for index, char in enumerate(decoded)
            ]
            pieces = "".join(chars).split("|")
            joined = "".join(
                pieces[int(item)]
                for item in order
                if isinstance(item, int) or str(item).isdigit()
            )
        except Exception:
            return ""
        return joined if re.fullmatch(r"[A-Fa-f0-9]{40}|[A-Z2-7]{32}", joined) else ""

    @classmethod
    def _site_magnet_from_hash(cls, info_hash: str, title: str = "") -> str:
        info_hash = str(info_hash or "").strip()
        if not info_hash:
            return ""
        if info_hash.lower().startswith("magnet:"):
            return cls._normalize_magnet(info_hash)
        if not re.fullmatch(r"[A-Fa-f0-9]{40}|[A-Z2-7]{32}", info_hash):
            return ""
        magnet = f"magnet:?xt=urn:btih:{info_hash}"
        if title:
            magnet += f"&dn={quote(str(title))}"
        return cls._normalize_magnet(magnet)

    @staticmethod
    def _list_value(values: Any, index: int, default: Any = None) -> Any:
        if isinstance(values, list) and 0 <= index < len(values):
            return values[index]
        return default

    def _candidate_from_resource_pages(
        self,
        items: List[Dict[str, Any]],
        media_url: str,
        headers: Dict[str, str],
        cookies: RequestsCookieJar,
        target_title: str,
        media_title: str = "",
        tv_season_pack_only: bool = False,
        target_season: int = 0,
    ) -> List[Dict[str, Any]]:
        magnet_items = self._magnet_resource_items(items)
        logger.info(f"[MP115guanying] 磁力资源链接筛选: {len(magnet_items)} / {len(items)}")
        media_match_score = self._score_title(target_title, media_title)
        links = self._rank_detail_links(
            magnet_items,
            media_url,
            target_title,
            allow_quality_fallback=True,
            quality_fallback_context_score=media_match_score,
            tv_season_pack_only=tv_season_pack_only,
            target_season=target_season,
        )
        selected_links = links[:self._detail_max_pages]
        if selected_links:
            logger.info(f"[MP115guanying] 准备进入资源磁力页: {len(selected_links)} / {len(links)}")

        candidates = []
        for index, link in enumerate(selected_links, 1):
            resource_url = link.get("detail_url")
            title = link.get("title") or media_title or ""
            logger.info(
                f"[MP115guanying] 资源页候选 {index}: score={link.get('score', 0):.3f}, "
                f"quality={link.get('quality_score', 0)}, title={title or '(无标题)'}, url={resource_url}"
            )
            try:
                response = self._search_http_request(
                    "GET",
                    resource_url,
                    headers=self._with_referer(headers, media_url),
                    cookies=cookies,
                    attempts=3,
                    curl_after_failures=1,
                )
                response.raise_for_status()
            except Exception as exc:
                logger.warning(f"[MP115guanying] 资源页请求失败: {resource_url} - {exc}")
                continue
            text = response.text or ""
            final_url = getattr(response, "url", resource_url)
            status_code = getattr(response, "status_code", "")
            logger.info(
                f"[MP115guanying] 资源页已返回: HTTP {status_code or 'unknown'}, "
                f"内容 {len(text)} 字符, URL {final_url}"
            )
            resource_candidates = self._candidate_from_text(text, base_url=final_url, include_links=False)
            magnet_count = 0
            for candidate in resource_candidates:
                magnet = self._normalize_magnet(candidate.get("magnet"))
                if not magnet:
                    continue
                magnet_count += 1
                candidates.append({
                    "magnet": magnet,
                    "title": title or candidate.get("title"),
                    "seeders": candidate.get("seeders") or link.get("seeders") or 0,
                    "detail_url": final_url,
                    "context_score": media_match_score,
                    "trusted_detail": True,
                })
            logger.info(f"[MP115guanying] 资源页磁链解析: {magnet_count} 条")
            if magnet_count:
                logger.info("[MP115guanying] 当前资源页已解析到磁链，停止尝试后续资源页")
                return candidates
            if not magnet_count:
                logger.info(f"[MP115guanying] 资源页未解析到磁链，页面线索: {self._magnet_page_hints(text, final_url)}")
        return candidates

    def _magnet_resource_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """影片页里只保留“磁力”标签下的资源入口，排除网盘/迅雷等其它下载方式。"""
        selected = []
        for item in items:
            detail_url = item.get("detail_url") or ""
            title = item.get("title") or ""
            haystack = " ".join(self._text_decode_variants(f"{detail_url} {title}")).lower()
            if self._looks_like_non_magnet_resource(haystack):
                continue
            if self._looks_like_magnet_resource(haystack):
                selected.append(item)
        return selected

    @staticmethod
    def _looks_like_magnet_resource(text: str) -> bool:
        return any(marker in text for marker in (
            "seed_id=",
            "magnet",
            "btih",
            "磁力",
            "/magnet",
            "/link_start/",
        ))

    @staticmethod
    def _looks_like_non_magnet_resource(text: str) -> bool:
        return any(marker in text for marker in (
            "redirect_to=pan_id",
            "pan_id_",
            "baidu",
            "quark",
            "xunlei",
            "aliyun",
            "alipan",
            "115://",
            "百度",
            "网盘",
            "夸克",
            "迅雷",
            "阿里",
            "uc(",
            "uc云",
        ))

    def _rank_detail_links(
        self,
        items: List[Dict[str, Any]],
        base_url: str,
        target_title: str,
        allow_quality_fallback: bool = False,
        quality_fallback_context_score: float = 0,
        tv_season_pack_only: bool = False,
        target_season: int = 0,
    ) -> List[Dict[str, Any]]:
        links = []
        seen = set()
        for item in items:
            detail_url = self._normalize_detail_url(item.get("detail_url"), base_url)
            if not detail_url or detail_url in seen or detail_url == base_url:
                continue
            seen.add(detail_url)
            title = (item.get("title") or "").strip()
            if not title or self._is_noise_detail_title(title):
                continue
            reject_reason = self._title_reject_reason(target_title, title)
            if reject_reason:
                logger.info(f"[MP115guanying] 跳过标题冲突链接: {reject_reason}, title={title}")
                continue
            score = self._score_title(target_title, title)
            title_evidence = self._has_title_evidence(target_title, title)
            quality_score, quality_hits = self._quality_score(title)
            if allow_quality_fallback and not self._resource_quality_allowed(title):
                logger.info(f"[MP115guanying] 跳过未满足字幕/清晰度规则的资源: title={title}")
                continue
            if allow_quality_fallback and tv_season_pack_only and not self._tv_season_pack_allowed(title, target_season):
                logger.info(f"[MP115guanying] 跳过非目标季整季资源: title={title}")
                continue
            if not allow_quality_fallback and tv_season_pack_only and not self._tv_title_matches_target_season(title, target_season):
                logger.info(f"[MP115guanying] 跳过非目标季影片页: title={title}")
                continue
            threshold = max(0.2, self._min_score - 0.2)
            if title and score < threshold:
                if not allow_quality_fallback or quality_score <= 0:
                    continue
                if quality_fallback_context_score < threshold and score < 0.12:
                    logger.info(
                        f"[MP115guanying] 跳过疑似串片资源: score={score:.3f}, "
                        f"context={quality_fallback_context_score:.3f}, title={title}"
                    )
                    continue
            if score < self._min_score and not title_evidence:
                if not allow_quality_fallback or quality_fallback_context_score < self._min_score:
                    logger.info(
                        f"[MP115guanying] 跳过弱匹配候选: score={score:.3f}, "
                        f"context={quality_fallback_context_score:.3f}, title={title}"
                    )
                    continue
            links.append({
                "detail_url": detail_url,
                "title": title,
                "seeders": item.get("seeders") or 0,
                "score": score,
                "quality_score": quality_score,
                "quality_hits": quality_hits,
            })
        if allow_quality_fallback:
            links.sort(
                key=lambda x: (
                    x.get("quality_score", 0),
                    x.get("score", 0),
                    x.get("seeders", 0),
                ),
                reverse=True,
            )
            return links
        links.sort(
            key=lambda x: (
                x.get("score", 0),
                x.get("quality_score", 0),
                x.get("seeders", 0),
            ),
            reverse=True,
        )
        return links

    def _submit_115(self, magnet: str) -> Tuple[bool, str, Dict[str, Any]]:
        try:
            client = _Lixian115Client(cookie=self._load_cookie(), timeout=self._timeout)
            result = client.add_task(magnet=magnet, wp_path_id=self._wp_path_id, savepath=self._savepath)
            name = result.get("name") or "115 离线任务"
            return True, f"已添加: {name}", result
        except Exception as exc:
            message = str(exc)
            if self._is_duplicate_115_task(message):
                return True, f"115 任务已存在，视为已添加: {message}", {"duplicate": True, "message": message}
            return False, str(exc), {}

    def _build_search_context(self, event_data: Any) -> Dict[str, str]:
        context = getattr(event_data, "context", None)
        media = getattr(context, "media_info", None)
        meta = getattr(context, "meta_info", None)
        torrent = getattr(context, "torrent_info", None)
        subscribe = self._subscribe_from_event(event_data)
        episodes = sorted(getattr(event_data, "episodes", None) or getattr(meta, "episode_list", None) or [])
        season_list = getattr(meta, "season_list", None) or []
        subscribe_title = self._first(
            getattr(subscribe, "keyword", None),
            getattr(subscribe, "name", None),
        )
        media_title_raw = self._first(getattr(media, "title", None), getattr(media, "name", None))
        meta_title_raw = getattr(meta, "title", None)
        torrent_title_raw = getattr(torrent, "title", None)
        base_title = self._first(
            subscribe_title,
            self._clean_release_title_for_search(media_title_raw),
            self._clean_release_title_for_search(meta_title_raw),
            self._clean_release_title_for_search(torrent_title_raw),
            media_title_raw,
            meta_title_raw,
            torrent_title_raw,
        )
        torrent_title = self._first(getattr(torrent, "title", None), getattr(meta, "org_string", None), base_title)
        year = self._first(getattr(media, "year", None), getattr(subscribe, "year", None), "")
        season = season_list[0] if season_list else self._first(
            getattr(media, "season", None),
            getattr(meta, "season", None),
            getattr(subscribe, "season", None),
            self._season_from_text(subscribe_title),
            self._season_from_text(base_title),
            self._season_from_text(media_title_raw),
            self._season_from_text(meta_title_raw),
            self._season_from_text(torrent_title),
        )
        episode = episodes[0] if episodes else ""
        media_type = self._first(getattr(media, "type", None), getattr(meta, "type", None), getattr(subscribe, "type", None), "")
        media_type_text = self._media_type_text(media_type)
        season_number = self._season_number(season)
        is_tv = self._is_tv_media_type(media_type) or bool(season_number)
        search_title = self._tv_search_title(base_title, subscribe_title, season_number) if is_tv else base_title
        # 搜索站点第一步是影片检索，不是 PT 种子检索；匹配目标也以媒体标题为准。
        title_candidates = self._collect_title_candidates(media, meta)
        for value in (base_title, subscribe_title, media_title_raw, meta_title_raw, torrent_title_raw):
            cleaned = self._clean_release_title_for_search(value)
            if cleaned:
                title_candidates.append(cleaned)
            stripped = self._strip_tv_season_marker(value)
            if stripped:
                title_candidates.append(stripped)
        title_candidates = self._dedupe_title_values(title_candidates)
        target_year = "" if is_tv else year
        target_titles = [
            " ".join(str(v) for v in [item, target_year] if v)
            for item in title_candidates
        ] or [" ".join(str(v) for v in [self._strip_tv_season_marker(base_title) or base_title, target_year] if v)]
        target_title = " || ".join(target_titles)
        return {
            "keyword": "",
            "raw_keyword": "",
            "title": str(search_title or ""),
            "media_title": str(base_title or ""),
            "search_title": str(search_title or ""),
            "target_title": target_title,
            "torrent_title": str(torrent_title or ""),
            "year": str(year or ""),
            "media_type": media_type_text,
            "is_tv": "1" if is_tv else "",
            "season": str(season_number or season or ""),
            "episode": str(episode or ""),
            "tmdbid": str(self._first(getattr(media, "tmdb_id", None), "")),
            "imdbid": str(self._first(getattr(media, "imdb_id", None), getattr(torrent, "imdbid", None), "")),
            "doubanid": str(self._first(getattr(media, "douban_id", None), "")),
        }

    def _search_context_from_subscribe(self, subscribe: Any) -> Dict[str, str]:
        title = str(self._first(
            getattr(subscribe, "keyword", None),
            getattr(subscribe, "name", None),
        ) or "").strip()
        media_title = str(getattr(subscribe, "name", "") or title).strip()
        year = str(getattr(subscribe, "year", "") or "").strip()
        media_type = self._media_type_text(getattr(subscribe, "type", ""))
        season_number = self._season_number(getattr(subscribe, "season", None)) or self._season_from_text(title)
        is_tv = self._is_tv_media_type(media_type) or bool(season_number)
        search_title = self._tv_search_title(media_title, title, season_number) if is_tv else (title or media_title)
        base_for_target = self._strip_tv_season_marker(media_title) or self._strip_tv_season_marker(title) or search_title
        target_year = "" if is_tv else year
        target_titles = [
            " ".join(str(item) for item in [candidate, target_year] if item)
            for candidate in self._dedupe_title_values([base_for_target, title, media_title])
            if candidate
        ]
        return {
            "keyword": "",
            "raw_keyword": "",
            "title": str(search_title or ""),
            "media_title": str(media_title or title or ""),
            "search_title": str(search_title or ""),
            "target_title": " || ".join(target_titles) or str(search_title or title or ""),
            "torrent_title": "",
            "year": year,
            "media_type": media_type,
            "is_tv": "1" if is_tv else "",
            "season": str(season_number or ""),
            "episode": "",
            "tmdbid": str(getattr(subscribe, "tmdbid", "") or ""),
            "imdbid": str(getattr(subscribe, "imdbid", "") or ""),
            "doubanid": str(getattr(subscribe, "doubanid", "") or ""),
        }

    def _subscribe_from_event(self, event_data: Any) -> Any:
        origin = getattr(event_data, "origin", "") or ""
        subscribe_info = self._parse_subscribe_origin(origin)
        subscribe_id = subscribe_info.get("id") if subscribe_info else None
        if not subscribe_id:
            return None
        try:
            return SubscribeOper().get(int(subscribe_id))
        except Exception as exc:
            logger.debug(f"[MP115guanying] 读取订阅信息失败: {exc}")
            return None

    @classmethod
    def _collect_title_candidates(cls, *sources: Any) -> List[str]:
        values = []
        for source in sources:
            if not source:
                continue
            for attr in (
                "title",
                "name",
                "original_title",
                "original_name",
                "en_title",
                "english_title",
                "aka",
                "akas",
                "alias",
                "aliases",
                "names",
                "also_known_as",
                "title_aliases",
            ):
                cls._append_title_values(values, getattr(source, attr, None))
        return cls._dedupe_title_values(values)

    @classmethod
    def _append_title_values(cls, values: List[str], value: Any):
        if value is None:
            return
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                values.append(stripped)
            return
        if isinstance(value, dict):
            for item in value.values():
                cls._append_title_values(values, item)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                cls._append_title_values(values, item)

    @classmethod
    def _dedupe_title_values(cls, values: Iterable[str]) -> List[str]:
        deduped = []
        seen = set()
        for item in values:
            normalized = cls._normalize_title(item)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(str(item).strip())
        return deduped

    def _update_subscribe_state(self, origin: str, context: Any, event_data: Any):
        subscribe_info = self._parse_subscribe_origin(origin)
        subscribe_id = subscribe_info.get("id") if subscribe_info else None
        if not subscribe_id:
            return
        try:
            subscribe_oper = SubscribeOper()
            subscribe = subscribe_oper.get(int(subscribe_id))
            if not subscribe:
                return
            media = getattr(context, "media_info", None)
            meta = getattr(context, "meta_info", None)
            media_type = getattr(media, "type", None)
            if media_type == MediaType.MOVIE or getattr(subscribe, "type", "") == MediaType.MOVIE.value:
                note = [1]
                lack_episode = 0
                finish = True
            else:
                existing_note = list(getattr(subscribe, "note", None) or [])
                episodes = sorted(getattr(event_data, "episodes", None) or getattr(meta, "episode_list", None) or [])
                new_note = sorted(set(existing_note).union(set(episodes))) if episodes else existing_note
                note = new_note
                old_lack = self._to_int(getattr(subscribe, "lack_episode", None), 0, 0, 100000)
                newly_done = len(set(new_note).difference(set(existing_note)))
                lack_episode = max(0, old_lack - newly_done) if old_lack else old_lack
                finish = bool(old_lack and lack_episode == 0)

            update_payload = {"note": note, "last_update": time.strftime("%Y-%m-%d %H:%M:%S")}
            if getattr(subscribe, "type", "") == MediaType.TV.value:
                update_payload["lack_episode"] = lack_episode
            subscribe_oper.update(subscribe.id, update_payload)
            if finish:
                latest = subscribe_oper.get(subscribe.id)
                if latest:
                    subscribe_oper.add_history(**latest.to_dict())
                    subscribe_oper.delete(latest.id)
                    eventmanager.send_event(EventType.SubscribeComplete, {
                        "subscribe_id": latest.id,
                        "subscribe_info": latest.to_dict(),
                        "mediainfo": media.to_dict() if hasattr(media, "to_dict") else {},
                    })
                    if SubscribeHelper:
                        SubscribeHelper().sub_done_async({
                            "tmdbid": getattr(media, "tmdb_id", None),
                            "doubanid": getattr(media, "douban_id", None),
                        })
                    logger.info(f"[MP115guanying] 已更新订阅完成状态: {latest.name}")
        except Exception as exc:
            logger.warning(f"[MP115guanying] 更新订阅状态失败: {exc}")

    def _update_subscribe_state_from_subscribe(self, subscribe: Any):
        if not subscribe:
            return
        try:
            subscribe_id = getattr(subscribe, "id", None)
            if not subscribe_id:
                return
            subscribe_oper = SubscribeOper()
            latest = subscribe_oper.get(int(subscribe_id))
            if not latest:
                return
            if getattr(latest, "type", "") == MediaType.MOVIE.value:
                subscribe_oper.update(latest.id, {
                    "note": [1],
                    "last_update": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                completed = subscribe_oper.get(latest.id)
                if completed:
                    subscribe_oper.add_history(**completed.to_dict())
                    subscribe_oper.delete(completed.id)
                    eventmanager.send_event(EventType.SubscribeComplete, {
                        "subscribe_id": completed.id,
                        "subscribe_info": completed.to_dict(),
                        "mediainfo": {},
                    })
                logger.info(f"[MP115guanying] 已将电影订阅标记完成: {getattr(latest, 'name', '')}")
                return
            update_payload = {"last_update": time.strftime("%Y-%m-%d %H:%M:%S")}
            total_episode = self._to_int(getattr(latest, "total_episode", None), 0, 0, 100000)
            start_episode = self._to_int(getattr(latest, "start_episode", None), 1, 1, 100000)
            if total_episode:
                update_payload["note"] = list(range(start_episode, total_episode + 1))
                update_payload["lack_episode"] = 0
            subscribe_oper.update(latest.id, update_payload)
            if total_episode:
                completed = subscribe_oper.get(latest.id)
                if completed:
                    subscribe_oper.add_history(**completed.to_dict())
                    subscribe_oper.delete(completed.id)
                    eventmanager.send_event(EventType.SubscribeComplete, {
                        "subscribe_id": completed.id,
                        "subscribe_info": completed.to_dict(),
                        "mediainfo": {},
                    })
                logger.info(f"[MP115guanying] 已将电视剧整季订阅标记完成: {getattr(latest, 'name', '')}")
        except Exception as exc:
            logger.warning(f"[MP115guanying] 订阅兜底更新订阅状态失败: {exc}")

    def _save_record(self, search_context: Dict[str, Any], selected: Dict[str, Any], message: str, result: Dict[str, Any]):
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "keyword": search_context.get("title") or search_context.get("torrent_title"),
            "selected": selected,
            "message": message,
            "result": result,
        }
        try:
            records = self.get_data("latest_records") or []
            records.insert(0, record)
            self.save_data("latest_records", records[:50])
        except Exception as exc:
            logger.debug(f"[MP115guanying] 保存记录失败: {exc}")

    def _submission_guard_lookup_keys(self, search_context: Dict[str, Any], origin: str = "") -> List[str]:
        keys = [self._submission_guard_key(search_context, origin)]
        keys.extend(self._submission_guard_broad_keys(search_context, include_missing_season=True))
        if self._is_tv_context(search_context):
            keys.extend([
                self._submission_guard_key(search_context, origin, include_origin=False),
                self._submission_guard_key(
                    search_context,
                    origin,
                    include_year=False,
                    include_ids=False,
                    include_origin=False,
                ),
            ])
            if not self._target_season_number(search_context):
                keys.extend([
                    self._submission_guard_key(search_context, origin, include_season=False),
                    self._submission_guard_key(
                        search_context,
                        origin,
                        include_season=False,
                        include_year=False,
                        include_ids=False,
                        include_origin=False,
                    ),
                ])
        return self._dedupe_title_values([key for key in keys if key])

    def _submission_guard_store_keys(self, search_context: Dict[str, Any], origin: str = "") -> List[str]:
        keys = [self._submission_guard_key(search_context, origin)]
        keys.extend(self._submission_guard_broad_keys(search_context, include_missing_season=True))
        if self._is_tv_context(search_context) and self._target_season_number(search_context):
            keys.extend([
                self._submission_guard_key(search_context, origin, include_origin=False),
                self._submission_guard_key(
                    search_context,
                    origin,
                    include_year=False,
                    include_ids=False,
                    include_origin=False,
                ),
                self._submission_guard_key(search_context, origin, include_season=False),
                self._submission_guard_key(
                    search_context,
                    origin,
                    include_season=False,
                    include_year=False,
                    include_ids=False,
                    include_origin=False,
                ),
            ])
        return self._dedupe_title_values([key for key in keys if key])

    def _submission_guard_key(
        self,
        search_context: Dict[str, Any],
        origin: str = "",
        include_season: bool = True,
        include_year: bool = True,
        include_ids: bool = True,
        include_origin: bool = True,
    ) -> str:
        subscribe_id = self._subscribe_id_from_origin(origin) if include_origin else ""
        title = self._submission_guard_title(search_context)
        parts = [
            f"subscribe:{subscribe_id}" if subscribe_id else "",
            search_context.get("media_type"),
            title,
        ]
        if include_year:
            parts.append(search_context.get("year"))
        if include_ids:
            parts.extend([
                search_context.get("tmdbid"),
                search_context.get("imdbid"),
                search_context.get("doubanid"),
            ])
        if include_season:
            parts.extend([
                search_context.get("season"),
                search_context.get("episode"),
            ])
        raw = "|".join(str(item or "").strip() for item in parts if str(item or "").strip())
        return self._normalize_title(raw)

    @classmethod
    def _submission_guard_broad_keys(
        cls,
        search_context: Dict[str, Any],
        include_missing_season: bool = False,
    ) -> List[str]:
        values = search_context or {}
        season = cls._submission_guard_season(values)
        is_tv = cls._is_tv_context(values) or bool(season)
        bases: List[str] = []
        for value in (
            values.get("media_title"),
            values.get("title"),
            values.get("search_title"),
            values.get("torrent_title"),
        ):
            cls._append_guard_base(bases, value, is_tv)
        for value in cls._target_title_values(values.get("target_title", "")):
            cls._append_guard_base(bases, value, is_tv)

        keys = []
        seen = set()
        for base in bases:
            normalized = cls._normalize_title(base)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            if is_tv:
                if season:
                    keys.append(f"tvtitle:{normalized}:s{season:02d}")
                    keys.append(f"tvtitle:{normalized}:any")
                elif include_missing_season:
                    keys.append(f"tvtitle:{normalized}:any")
            else:
                keys.append(f"title:{normalized}")
        return keys

    @classmethod
    def _append_guard_base(cls, values: List[str], value: Any, is_tv: bool):
        for item in cls._target_title_values(str(value or "")) or [str(value or "")]:
            base = cls._tv_keyword_base(item) if is_tv else cls._clean_release_title_for_search(item)
            base = base or str(item or "").strip()
            if base:
                values.append(base)

    @classmethod
    def _submission_guard_season(cls, search_context: Dict[str, Any]) -> int:
        values = search_context or {}
        season = cls._target_season_number(values)
        if season:
            return season
        for value in (
            values.get("title"),
            values.get("search_title"),
            values.get("media_title"),
            values.get("torrent_title"),
            values.get("target_title"),
        ):
            season = cls._season_from_text(value)
            if season:
                return season
        return 0

    @classmethod
    def _submission_guard_title(cls, search_context: Dict[str, Any]) -> str:
        if cls._is_tv_context(search_context):
            for value in (
                search_context.get("media_title"),
                search_context.get("title"),
                search_context.get("search_title"),
            ):
                base = cls._tv_keyword_base(value)
                if base:
                    return base
            for value in cls._target_title_values(search_context.get("target_title", "")):
                base = cls._tv_keyword_base(value)
                if base:
                    return base
        return str(search_context.get("media_title") or search_context.get("title") or "").strip()

    @classmethod
    def _magnet_submission_key(cls, magnet: Any) -> str:
        normalized = cls._normalize_magnet(magnet)
        if not normalized:
            return ""
        match = re.search(r"btih:([^&]+)", normalized, re.I)
        if match:
            return "magnet:" + match.group(1).lower()
        return "magnet:" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _submission_done_keys(keys: Any) -> List[str]:
        if isinstance(keys, str):
            keys = [keys]
        return [f"done:{key}" for key in (keys or []) if key]

    def _submission_success_keys(self, keys: Any) -> List[str]:
        if isinstance(keys, str):
            keys = [keys]
        keys = [key for key in (keys or []) if key]
        return keys + self._submission_done_keys(keys)

    def _has_recent_success_context(self, search_context: Optional[Dict[str, Any]]) -> bool:
        if not search_context:
            return False
        return self._is_recent_submission(
            self._submission_done_keys(self._submission_guard_lookup_keys(search_context, "")),
            force_reload=True,
        )

    def _subscribe_id_from_origin(self, origin: str = "") -> str:
        subscribe_info = self._parse_subscribe_origin(origin)
        return str(subscribe_info.get("id") or "") if subscribe_info else ""

    def _is_recent_submission(self, keys: Any, force_reload: bool = True) -> bool:
        if isinstance(keys, str):
            keys = [keys]
        keys = [key for key in (keys or []) if key]
        if not keys:
            return False
        now = time.time()
        recent = self._recent_submission_map(force_reload=force_reload)
        return any(
            self._to_float(recent.get(key), 0, 0, 9999999999) > now
            for key in keys
        )

    def _claim_recent_submission(self, keys: Any, ttl: int = 120) -> bool:
        if isinstance(keys, str):
            keys = [keys]
        keys = [key for key in (keys or []) if key]
        if not keys:
            return True
        with self._recent_guard_lock:
            now = time.time()
            recent = self._recent_submission_map(force_reload=True)
            if any(
                self._to_float(recent.get(key), 0, 0, 9999999999) > now
                for key in keys
            ):
                return False
            expires_at = now + self._to_int(ttl, self._recent_submit_ttl, 1, 86400)
            for key in keys:
                recent[key] = expires_at
            self._store_recent_submission_map(self._prune_recent_submission_map(recent, now))
            return True

    def _remember_recent_submission(
        self,
        key: Any,
        search_context: Dict[str, Any],
        selected: Dict[str, Any],
        ttl: Optional[int] = None,
    ):
        keys = [key] if isinstance(key, str) else list(key or [])
        keys = [item for item in keys if item]
        if not keys:
            return
        with self._recent_guard_lock:
            now = time.time()
            recent = self._recent_submission_map(force_reload=True)
            expires_at = now + self._to_int(ttl, self._recent_submit_ttl, 1, 86400)
            for item in keys:
                recent[item] = expires_at
            self._store_recent_submission_map(self._prune_recent_submission_map(recent, now))

    def _forget_recent_submission(self, keys: Any):
        if isinstance(keys, str):
            keys = [keys]
        keys = [key for key in (keys or []) if key]
        if not keys:
            return
        with self._recent_guard_lock:
            recent = self._recent_submission_map(force_reload=True)
            for key in keys:
                recent.pop(key, None)
            self._store_recent_submission_map(recent)

    def _store_recent_submission_map(self, recent: Dict[str, Any]):
        recent = dict(recent or {})
        self._recent_submissions = recent
        try:
            type(self)._recent_submissions_global = dict(recent)
        except Exception:
            pass
        try:
            self.save_data("recent_submissions", recent)
        except Exception as exc:
            logger.debug(f"[MP115guanying] 保存近期提交记录失败: {exc}")

    def _prune_recent_submission_map(self, recent: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        cutoff = now - 60
        return {
            item_key: item_expires_at
            for item_key, item_expires_at in dict(recent or {}).items()
            if self._to_float(item_expires_at, 0, 0, 9999999999) > cutoff
        }

    def _recent_submission_map(self, force_reload: bool = False) -> Dict[str, Any]:
        recent: Dict[str, Any] = {}
        if not force_reload:
            cached = getattr(self, "_recent_submissions", None)
            if isinstance(cached, dict):
                recent.update(cached)
        try:
            global_recent = getattr(type(self), "_recent_submissions_global", {}) or {}
            if isinstance(global_recent, dict):
                recent.update(global_recent)
        except Exception:
            pass
        if force_reload or not recent:
            try:
                loaded = self.get_data("recent_submissions") or {}
            except Exception:
                loaded = {}
            if isinstance(loaded, dict):
                recent.update(loaded)
        recent = self._prune_recent_submission_map(recent)
        self._recent_submissions = dict(recent)
        try:
            type(self)._recent_submissions_global = dict(recent)
        except Exception:
            pass
        return dict(recent)

    def _notify_message(self, title: str, text: str):
        if not self._notify:
            return
        try:
            self.post_message(title=title, text=text)
        except Exception as exc:
            logger.debug(f"[MP115guanying] 发送通知失败: {exc}")

    def _check_cookie(self) -> Tuple[bool, str]:
        try:
            cookie = self._load_cookie()
            missing = [key for key in ("UID", "CID", "SEID") if f"{key}=" not in cookie]
            if missing:
                return False, f"Cookie 缺少字段: {','.join(missing)}"
            return True, "Cookie 格式看起来正常"
        except Exception as exc:
            return False, str(exc)

    def _load_cookie(self) -> str:
        if self._cookie:
            return self._cookie
        if self._cookies_file:
            with open(self._cookies_file, "r", encoding="utf-8") as fp:
                return fp.read().strip()
        return ""

    def _ensure_search_site_login(
        self,
        url: str,
        headers: Dict[str, str],
        cookies: RequestsCookieJar,
    ):
        if not self._search_username or not self._search_password:
            return
        login_url = self._search_login_endpoint(url)
        if not login_url:
            return
        login_headers = self._with_referer(headers, self._origin_url(url))
        masked_username = self._mask_account(self._search_username)
        try:
            logger.info(f"[MP115guanying] 搜索站点登录准备: url={login_url}, user={masked_username}")
            login_page = self._search_http_request(
                "GET",
                login_url,
                headers=login_headers,
                cookies=cookies,
                attempts=3,
            )
            if self._search_site_logged_in(login_page.text or ""):
                logger.info("[MP115guanying] 搜索站点已处于登录状态")
                return
            post_headers = self._with_referer(headers, login_url)
            post_headers.update({
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": self._origin_url(login_url),
                "X-Requested-With": "XMLHttpRequest",
            })
            response = self._http_request(
                "POST",
                login_url,
                headers=post_headers,
                data={
                    "code": "",
                    "siteid": "1",
                    "dosubmit": "1",
                    "cookietime": "10506240",
                    "username": self._search_username,
                    "password": self._search_password,
                },
                cookies=cookies,
            )
            text = (response.text or "").strip()
            if text:
                try:
                    result = response.json()
                except Exception:
                    result = {}
                if str(result.get("code")) == "200" or result.get("success") is True:
                    logger.info("[MP115guanying] 搜索站点账号登录成功")
                    return
                if self._verify_search_login_state(url, headers, cookies):
                    logger.info("[MP115guanying] 搜索站点登录后状态确认成功")
                    return
                message = result.get("msg") if isinstance(result, dict) else ""
                logger.warning(f"[MP115guanying] 搜索站点登录未确认成功: {message or text[:120]}")
            else:
                if self._verify_search_login_state(url, headers, cookies):
                    logger.info("[MP115guanying] 搜索站点登录接口返回空内容，但登录状态确认成功")
                    return
                logger.warning("[MP115guanying] 搜索站点登录接口返回空内容，将继续使用当前 Cookie 尝试搜索")
        except Exception as exc:
            logger.warning(f"[MP115guanying] 搜索站点账号登录失败，将继续尝试搜索: {exc}")

    def _verify_search_login_state(
        self,
        url: str,
        headers: Dict[str, str],
        cookies: RequestsCookieJar,
    ) -> bool:
        try:
            response = self._search_http_request(
                "GET",
                self._origin_url(url) or url,
                headers=self._with_referer(headers, url),
                cookies=cookies,
                attempts=2,
            )
            text = response.text or ""
            if self._search_site_logged_in(text):
                logger.info("[MP115guanying] 搜索站点登录状态确认成功")
                return True
            if self._search_site_login_required(text):
                logger.warning("[MP115guanying] 搜索站点仍显示未登录，请检查搜索站点账号/密码或改填搜索站点 Cookie")
                return False
            return False
        except Exception as exc:
            logger.debug(f"[MP115guanying] 搜索站点登录状态确认失败: {exc}")
            return False

    def _search_http_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
        cookies: Optional[RequestsCookieJar] = None,
        attempts: int = 3,
        curl_after_failures: int = 0,
    ):
        response = self._http_request(
            method,
            url,
            headers=headers,
            data=data,
            cookies=cookies,
            attempts=attempts,
            curl_after_failures=curl_after_failures,
        )
        if (method or "GET").upper() != "GET":
            return response
        return self._complete_browser_verification_if_needed(
            response=response,
            requested_url=url,
            headers=headers or {},
            cookies=cookies,
        )

    def _complete_browser_verification_if_needed(
        self,
        response: Any,
        requested_url: str,
        headers: Dict[str, str],
        cookies: Optional[RequestsCookieJar],
    ):
        text = response.text or ""
        challenge = self._parse_browser_verification_challenge(text)
        if not challenge:
            return response
        verify_url = getattr(response, "url", requested_url) or requested_url
        logger.info(f"[MP115guanying] 搜索站点触发浏览器计算验证，开始计算: {verify_url}")
        nonces = self._solve_browser_verification_challenge(challenge)
        verify_data = [("action", "verify"), ("id", str(challenge.get("id") or ""))]
        verify_data.extend(("nonce[]", str(nonce)) for nonce in nonces)
        verify_headers = self._with_referer(headers, verify_url)
        verify_headers["Content-Type"] = "application/x-www-form-urlencoded"
        verify_response = self._http_request(
            "POST",
            verify_url,
            headers=verify_headers,
            data=verify_data,
            cookies=cookies,
        )
        try:
            result = verify_response.json()
        except Exception as exc:
            raise RuntimeError(f"浏览器计算验证返回非 JSON: {(verify_response.text or '')[:120]}") from exc
        if result.get("success") is not True:
            raise RuntimeError(f"浏览器计算验证失败: {result}")
        logger.info("[MP115guanying] 搜索站点浏览器计算验证通过，重新请求原页面")
        return self._http_request(
            "GET",
            verify_url,
            headers=self._with_referer(headers, verify_url),
            cookies=cookies,
        )

    @classmethod
    def _parse_browser_verification_challenge(cls, text: str) -> Dict[str, Any]:
        if not text or "浏览器安全验证" not in text:
            return {}
        match = cls._browser_verify_json_regex.search(text)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(1))
        except Exception:
            return {}
        if not payload.get("id") or not payload.get("challenge") or not payload.get("salt"):
            return {}
        return payload

    @staticmethod
    def _solve_browser_verification_challenge(challenge: Dict[str, Any]) -> List[int]:
        targets = [str(item) for item in (challenge.get("challenge") or []) if item]
        diff = int(challenge.get("diff") or 0)
        salt = str(challenge.get("salt") or "")
        if not targets or not salt or diff <= 0:
            raise RuntimeError("浏览器计算验证参数不完整")
        if diff > 5000000:
            raise RuntimeError(f"浏览器计算验证难度过高: {diff}")
        remaining = set(targets)
        found: Dict[str, int] = {}
        for nonce in range(diff + 1):
            digest = hashlib.sha256(f"{nonce}{salt}".encode("utf-8")).hexdigest()
            if digest in remaining:
                found[digest] = nonce
                remaining.remove(digest)
                if not remaining:
                    break
        missing = [target for target in targets if target not in found]
        if missing:
            raise RuntimeError(f"浏览器计算验证 nonce 未找到: {len(missing)}")
        return [found[target] for target in targets]

    def _search_login_endpoint(self, search_url: str) -> str:
        origin = self._origin_url(search_url)
        login_url = (self._search_login_url or "").strip()
        if login_url:
            parsed = urlsplit(login_url)
            if parsed.scheme and parsed.netloc and origin:
                origin_parts = urlsplit(origin)
                return urlunsplit((
                    origin_parts.scheme,
                    origin_parts.netloc,
                    parsed.path or "/user/login",
                    parsed.query,
                    "",
                ))
            return urljoin(origin + "/", login_url) if origin else urljoin(search_url or "", login_url)
        return urljoin(origin + "/", "/user/login") if origin else ""

    @staticmethod
    def _origin_url(url: str) -> str:
        parsed = urlsplit(url or "")
        if not parsed.scheme or not parsed.netloc:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @staticmethod
    def _search_site_logged_in(text: str) -> bool:
        return bool(text and ("/user/logout" in text or "退出登录" in text or "账户设置" in text))

    @staticmethod
    def _search_site_login_required(text: str) -> bool:
        return bool(text and ("未登录" in text or "请登录后继续" in text or "/user/login" in text))

    @staticmethod
    def _mask_account(value: str) -> str:
        text = str(value or "")
        if len(text) <= 4:
            return "*" * len(text)
        return f"{text[:2]}***{text[-2:]}"

    def _request_search_page(
        self,
        url: str,
        keyword: str,
        headers: Dict[str, str],
        cookies: RequestsCookieJar,
    ):
        method = self._search_method if self._search_method in ("GET", "POST") else "GET"
        field = self._post_field or "keyword"
        if self._has_keyword_placeholder(self._search_url) or method == "POST":
            data = {field: keyword} if method == "POST" else None
            if method == "POST":
                return self._search_http_request(method, url, headers=headers, data=data, cookies=cookies)
            try:
                response = self._search_http_request(method, url, headers=headers, data=data, cookies=cookies)
                response.raise_for_status()
                return response
            except Exception as exc:
                parsed = urlsplit(url or "")
                origin_url = urlunsplit((parsed.scheme, parsed.netloc, "/", "", "")) if parsed.scheme and parsed.netloc else url
                logger.warning(f"[MP115guanying] 直接搜索入口失败，尝试备用搜索入口: {exc}")
                return self._request_fallback_search_urls(
                    url=origin_url,
                    field=field,
                    keyword=keyword,
                    headers=headers,
                    cookies=cookies,
                    referer_url=url,
                )

        logger.info("[MP115guanying] 搜索框模式: 先打开搜索页/首页并尝试识别页面搜索表单")
        try:
            initial = self._search_http_request("GET", url, headers=headers, cookies=cookies, attempts=1)
            initial.raise_for_status()
        except Exception as exc:
            logger.warning(f"[MP115guanying] 打开搜索首页失败，直接尝试搜索入口: {exc}")
            return self._request_fallback_search_urls(
                url=url,
                field=field,
                keyword=keyword,
                headers=headers,
                cookies=cookies,
                referer_url=url,
            )
        form = self._extract_search_form(initial.text or "", getattr(initial, "url", url), keyword)
        if form:
            form_method, form_url, form_data, form_field = form
            form_headers = self._build_search_headers(form_url)
            form_headers.update(headers)
            form_headers = self._with_referer(form_headers, getattr(initial, "url", url))
            logger.info(
                f"[MP115guanying] 发现搜索表单: method={form_method}, field={form_field}, url={form_url}"
            )
            if form_method == "POST":
                return self._search_http_request("POST", form_url, headers=form_headers, data=form_data, cookies=cookies)
            return self._search_http_request(
                "GET",
                self._append_query(form_url, form_data),
                headers=form_headers,
                cookies=cookies,
            )

        logger.info("[MP115guanying] 未发现标准搜索表单，改用 GET 搜索入口")
        return self._request_fallback_search_urls(
            url=url,
            field=field,
            keyword=keyword,
            headers=headers,
            cookies=cookies,
            referer_url=getattr(initial, "url", url),
            initial=initial,
        )

    def _request_fallback_search_urls(
        self,
        url: str,
        field: str,
        keyword: str,
        headers: Dict[str, str],
        cookies: RequestsCookieJar,
        referer_url: str = "",
        initial: Any = None,
    ):
        urls = self._fallback_search_urls(url, field, keyword)
        logger.info(f"[MP115guanying] 尝试 GET 搜索入口: {len(urls)} 个")
        best_response = initial if self._is_usable_search_response(initial, url, keyword) else None
        best_score = self._search_response_score(best_response, keyword) if best_response is not None else -1
        last_error = None
        for index, search_url in enumerate(urls, 1):
            try:
                response = self._search_http_request(
                    "GET",
                    search_url,
                    headers=self._with_referer(headers, referer_url or url),
                    cookies=cookies,
                    attempts=6 if self._is_preferred_search_url(search_url) else 3,
                )
                response.raise_for_status()
            except Exception as exc:
                last_error = exc
                logger.warning(f"[MP115guanying] 搜索入口请求失败 {index}/{len(urls)}: {search_url} - {exc}")
                continue
            score = self._search_response_score(response, keyword)
            logger.info(
                f"[MP115guanying] 搜索入口候选 {index}/{len(urls)}: score={score}, "
                f"内容 {len(response.text or '')} 字符, URL {getattr(response, 'url', search_url)}"
            )
            if self._is_preferred_search_response(response, search_url, keyword):
                logger.info(f"[MP115guanying] 使用真实搜索结果页: {getattr(response, 'url', search_url)}")
                return response
            if not self._is_usable_search_response(response, search_url, keyword):
                logger.info(f"[MP115guanying] 跳过疑似泛化首页搜索结果: {getattr(response, 'url', search_url)}")
                continue
            if score > best_score:
                best_score = score
                best_response = response
            if self._response_contains_keyword(response, keyword):
                best_response = response
                break
        preferred_retry = [
            item
            for item in urls
            if self._is_preferred_search_url(item)
        ]
        if preferred_retry:
            logger.info("[MP115guanying] 真实搜索入口未命中，最后重试 /s/片名/ 入口")
        for retry_url in preferred_retry:
            try:
                response = self._search_http_request(
                    "GET",
                    retry_url,
                    headers=self._with_referer(headers, referer_url or url),
                    cookies=cookies,
                    attempts=6,
                )
                response.raise_for_status()
            except Exception as exc:
                last_error = exc
                logger.warning(f"[MP115guanying] 真实搜索入口最终重试失败: {retry_url} - {exc}")
                continue
            if self._is_preferred_search_response(response, retry_url, keyword):
                logger.info(f"[MP115guanying] 使用最终重试命中的真实搜索结果页: {getattr(response, 'url', retry_url)}")
                return response
        if best_response is not None:
            return best_response
        raise RuntimeError(f"搜索入口全部失败: {last_error}")

    def _http_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Any] = None,
        cookies: Optional[RequestsCookieJar] = None,
        attempts: int = 3,
        curl_after_failures: int = 0,
    ):
        method = (method or "GET").upper()
        headers = dict(headers or {})
        self._move_cookie_headers_to_jar(headers, cookies)
        proxy_url = self._normalized_proxy_url(self._search_proxy)
        proxy_config = self._proxy_config(proxy_url)
        override_ips = self._resolve_ips_for_url(url)
        if override_ips:
            last_error = None
            for index, override_ip in enumerate(override_ips, 1):
                try:
                    return self._curl_request(
                        method,
                        url,
                        headers=self._headers_with_cookie_jar(headers, cookies),
                        data=data,
                        override_ip=override_ip,
                        override_index=index,
                        override_total=len(override_ips),
                        proxy_url=proxy_url,
                    )
                except Exception as exc:
                    last_error = exc
                    logger.warning(f"[MP115guanying] DNS/IP 覆盖请求失败: {override_ip} - {exc}")
            raise RuntimeError(f"所有 DNS/IP 覆盖均请求失败: {last_error}")
        last_error = None
        attempts = max(1, int(attempts or 1))
        curl_after_failures = max(0, int(curl_after_failures or 0))
        curl_tried = False
        for attempt in range(attempts):
            try:
                if method == "POST":
                    response = requests.post(
                        url,
                        data=data or {},
                        headers=headers,
                        cookies=cookies,
                        proxies=proxy_config,
                        timeout=self._timeout,
                        allow_redirects=True,
                    )
                else:
                    response = requests.get(
                        url,
                        headers=headers,
                        cookies=cookies,
                        proxies=proxy_config,
                        timeout=self._timeout,
                        allow_redirects=True,
                    )
                self._merge_response_cookies(response, cookies)
                return response
            except Exception as exc:
                last_error = exc
                if method == "GET" and curl_after_failures and not curl_tried and attempt + 1 >= curl_after_failures:
                    curl_tried = True
                    try:
                        logger.info(f"[MP115guanying] requests 请求失败，提前改用 curl 兜底请求: {url}")
                        return self._curl_request(
                            method,
                            url,
                            headers=self._headers_with_cookie_jar(headers, cookies),
                            data=data,
                            override_ip="",
                            proxy_url=proxy_url,
                        )
                    except Exception as curl_exc:
                        logger.warning(f"[MP115guanying] curl 提前兜底请求失败: {url} - {curl_exc}")
                if attempt >= attempts - 1:
                    break
                headers = dict(headers or {})
                headers["Connection"] = "close"
                time.sleep(0.5 * (attempt + 1))
                continue
        if method == "GET" and not curl_tried:
            try:
                logger.info(f"[MP115guanying] requests 多次失败，改用 curl 兜底请求: {url}")
                return self._curl_request(
                    method,
                    url,
                    headers=self._headers_with_cookie_jar(headers, cookies),
                    data=data,
                    override_ip="",
                    proxy_url=proxy_url,
                )
            except Exception as curl_exc:
                logger.warning(f"[MP115guanying] curl 兜底请求失败: {url} - {curl_exc}")
        raise last_error

    def _curl_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        data: Optional[Any],
        override_ip: str = "",
        override_index: int = 1,
        override_total: int = 1,
        proxy_url: str = "",
    ):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host:
            raise RuntimeError(f"URL 缺少 host: {url}")
        if override_ip:
            logger.info(f"[MP115guanying] 使用 DNS/IP 覆盖({override_index}/{override_total}): {host}:{port} -> {override_ip}")
        marker = "\n__MP115CLOUD_STATUS__:%{http_code}\n__MP115CLOUD_URL__:%{url_effective}\n"
        cmd = [
            "curl",
            "-L",
            "-sS",
            "--http1.1",
            "--compressed",
            "--max-time",
            str(self._timeout),
            "--connect-timeout",
            str(self._timeout),
            "-w",
            marker,
        ]
        if override_ip:
            cmd.extend(["--resolve", f"{host}:{port}:{override_ip}"])
        if override_ip and ":" not in override_ip:
            cmd.append("-4")
        if proxy_url:
            cmd.extend(["--proxy", proxy_url])
        for key, value in (headers or {}).items():
            cmd.extend(["-H", f"{key}: {value}"])
        if method == "POST":
            cmd.extend(["-X", "POST"])
            data_items = data.items() if hasattr(data, "items") else (data or [])
            for key, value in data_items:
                cmd.extend(["--data-urlencode", f"{key}={value}"])
        cmd.append(url)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self._timeout + 5)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "").strip() or f"curl 返回码 {proc.returncode}")
        match = re.search(r"\n__MP115CLOUD_STATUS__:(\d+)\n__MP115CLOUD_URL__:(.*?)\n?$", proc.stdout, re.S)
        if not match:
            raise RuntimeError("curl 返回内容缺少状态标记")
        body = proc.stdout[:match.start()]
        status_code = self._to_int(match.group(1), 0, 0, 999)
        final_url = match.group(2).strip() or url
        return _SimpleResponse(text=body, url=final_url, status_code=status_code)

    @staticmethod
    def _normalized_proxy_url(value: str) -> str:
        proxy = (value or "").strip()
        if proxy and "://" not in proxy:
            proxy = f"http://{proxy}"
        return proxy

    @staticmethod
    def _proxy_config(proxy_url: str) -> Optional[Dict[str, str]]:
        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}

    @staticmethod
    def _mask_proxy_url(value: str) -> str:
        proxy = MP115guanying._normalized_proxy_url(value)
        try:
            parsed = urlsplit(proxy)
            if parsed.username or parsed.password:
                netloc = parsed.hostname or ""
                if parsed.port:
                    netloc = f"{netloc}:{parsed.port}"
                return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        except Exception:
            pass
        return proxy

    def _build_search_headers(self, url: str = "") -> Dict[str, str]:
        headers = self._default_search_headers(url)
        headers.update(self._parse_headers(self._search_headers))
        if self._search_cookie and not self._has_header(headers, "Cookie"):
            headers["Cookie"] = self._search_cookie
        return headers

    def _build_search_cookiejar(self, headers: Dict[str, str]) -> RequestsCookieJar:
        cookies = RequestsCookieJar()
        self._move_cookie_headers_to_jar(headers, cookies)
        return cookies

    def _resolve_ips_for_url(self, url: str) -> List[str]:
        parsed = urlsplit(url or "")
        host = (parsed.hostname or "").lower()
        if not host:
            return []
        return self._parse_resolve_overrides(self._resolve_overrides).get(host, [])

    @staticmethod
    def _with_referer(headers: Dict[str, str], referer: str) -> Dict[str, str]:
        next_headers = dict(headers or {})
        if referer:
            next_headers["Referer"] = referer
        return next_headers

    @staticmethod
    def _merge_response_cookies(response: Any, cookies: Optional[RequestsCookieJar]):
        if cookies is None:
            return
        response_cookies = getattr(response, "cookies", None)
        if not response_cookies:
            return
        try:
            cookies.update(response_cookies)
        except Exception:
            for name, value in MP115guanying._iter_cookie_pairs(response_cookies):
                MP115guanying._set_cookie_value(cookies, name, value)

    @staticmethod
    def _move_cookie_headers_to_jar(headers: Dict[str, str], cookies: Optional[RequestsCookieJar]):
        if cookies is None:
            return
        cookie_header_keys = [
            key
            for key in list((headers or {}).keys())
            if str(key).lower() == "cookie"
        ]
        for key in cookie_header_keys:
            cookie_text = headers.pop(key, "") or ""
            for name, value in MP115guanying._parse_cookie_pairs(cookie_text).items():
                MP115guanying._set_cookie_value(cookies, name, value)

    def _headers_with_cookie_jar(
        self,
        headers: Optional[Dict[str, str]],
        cookies: Optional[RequestsCookieJar],
    ) -> Dict[str, str]:
        next_headers = dict(headers or {})
        cookie_header = self._cookiejar_to_header(cookies)
        if cookie_header and not self._has_header(next_headers, "Cookie"):
            next_headers["Cookie"] = cookie_header
        return next_headers

    @staticmethod
    def _parse_cookie_pairs(cookie_text: str) -> Dict[str, str]:
        pairs = {}
        for part in (cookie_text or "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                pairs[name] = value.strip()
        return pairs

    @staticmethod
    def _set_cookie_value(cookies: RequestsCookieJar, name: str, value: str):
        try:
            cookies.set(name, value)
        except Exception:
            try:
                cookies[name] = value
            except Exception:
                pass

    @staticmethod
    def _iter_cookie_pairs(cookies: Any) -> Iterable[Tuple[str, str]]:
        if hasattr(cookies, "items"):
            try:
                for name, value in cookies.items():
                    yield str(name), str(value)
                return
            except Exception:
                pass
        try:
            for cookie in cookies:
                name = getattr(cookie, "name", "")
                value = getattr(cookie, "value", "")
                if name:
                    yield str(name), str(value)
        except Exception:
            return

    @staticmethod
    def _cookiejar_to_header(cookies: Optional[RequestsCookieJar]) -> str:
        if not cookies:
            return ""
        return "; ".join(
            f"{name}={value}"
            for name, value in MP115guanying._iter_cookie_pairs(cookies)
            if name
        )

    def _extract_search_form(
        self,
        html_text: str,
        base_url: str,
        keyword: str,
    ) -> Optional[Tuple[str, str, Dict[str, str], str]]:
        parser = _SearchFormHTMLParser()
        try:
            parser.feed(html_text or "")
        except Exception:
            pass
        form = parser.best_form()
        if not form:
            return None
        field = form.get("field") or self._post_field or "keyword"
        if not field:
            return None
        data = dict(form.get("data") or {})
        data[field] = keyword
        method = (form.get("method") or self._search_method or "GET").upper()
        if method not in ("GET", "POST"):
            method = "GET"
        action = form.get("action") or base_url
        return method, urljoin(base_url or "", action), data, field

    def _fallback_search_urls(self, base_url: str, field: str, keyword: str) -> List[str]:
        parsed = urlsplit(base_url or "")
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        fields = []
        for item in [field, "q", "keyword", "search", "wd"]:
            if item and item not in fields:
                fields.append(item)
        urls = []
        if origin and keyword:
            urls.append(urljoin(origin + "/", f"/s/{quote(str(keyword).strip(), safe='')}/"))
        for item in fields:
            urls.append(self._append_query(base_url, {item: keyword}))
        if origin:
            for item in ["q", "keyword", field or "keyword"]:
                urls.append(self._append_query(urljoin(origin + "/", "/search"), {item: keyword}))
        deduped = []
        seen = set()
        for item in urls:
            if item and item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped[:8]

    def _search_response_score(self, response: Any, keyword: str) -> int:
        text = getattr(response, "text", "") or ""
        url = getattr(response, "url", "") or ""
        candidates = self._candidate_from_text(text, base_url=url)
        direct_count = len([item for item in candidates if item.get("magnet")])
        detail_count = len([item for item in candidates if item.get("detail_url")])
        keyword_hits = 0
        for token in re.split(r"[\s._:+/\-]+", keyword or ""):
            token = token.strip()
            if len(token) >= 2 and token.lower() in text.lower():
                keyword_hits += 1
        path = (urlsplit(url).path or "").lower()
        path_bonus = 5 if "/search" in path or path.startswith("/s/") else 0
        exact_keyword_bonus = 5000 if self._response_contains_keyword(response, keyword) else 0
        return direct_count * 100 + detail_count * 8 + keyword_hits * 3 + path_bonus + exact_keyword_bonus

    @staticmethod
    def _is_preferred_search_url(value: str) -> bool:
        return (urlsplit(value or "").path or "").lower().startswith("/s/")

    def _is_preferred_search_response(self, response: Any, requested_url: str, keyword: str) -> bool:
        final_url = getattr(response, "url", "") or requested_url or ""
        requested_path = (urlsplit(requested_url or "").path or "").lower()
        final_path = (urlsplit(final_url or "").path or "").lower()
        if not (requested_path.startswith("/s/") or final_path.startswith("/s/")):
            return False
        if final_path in ("", "/"):
            return False
        if self._response_has_matching_candidate(response, keyword):
            return True
        return self._response_contains_keyword(response, keyword)

    def _is_usable_search_response(self, response: Any, requested_url: str, keyword: str) -> bool:
        if response is None:
            return False
        if self._is_preferred_search_response(response, requested_url, keyword):
            return True
        if self._response_has_matching_candidate(response, keyword):
            return True
        final_url = getattr(response, "url", "") or requested_url or ""
        final_path = (urlsplit(final_url or "").path or "").lower()
        if final_path in ("", "/") and urlsplit(final_url or "").query:
            return False
        return False

    def _response_has_matching_candidate(self, response: Any, keyword: str) -> bool:
        text = getattr(response, "text", "") or ""
        final_url = getattr(response, "url", "") or ""
        candidates = self._candidate_from_text(text, base_url=final_url)
        for item in candidates:
            title = (item.get("title") or "").strip()
            if not title or self._is_noise_detail_title(title):
                continue
            if self._title_reject_reason(keyword, title):
                continue
            if self._score_title(keyword, title) >= self._min_score:
                return True
            if self._has_title_evidence(keyword, title):
                return True
        return False

    @staticmethod
    def _response_contains_keyword(response: Any, keyword: str) -> bool:
        text = (getattr(response, "text", "") or "").lower()
        keyword = (keyword or "").strip().lower()
        return bool(keyword and keyword in text)

    @staticmethod
    def _append_query(url: str, params: Dict[str, Any]) -> str:
        parsed = urlsplit(url or "")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for key, value in (params or {}).items():
            if key:
                query[str(key)] = str(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

    @staticmethod
    def _has_keyword_placeholder(value: str) -> bool:
        return any(
            MP115guanying._template_uses_placeholder(value, key)
            for key in ("keyword", "raw_keyword", "title", "media_title", "search_title", "torrent_title")
        )

    @staticmethod
    def _has_header(headers: Dict[str, str], key: str) -> bool:
        target = (key or "").lower()
        return any(str(existing).lower() == target for existing in (headers or {}))

    @staticmethod
    def _parse_resolve_overrides(value: str) -> Dict[str, List[str]]:
        overrides = {}
        for line in (value or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                host, ips = line.split("=", 1)
            else:
                parts = line.split()
                if len(parts) < 2:
                    continue
                host, ips = parts[0], " ".join(parts[1:])
            host = host.strip()
            parsed = urlsplit(host if "://" in host else f"//{host}")
            normalized_host = (parsed.hostname or host.split(":", 1)[0]).strip().lower()
            parsed_ips = [
                item.strip().strip(",")
                for item in re.split(r"[\s,]+", ips or "")
                if item.strip().strip(",")
            ]
            if normalized_host and parsed_ips:
                overrides[normalized_host] = parsed_ips
        return overrides

    @staticmethod
    def _parse_headers(value: str) -> Dict[str, str]:
        value = (value or "").strip()
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            pass
        headers = {}
        for line in value.splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            headers[key.strip()] = val.strip()
        return headers

    @staticmethod
    def _default_search_headers(url: str = "") -> Dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        try:
            parsed = urlsplit(url or "")
            if parsed.scheme and parsed.netloc:
                headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        except Exception:
            pass
        return headers

    @staticmethod
    def _describe_url_network(url: str) -> str:
        try:
            parsed = urlsplit(url or "")
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not host:
                return f"URL={url}, host=empty"
            addresses = []
            for family, _socktype, _proto, _canonname, sockaddr in socket.getaddrinfo(host, port):
                address = sockaddr[0]
                label = "IPv6" if family == socket.AF_INET6 else "IPv4"
                item = f"{label} {address}"
                if item not in addresses:
                    addresses.append(item)
            return f"URL={url}, host={host}, port={port}, DNS={addresses or ['empty']}"
        except Exception as exc:
            return f"URL={url}, DNS解析失败: {exc}"

    @staticmethod
    def _parse_subscribe_origin(origin: str) -> Dict[str, Any]:
        if not origin or not origin.startswith("Subscribe|"):
            return {}
        try:
            return json.loads(origin.split("|", 1)[1])
        except Exception:
            return {}

    @staticmethod
    def _subscribe_origin(subscribe: Any) -> str:
        subscribe_id = getattr(subscribe, "id", None)
        if not subscribe_id:
            return ""
        return "Subscribe|" + json.dumps({"id": subscribe_id}, ensure_ascii=False)

    @classmethod
    def _normalize_magnet(cls, magnet: Any) -> str:
        magnets = cls._extract_magnets_from_text(magnet)
        return magnets[0] if magnets else ""

    @classmethod
    def _extract_magnets_from_text(cls, text: Any) -> List[str]:
        if not text:
            return []
        results = []
        seen = set()
        for variant in cls._text_decode_variants(str(text)):
            found_values = cls._magnet_regex.findall(variant)
            found_full_magnet = bool(found_values)
            for found in found_values:
                magnet = cls._clean_magnet(found)
                if magnet and magnet not in seen:
                    seen.add(magnet)
                    results.append(magnet)
            if found_full_magnet:
                continue
            for hash_value in cls._btih_hash_regex.findall(variant):
                magnet = f"magnet:?xt=urn:btih:{hash_value}"
                if magnet not in seen:
                    seen.add(magnet)
                    results.append(magnet)
        return results

    @classmethod
    def _text_decode_variants(cls, text: Any) -> List[str]:
        if not text:
            return []
        variants = []
        queue = [str(text)]
        seen = set()
        while queue and len(seen) < 40:
            value = queue.pop(0)
            if value in seen:
                continue
            seen.add(value)
            variants.append(value)
            next_values = [
                unescape(value),
                value.replace("\\/", "/"),
                cls._decode_js_escapes(value),
                unquote(value),
                unquote_plus(value),
            ]
            for next_value in next_values:
                if next_value and next_value not in seen:
                    queue.append(next_value)
        for value in list(variants):
            lower = value.lower()
            if "magnet" in lower or "btih" in lower:
                continue
            for token in cls._base64_candidate_regex.findall(value):
                decoded = cls._decode_base64_token(token)
                if not decoded:
                    continue
                decoded_lower = decoded.lower()
                if ("magnet" in decoded_lower or "btih" in decoded_lower) and decoded not in seen:
                    seen.add(decoded)
                    variants.append(decoded)
        return variants

    @staticmethod
    def _decode_js_escapes(value: str) -> str:
        if not value or ("\\" not in value):
            return value

        def replace_unicode(match):
            try:
                return chr(int(match.group(1), 16))
            except Exception:
                return match.group(0)

        def replace_hex(match):
            try:
                return chr(int(match.group(1), 16))
            except Exception:
                return match.group(0)

        value = re.sub(r"\\u([0-9A-Fa-f]{4})", replace_unicode, value)
        value = re.sub(r"\\x([0-9A-Fa-f]{2})", replace_hex, value)
        return value.replace("\\/", "/")

    @staticmethod
    def _decode_base64_token(token: str) -> str:
        token = (token or "").strip()
        if len(token) < 24:
            return ""
        normalized = token.replace("-", "+").replace("_", "/")
        normalized += "=" * (-len(normalized) % 4)
        try:
            raw = base64.b64decode(normalized, validate=False)
            return raw.decode("utf-8", errors="ignore")
        except Exception:
            return ""

    @classmethod
    def _clean_magnet(cls, magnet: str) -> str:
        if not magnet:
            return ""
        text = unescape(str(magnet).strip())
        found = cls._magnet_regex.search(text)
        if not found:
            return ""
        return found.group(0).rstrip(".,;，。)）]】}'\"")

    @classmethod
    def _magnet_page_hints(cls, text: str, final_url: str = "") -> str:
        variants = cls._text_decode_variants(text or "")
        joined = "\n".join(variants[:8])
        lower = joined.lower()
        keywords = [
            "复制磁力",
            "data-clipboard-text",
            "clipboard",
            "magnet",
            "btih",
            "seed_id",
            "link_start",
            "redirect_to",
            "window.",
            "fetch",
            "ajax",
            "atob",
            "base64",
            "hash",
        ]
        found_keywords = [keyword for keyword in keywords if keyword.lower() in lower]
        snippets = []
        compact = re.sub(r"\s+", " ", joined)
        compact_lower = compact.lower()
        for keyword in ("复制磁力", "clipboard", "magnet", "btih", "seed_id", "link_start", "hash"):
            pos = compact_lower.find(keyword.lower())
            if pos >= 0:
                start = max(0, pos - 70)
                end = min(len(compact), pos + 170)
                snippets.append(compact[start:end])
            if len(snippets) >= 3:
                break
        return f"url={final_url}, 命中={found_keywords or ['none']}, 片段={snippets or ['none']}"

    @staticmethod
    def _normalize_detail_url(value: Any, base_url: str = "") -> str:
        if not value:
            return ""
        raw = unescape(str(value).strip())
        if not raw or raw.startswith("#"):
            return ""
        lowered = raw.lower()
        if lowered.startswith(("javascript:", "mailto:", "tel:", "magnet:")):
            return ""
        url = urljoin(base_url or "", raw)
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        if base_url:
            base_host = (urlsplit(base_url).hostname or "").lower()
            current_host = (parsed.hostname or "").lower()
            if base_host and current_host and current_host != base_host:
                return ""
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|mp4|mkv|avi|zip|rar|7z)(?:$|\?)", parsed.path, re.I):
            return ""
        return url

    @staticmethod
    def _get_by_path(data: Any, path: str) -> Any:
        if not path:
            return data
        current = data
        for part in path.split("."):
            if current is None:
                return None
            part = part.strip()
            if not part:
                continue
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                index = int(part)
                current = current[index] if 0 <= index < len(current) else None
            else:
                return None
        return current

    @classmethod
    def _walk_values(cls, data: Any) -> Iterable[Any]:
        if isinstance(data, dict):
            for value in data.values():
                yield from cls._walk_values(value)
        elif isinstance(data, list):
            for value in data:
                yield from cls._walk_values(value)
        else:
            yield data

    def _search_template_values(
        self,
        keyword: str,
        template_values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        values = dict(template_values or {})
        values.update({"keyword": self._encode_keyword_for_search_url(keyword or ""), "raw_keyword": keyword or ""})
        return values

    def _encode_keyword_for_search_url(self, keyword: str) -> str:
        template = self._search_url or ""
        parsed = urlsplit(template)
        if "{keyword}" in (parsed.path or ""):
            return quote(keyword or "", safe="")
        return quote_plus(keyword or "")

    def _fallback_template_values(
        self,
        template_values: Optional[Dict[str, Any]],
        fallback_keyword: str,
    ) -> Dict[str, Any]:
        values = dict(template_values or {})
        for key in ("title", "media_title", "search_title"):
            if self._template_uses_placeholder(self._search_url, key):
                values[key] = fallback_keyword
        return values

    @staticmethod
    def _template_uses_placeholder(template: str, key: str) -> bool:
        return "{" + key + "}" in (template or "")

    @staticmethod
    def _render_template(template: str, values: Dict[str, Any]) -> str:
        rendered = template or ""
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", str(value or ""))
        return rendered

    @staticmethod
    def _title_reject_reason(target: str, candidate: str) -> str:
        if not target or not candidate:
            return ""
        reasons = []
        has_possible_match = False
        for target_item in MP115guanying._target_title_values(target):
            reason = MP115guanying._single_title_reject_reason(target_item, candidate)
            if reason and ("系列部数不一致" in reason or "候选缺少明确系列部数" in reason):
                return reason
            if not reason:
                has_possible_match = True
                continue
            reasons.append(reason)
        if has_possible_match:
            return ""
        return reasons[0] if reasons else ""

    @staticmethod
    def _single_title_reject_reason(target: str, candidate: str) -> str:
        target_years = MP115guanying._title_years(target)
        candidate_years = MP115guanying._title_years(candidate)
        if target_years and candidate_years and not target_years.intersection(candidate_years):
            return f"年份不一致({','.join(sorted(target_years))}!={','.join(sorted(candidate_years))})"
        return MP115guanying._series_part_reject_reason(target, candidate, target_years, candidate_years)

    @staticmethod
    def _score_title(target: str, candidate: str) -> float:
        if MP115guanying._title_reject_reason(target, candidate):
            return 0
        target_values = MP115guanying._target_title_values(target)
        if not target_values:
            return 0
        return max(MP115guanying._score_single_title(item, candidate) for item in target_values)

    @staticmethod
    def _has_title_evidence(target: str, candidate: str) -> bool:
        if MP115guanying._title_reject_reason(target, candidate):
            return False
        candidate_text = candidate or ""
        candidate_norm = MP115guanying._strip_years(MP115guanying._normalize_title(candidate_text))
        if not candidate_norm:
            return False
        candidate_cjk = "".join(re.findall(r"[\u4e00-\u9fff]", candidate_text))
        candidate_latin = {
            token
            for token in re.findall(r"[a-z0-9]+", candidate_text.lower())
            if len(token) >= 4 and not token.isdigit()
        }
        for target_item in MP115guanying._target_title_values(target):
            target_norm = MP115guanying._strip_years(MP115guanying._normalize_title(target_item))
            if len(target_norm) >= 4 and (target_norm in candidate_norm or candidate_norm in target_norm):
                return True
            target_cjk = "".join(re.findall(r"[\u4e00-\u9fff]", target_item))
            if target_cjk and candidate_cjk:
                target_grams = set(MP115guanying._char_ngrams(target_cjk, 2))
                candidate_grams = set(MP115guanying._char_ngrams(candidate_cjk, 2))
                overlap = target_grams.intersection(candidate_grams)
                if len(overlap) >= 2 and MP115guanying._dice_similarity(target_grams, candidate_grams) >= 0.67:
                    return True
            target_latin = {
                token
                for token in re.findall(r"[a-z0-9]+", target_item.lower())
                if len(token) >= 4 and not token.isdigit()
            }
            if target_latin and candidate_latin and target_latin.intersection(candidate_latin):
                return True
        return False

    @staticmethod
    def _score_single_title(target: str, candidate: str) -> float:
        if MP115guanying._title_reject_reason(target, candidate):
            return 0
        target_norm = MP115guanying._normalize_title(target)
        candidate_norm = MP115guanying._normalize_title(candidate)
        if not target_norm or not candidate_norm:
            return 0
        target_base = MP115guanying._strip_years(target_norm)
        candidate_base = MP115guanying._strip_years(candidate_norm)
        if target_base and target_base == candidate_base:
            return 1
        if target_base and len(target_base) >= 4 and target_base in candidate_base:
            return 0.98
        ratio = SequenceMatcher(None, target_base, candidate_base).ratio()
        bigram_score = MP115guanying._dice_similarity(
            MP115guanying._char_ngrams(target_base),
            MP115guanying._char_ngrams(candidate_base),
        )
        token_score = MP115guanying._dice_similarity(
            MP115guanying._title_tokens(target),
            MP115guanying._title_tokens(candidate),
        )
        score = max(ratio, bigram_score, token_score)
        target_years = set(re.findall(r"(?:19|20)\d{2}", target_norm))
        candidate_years = set(re.findall(r"(?:19|20)\d{2}", candidate_norm))
        if target_years and candidate_years:
            if target_years.intersection(candidate_years):
                score = min(1, score + 0.08)
        return score

    @staticmethod
    def _title_years(value: str) -> set:
        return set(re.findall(r"(?:19|20)\d{2}", value or ""))

    @staticmethod
    def _series_part_reject_reason(
        target: str,
        candidate: str,
        target_years: Optional[set] = None,
        candidate_years: Optional[set] = None,
    ) -> str:
        target_infos = MP115guanying._series_title_infos(target)
        candidate_infos = MP115guanying._series_title_infos(candidate)
        if not target_infos or not candidate_infos:
            return ""
        target_years = target_years if target_years is not None else MP115guanying._title_years(target)
        candidate_years = candidate_years if candidate_years is not None else MP115guanying._title_years(candidate)
        for target_info in target_infos:
            for candidate_info in candidate_infos:
                if target_info.get("base") != candidate_info.get("base"):
                    continue
                target_part = target_info.get("part")
                candidate_part = candidate_info.get("part")
                if target_part is None:
                    if candidate_part and candidate_part > 1:
                        return f"系列部数不一致(目标首部/无编号, 候选第{candidate_part}部)"
                    continue
                if target_part == 1:
                    if candidate_part and candidate_part != 1:
                        return f"系列部数不一致(目标第1部, 候选第{candidate_part}部)"
                    continue
                if candidate_part is not None:
                    if candidate_part != target_part:
                        return f"系列部数不一致(目标第{target_part}部, 候选第{candidate_part}部)"
                    continue
                if target_years and candidate_years and target_years.intersection(candidate_years):
                    continue
                return f"候选缺少明确系列部数(目标第{target_part}部)"
        return ""

    @staticmethod
    def _series_title_infos(value: str) -> List[Dict[str, Any]]:
        text = value or ""
        explicit = []
        for match in re.finditer(
            r"([\u4e00-\u9fff][\u4e00-\u9fff·・:：]{1,30})[\s._-]*([1-9]\d?)(?=$|[\s._:：\-()\[\]（）【】《》])",
            text,
        ):
            base = MP115guanying._normalize_series_base(match.group(1))
            part = MP115guanying._to_series_part(match.group(2))
            if base and part:
                explicit.append({"base": base, "part": part})
        for match in re.finditer(
            r"([\u4e00-\u9fff][\u4e00-\u9fff·・:：]{1,30})[\s._-]*([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩivxIVX]+)(?=$|[\s._:：\-()\[\]（）【】《》])",
            text,
        ):
            base = MP115guanying._normalize_series_base(match.group(1))
            part = MP115guanying._to_series_part(match.group(2))
            if base and part:
                explicit.append({"base": base, "part": part})
        for match in re.finditer(
            r"([\u4e00-\u9fff][\u4e00-\u9fff·・:：]{1,30})[\s._-]*(?:第)?([一二三四五六七八九十两]+)部",
            text,
        ):
            base = MP115guanying._normalize_series_base(match.group(1))
            part = MP115guanying._to_series_part(match.group(2))
            if base and part:
                explicit.append({"base": base, "part": part})
        explicit = MP115guanying._dedupe_series_infos(explicit)
        if explicit:
            return explicit
        for item in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            base = MP115guanying._normalize_series_base(item)
            if base:
                return [{"base": base, "part": None}]
        return []

    @staticmethod
    def _normalize_series_base(value: str) -> str:
        base = re.sub(r"(?:19|20)\d{2}", "", value or "")
        base = re.sub(r"(?:第)?[一二三四五六七八九十两]+部$", "", base)
        base = re.sub(r"第$", "", base)
        base = re.sub(r"[1-9]\d?$", "", base)
        base = re.sub(r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+$", "", base, flags=re.I)
        base = re.sub(r"[\W_]+", "", base, flags=re.U)
        if not base or len(base) < 2:
            return ""
        if base in {
            "中文字幕",
            "中字",
            "简中",
            "繁中",
            "简繁",
            "杜比视界",
            "国语",
            "粤语",
            "磁力",
            "资源",
            "电影",
        }:
            return ""
        return base

    @staticmethod
    def _dedupe_series_infos(values: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        deduped = []
        seen = set()
        for item in values or []:
            key = (item.get("base"), item.get("part"))
            if not key[0] or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _search_keyword_fallbacks(
        keyword: str,
        template_values: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        keyword = (keyword or "").strip()
        variants: List[str] = []
        values = template_values or {}
        target_season = MP115guanying._target_season_number(values) or MP115guanying._season_from_text(keyword)
        if MP115guanying._is_tv_context(values) or target_season:
            variants.extend(MP115guanying._tv_search_keyword_fallbacks(keyword, values, target_season))
        for value in MP115guanying._target_title_values(keyword) or [keyword]:
            for info in MP115guanying._series_title_infos(value):
                base = info.get("base") or ""
                part = info.get("part")
                if not base:
                    continue
                if part:
                    label = MP115guanying._series_part_label(part)
                    if label:
                        variants.append(f"{base}第{label}部")
                variants.append(base)

        deduped = []
        seen = {keyword}
        for item in variants:
            item = (item or "").strip()
            cleaned_item = MP115guanying._clean_release_title_for_search(item) if MP115guanying._looks_like_release_title(item) else ""
            if cleaned_item and MP115guanying._normalize_title(cleaned_item) != MP115guanying._normalize_title(item):
                item = cleaned_item
            if not item or item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    @staticmethod
    def _tv_search_keyword_fallbacks(
        keyword: str,
        template_values: Optional[Dict[str, Any]],
        target_season: int,
    ) -> List[str]:
        if not target_season:
            return []
        values = template_values or {}
        bases: List[str] = []
        for value in (
            values.get("media_title"),
            values.get("search_title"),
            values.get("title"),
            keyword,
        ):
            base = MP115guanying._tv_keyword_base(value)
            if base:
                bases.append(base)
        for target_item in MP115guanying._target_title_values(values.get("target_title", "")):
            base = MP115guanying._tv_keyword_base(target_item)
            if base:
                bases.append(base)

        variants: List[str] = []
        label = MP115guanying._series_part_label(target_season) or str(target_season)
        for base in MP115guanying._dedupe_title_values(bases):
            variants.extend([
                f"{base} 第{label}季",
                f"{base}第{label}季",
                f"{base} S{target_season:02d}",
                f"{base}S{target_season:02d}",
                f"{base} Season {target_season}",
                base,
            ])
        return variants

    @staticmethod
    def _tv_keyword_base(value: Any) -> str:
        text = MP115guanying._clean_release_title_for_search(value) or str(value or "").strip()
        if not text:
            return ""
        text = MP115guanying._strip_tv_season_marker(text)
        text = re.sub(r"\b(?:19|20)\d{2}\b", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" -_./|")
        return text

    @staticmethod
    def _clean_release_title_for_search(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        chinese_prefix = re.match(
            r"\s*([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9·・:：\- ]{1,40}?)(?=$|[\[【(（._\s])",
            text,
        )
        if chinese_prefix:
            prefix = re.sub(r"\s+", " ", chinese_prefix.group(1)).strip(" -_./|")
            if prefix and MP115guanying._normalize_title(prefix) not in {
                "中文字幕",
                "中字",
                "简繁英字幕",
                "国语配音中文字幕",
                "杜比视界",
            }:
                return MP115guanying._strip_tv_season_marker(prefix) or prefix
        cleaned = re.sub(r"[\[\]【】()（）]", " ", text)
        cleaned = re.sub(r"[._]+", " ", cleaned)
        cleaned = re.sub(r"@[\w.-]+$", "", cleaned)
        parts = re.split(
            r"\b(?:"
            r"S\d{1,2}(?:E\d{1,3})?|Season\s*\d{1,2}|"
            r"(?:19|20)\d{2}|2160p|1080p|720p|4k|uhd|"
            r"web[- ]?dl|webrip|blu[- ]?ray|hdtv|"
            r"h\.?26[45]|x26[45]|hevc|avc|aac|ddp\d*(?:\.\d)?|atmos|"
            r"dolby|vision|dv|hdr|repack|proper|pure|hdsweb|blacktv|colortv|colorweb"
            r")\b",
            cleaned,
            maxsplit=1,
            flags=re.I,
        )
        cleaned = parts[0] if parts else cleaned
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_./|")
        return cleaned

    @staticmethod
    def _looks_like_release_title(value: Any) -> bool:
        text = str(value or "")
        return bool(re.search(
            r"@[\w.-]+$|[._].*[._]|\b("
            r"2160p|1080p|720p|4k|uhd|web[- .]?dl|webrip|blu[- .]?ray|hdtv|"
            r"h\.?26[45]|x26[45]|hevc|avc|ddp\d*(?:\.\d)?|atmos|dv|hdr|"
            r"repack|proper|hdsweb|blacktv|colortv|colorweb"
            r")\b",
            text,
            re.I,
        ))

    @staticmethod
    def _series_part_label(value: Any) -> str:
        try:
            part = int(value)
        except Exception:
            return ""
        if part <= 0:
            return ""
        digits = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
        if part < 10:
            return digits[part]
        if part == 10:
            return "十"
        if part < 20:
            return "十" + digits[part % 10]
        if part < 100:
            ten, one = divmod(part, 10)
            return digits[ten] + "十" + (digits[one] if one else "")
        return str(part)

    @staticmethod
    def _to_series_part(value: Any) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        if text.isdigit():
            return int(text)
        roman = {
            "Ⅰ": 1,
            "Ⅱ": 2,
            "Ⅲ": 3,
            "Ⅳ": 4,
            "Ⅴ": 5,
            "Ⅵ": 6,
            "Ⅶ": 7,
            "Ⅷ": 8,
            "Ⅸ": 9,
            "Ⅹ": 10,
            "i": 1,
            "ii": 2,
            "iii": 3,
            "iv": 4,
            "v": 5,
            "vi": 6,
            "vii": 7,
            "viii": 8,
            "ix": 9,
            "x": 10,
        }
        if text.lower() in roman:
            return roman[text.lower()]
        if text in roman:
            return roman[text]
        numerals = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if text == "十":
            return 10
        if text.startswith("十"):
            return 10 + numerals.get(text[1:], 0)
        if text.endswith("十"):
            return numerals.get(text[:-1], 0) * 10
        if "十" in text:
            left, right = text.split("十", 1)
            return numerals.get(left, 1) * 10 + numerals.get(right, 0)
        return numerals.get(text, 0)

    @staticmethod
    def _target_title_values(target: str) -> List[str]:
        values = []
        for item in re.split(r"\s*\|\|\s*|\n+", target or ""):
            item = item.strip()
            if item:
                values.append(item)
        return MP115guanying._dedupe_title_values(values)

    @staticmethod
    def _strip_years(value: str) -> str:
        return re.sub(r"(?:19|20)\d{2}", "", value or "")

    @staticmethod
    def _char_ngrams(value: str, size: int = 2) -> List[str]:
        value = value or ""
        if len(value) <= size:
            return [value] if value else []
        return [value[index:index + size] for index in range(0, len(value) - size + 1)]

    @staticmethod
    def _title_tokens(value: str) -> List[str]:
        return [
            token.lower()
            for token in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", value or "")
            if token
        ]

    @staticmethod
    def _dice_similarity(left: Iterable[str], right: Iterable[str]) -> float:
        left_items = list(left or [])
        right_items = list(right or [])
        if not left_items or not right_items:
            return 0
        left_set = set(left_items)
        right_set = set(right_items)
        return (2 * len(left_set.intersection(right_set))) / (len(left_set) + len(right_set))

    @staticmethod
    def _is_noise_detail_title(title: str) -> bool:
        normalized = MP115guanying._normalize_title(title)
        if len(normalized) < 4:
            return True
        if normalized.isdigit():
            return True
        return normalized in {
            "首页",
            "电影",
            "动漫",
            "剧集",
            "百度",
            "夸克",
            "迅雷",
            "阿里",
            "磁力",
            "磁力链接",
            "复制磁力",
            "迅雷高速下载",
            "网盘离线",
            "其他软件",
            "点击加入tg群",
            "上一页",
            "下一页",
            "最近更新",
            "上映时间",
            "豆瓣评分",
            "近期热门",
        }

    @staticmethod
    def _normalize_title(value: str) -> str:
        value = (value or "").lower()
        value = re.sub(r"[\W_]+", "", value, flags=re.U)
        return value

    @staticmethod
    def _media_type_text(value: Any) -> str:
        if value is None:
            return ""
        nested = getattr(value, "value", None)
        if nested is not None and nested is not value:
            return MP115guanying._media_type_text(nested)
        return str(value or "").strip()

    @staticmethod
    def _is_tv_media_type(value: Any) -> bool:
        text = MP115guanying._media_type_text(value).lower()
        return text in {"电视剧", "剧集", "tv", "show", "series"} or "电视剧" in text

    @staticmethod
    def _is_tv_context(values: Optional[Dict[str, Any]]) -> bool:
        values = values or {}
        if MP115guanying._is_tv_media_type(values.get("media_type")):
            return True
        return bool(values.get("is_tv") or values.get("season"))

    @staticmethod
    def _tv_search_title(base_title: Any, subscribe_title: Any, season: int) -> str:
        base = str(base_title or "").strip()
        subscribed = str(subscribe_title or "").strip()
        if not season:
            return base or subscribed
        if subscribed and MP115guanying._season_from_text(subscribed) == season:
            return subscribed
        if base and MP115guanying._season_from_text(base) == season:
            return base
        clean_base = MP115guanying._strip_tv_season_marker(base) or MP115guanying._strip_tv_season_marker(subscribed)
        clean_base = clean_base or base or subscribed
        return f"{clean_base} S{season:02d}".strip()

    @staticmethod
    def _strip_tv_season_marker(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = re.sub(r"(?<![a-z0-9])s(?:eason)?[\s._-]*\d{1,2}(?!\d)", " ", text, flags=re.I)
        text = re.sub(r"第\s*[一二三四五六七八九十两\d]{1,3}\s*季", " ", text)
        return re.sub(r"\s+", " ", text).strip(" -_./|")

    @staticmethod
    def _target_season_number(values: Optional[Dict[str, Any]]) -> int:
        values = values or {}
        for key in ("season", "season_number", "season_index"):
            parsed = MP115guanying._season_number(values.get(key))
            if parsed:
                return parsed
        return 0

    @staticmethod
    def _season_from_text(value: Any) -> int:
        text = str(value or "")
        if not text:
            return 0
        match = re.search(r"(?<![a-z0-9])s(?:eason)?[\s._-]*(\d{1,2})(?!\d)", text, re.I)
        if match:
            return int(match.group(1))
        match = re.search(r"第\s*([一二三四五六七八九十两\d]{1,3})\s*季", text)
        if match:
            return MP115guanying._to_series_part(match.group(1))
        return 0

    @staticmethod
    def _season_number(value: Any) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        if isinstance(value, int):
            return value
        match = re.search(r"(?<![a-z0-9])s(?:eason)?[\s._-]*(\d{1,2})(?!\d)", text, re.I)
        if match:
            return int(match.group(1))
        match = re.search(r"第\s*([一二三四五六七八九十两\d]{1,3})\s*季", text)
        if match:
            return MP115guanying._to_series_part(match.group(1))
        match = re.search(r"\d{1,2}", text)
        if match:
            return int(match.group(0))
        return MP115guanying._to_series_part(text)

    @staticmethod
    def _tv_season_pack_allowed(title: str, target_season: int = 0) -> bool:
        if not title:
            return False
        seasons = MP115guanying._tv_title_seasons(title)
        if target_season and seasons:
            if seasons != {target_season}:
                return False
        if target_season and not seasons:
            return False
        if MP115guanying._has_multi_season_marker(title):
            return False
        if MP115guanying._has_partial_episode_range_marker(title):
            return False
        if MP115guanying._has_single_episode_marker(title):
            return False
        return bool(seasons or MP115guanying._has_full_season_marker(title))

    @staticmethod
    def _tv_title_matches_target_season(title: str, target_season: int = 0) -> bool:
        if not target_season:
            return False
        seasons = MP115guanying._tv_title_seasons(title)
        if not seasons and target_season == 1:
            return not MP115guanying._has_multi_season_marker(title)
        return seasons == {target_season}

    @staticmethod
    def _tv_title_seasons(title: str) -> set:
        text = title or ""
        seasons = set()
        for match in re.finditer(r"(?<![a-z0-9])s(?:eason)?[\s._-]*(\d{1,2})(?!\d)", text, re.I):
            seasons.add(int(match.group(1)))
        for match in re.finditer(r"第\s*([一二三四五六七八九十两\d]{1,3})\s*季", text):
            part = MP115guanying._to_series_part(match.group(1))
            if part:
                seasons.add(part)
        return seasons

    @staticmethod
    def _has_multi_season_marker(title: str) -> bool:
        lower = (title or "").lower()
        if re.search(r"(?<![a-z0-9])s\d{1,2}\s*[-~至到]\s*s?\d{1,2}(?![a-z0-9])", lower):
            return True
        return len(MP115guanying._tv_title_seasons(title)) > 1

    @staticmethod
    def _has_episode_range_marker(title: str) -> bool:
        return bool(MP115guanying._episode_range_spans(title))

    @staticmethod
    def _has_partial_episode_range_marker(title: str) -> bool:
        spans = MP115guanying._episode_range_spans(title)
        return any(not MP115guanying._looks_like_full_episode_range(start, end) for start, end in spans)

    @staticmethod
    def _episode_range_spans(title: str) -> List[Tuple[int, int]]:
        text = title or ""
        lower = text.lower()
        spans: List[Tuple[int, int]] = []
        for match in re.finditer(
            r"(?<![a-z0-9])(?:s\d{1,2}[\s._-]*)?e(?:p)?[\s._-]*(\d{1,3})"
            r"\s*[-~至到]\s*(?:e(?:p)?[\s._-]*)?(\d{1,3})(?!\d)",
            lower,
        ):
            spans.append((int(match.group(1)), int(match.group(2))))
        for match in re.finditer(r"(?:第|\[第|\s)(\d{1,3})\s*[-~至到]\s*(\d{1,3})\s*集", text):
            spans.append((int(match.group(1)), int(match.group(2))))
        return spans

    @staticmethod
    def _looks_like_full_episode_range(start: int, end: int) -> bool:
        return start == 1 and end >= 6

    @staticmethod
    def _has_single_episode_marker(title: str) -> bool:
        if MP115guanying._has_full_season_marker(title) or MP115guanying._has_episode_range_marker(title):
            return False
        lower = (title or "").lower()
        if re.search(r"(?<![a-z0-9])s\d{1,2}[\s._-]*e\d{1,3}(?!\d)", lower):
            return True
        if re.search(r"(?<![a-z0-9])ep?\d{1,3}(?!\d)", lower):
            return True
        if re.search(r"(?<!全)(?:第\s*)?\d{1,3}\s*集", title or ""):
            return True
        return False

    @staticmethod
    def _has_full_season_marker(title: str) -> bool:
        text = title or ""
        lower = text.lower()
        if any(marker in text for marker in ("全集", "全季", "整季")):
            return True
        if re.search(r"全\s*\d{1,3}\s*集", text):
            return True
        if "complete" in lower:
            return True
        return any(
            MP115guanying._looks_like_full_episode_range(start, end)
            for start, end in MP115guanying._episode_range_spans(text)
        )

    def _resource_quality_allowed(self, title: str) -> bool:
        if self._require_chinese_subtitle and not self._has_chinese_subtitle(title):
            return False
        return self._resolution_rank(title) > 0

    @classmethod
    def _resource_priority_score(cls, title: str) -> Tuple[int, List[str]]:
        score = 0
        hits = []
        rank = cls._resolution_rank(title)
        has_dv = cls._has_dolby_vision(title)
        if rank >= 2 and has_dv:
            score += 3000
            hits.append("4K+杜比视界")
        elif rank >= 2:
            score += 2000
            hits.append("4K")
        elif rank == 1:
            score += 1000
            hits.append("1080P")
        if cls._has_chinese_subtitle(title):
            score += 100
            hits.append("中文字幕")
        return score, hits

    @staticmethod
    def _has_chinese_subtitle(title: str) -> bool:
        text = title or ""
        lower = text.lower()
        if any(keyword in text for keyword in (
            "中文字幕",
            "中字",
            "简中",
            "繁中",
            "简繁",
            "简繁英",
            "简英",
            "繁英",
            "中英",
            "中英双字",
            "中英双语",
            "双语字幕",
            "国英双语",
        )):
            return True
        if "chinese" in lower or any(keyword in lower for keyword in (
            "zh-cn",
            "zh_cn",
            "zh-hans",
            "zh-hant",
            "chs-eng",
            "chs_eng",
            "chs.eng",
            "cht-eng",
            "cht_eng",
            "cht.eng",
        )):
            return True
        if re.search(r"(?<![a-z0-9])(?:chs|cht|chi)(?![a-z0-9])", lower):
            return True
        return False

    @staticmethod
    def _has_dolby_vision(title: str) -> bool:
        text = title or ""
        lower = text.lower()
        if "杜比视界" in text or "dolby vision" in lower or "dovi" in lower:
            return True
        return re.search(r"(?<![a-z0-9])dv(?![a-z0-9])", lower) is not None

    @staticmethod
    def _resolution_rank(title: str) -> int:
        lower = (title or "").lower()
        if re.search(r"(?<!\d)(?:4k|2160p|uhd)(?!\d)", lower):
            return 2
        if re.search(r"(?<!\d)(?:1080p|fhd)(?!\d)", lower):
            return 1
        return 0

    def _quality_score(self, title: str) -> Tuple[int, List[str]]:
        score, hits = self._resource_priority_score(title)
        for keyword, weight in self._parse_weighted_keywords(self._priority_keywords):
            if self._keyword_present(title, keyword):
                score += weight
                if keyword not in hits:
                    hits.append(keyword)
        return score, hits

    def _is_rejected(self, title: str) -> bool:
        if not title:
            return False
        for keyword in self._parse_plain_keywords(self._reject_keywords):
            if self._keyword_present(title, keyword):
                return True
        return False

    @staticmethod
    def _is_duplicate_115_task(message: str) -> bool:
        text = (message or "").lower()
        return (
            "任务已存在" in text
            or "请勿输入重复" in text
            or ("重复" in text and "链接" in text)
            or ("duplicate" in text and ("task" in text or "url" in text or "link" in text))
        )

    @staticmethod
    def _parse_weighted_keywords(value: str) -> List[Tuple[str, int]]:
        rules = []
        for line in (value or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                keyword, weight = line.rsplit(":", 1)
                keyword = keyword.strip()
                try:
                    parsed_weight = int(weight.strip())
                except Exception:
                    parsed_weight = 1
            else:
                keyword = line
                parsed_weight = 1
            if keyword:
                rules.append((keyword, parsed_weight))
        return rules

    @staticmethod
    def _parse_plain_keywords(value: str) -> List[str]:
        return [
            line.strip()
            for line in (value or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    @staticmethod
    def _keyword_present(title: str, keyword: str) -> bool:
        title_lower = (title or "").lower()
        keyword_lower = (keyword or "").lower().strip()
        if not keyword_lower:
            return False
        if re.fullmatch(r"[a-z0-9]+", keyword_lower):
            return re.search(rf"(?<![a-z0-9]){re.escape(keyword_lower)}(?![a-z0-9])", title_lower) is not None
        return keyword_lower in title_lower

    @staticmethod
    def _default_priority_keywords() -> str:
        return "\n".join([
            "杜比视界:30",
            "Dolby Vision:30",
            "DV:22",
            "4K:28",
            "2160p:28",
            "UHD:20",
            "HDR10+:18",
            "HDR:14",
            "标准:14",
            "中文字幕:18",
            "中字:16",
            "简繁英:20",
            "简繁:16",
            "国粤:10",
            "BluRay:8",
            "WEB-DL:6",
            "H.265:6",
            "x265:6",
            "HEVC:6",
        ])

    @staticmethod
    def _default_reject_keywords() -> str:
        return "\n".join([
            "枪版",
            "抢先",
            "TC",
            "TS",
            "CAM",
            "HDTC",
            "HDCAM",
            "清晰版",
            "机翻",
        ])

    @staticmethod
    def _first(*values: Any) -> Any:
        for value in values:
            if value not in (None, ""):
                return value
        return ""

    @staticmethod
    def _to_int(value: Any, default: int, min_value: int, max_value: int) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = default
        return max(min_value, min(max_value, parsed))

    @staticmethod
    def _to_float(value: Any, default: float, min_value: float, max_value: float) -> float:
        try:
            parsed = float(value)
        except Exception:
            parsed = default
        return max(min_value, min(max_value, parsed))


class _MagnetHTMLParser(HTMLParser):
    def __init__(self, magnet_extractor):
        super().__init__(convert_charrefs=True)
        self._magnet_extractor = magnet_extractor
        self.candidates: List[Dict[str, Any]] = []
        self.links: List[Dict[str, Any]] = []
        self._active_anchor: Optional[Dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        attr_map = {str(key).lower(): value or "" for key, value in attrs}
        title = self._pick_title(attr_map)
        magnets = self._find_magnets(" ".join(attr_map.values()))
        if tag.lower() == "a":
            self._active_anchor = {
                "href": attr_map.get("href") or "",
                "magnets": magnets,
                "title": title,
                "text": [],
            }
            return
        for magnet in magnets:
            self.candidates.append({"magnet": magnet, "title": title, "seeders": 0})

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str):
        if self._active_anchor is not None and data:
            self._active_anchor["text"].append(data)

    def handle_endtag(self, tag: str):
        if tag.lower() != "a" or self._active_anchor is None:
            return
        text = " ".join(part.strip() for part in self._active_anchor.get("text", []) if part.strip())
        title = self._active_anchor.get("title") or text
        magnets = self._active_anchor.get("magnets") or []
        if magnets:
            for magnet in magnets:
                self.candidates.append({
                    "magnet": magnet,
                    "title": title,
                    "seeders": 0,
                })
        elif self._active_anchor.get("href"):
            self.links.append({
                "detail_url": self._active_anchor.get("href"),
                "title": title,
                "seeders": 0,
            })
        self._active_anchor = None

    def _find_magnets(self, text: str) -> List[str]:
        return self._magnet_extractor(text or "")

    @staticmethod
    def _pick_title(attrs: Dict[str, str]) -> str:
        for key in ("title", "aria-label", "data-title", "data-name", "alt"):
            value = attrs.get(key)
            if value:
                return unescape(value).strip()
        return ""


class _SearchFormHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: List[Dict[str, Any]] = []
        self._current_form: Optional[Dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        tag = tag.lower()
        attr_map = {str(key).lower(): value or "" for key, value in attrs}
        if tag == "form":
            self._current_form = {
                "method": (attr_map.get("method") or "GET").upper(),
                "action": attr_map.get("action") or "",
                "inputs": [],
            }
            return
        if tag != "input":
            return
        target_form = self._current_form
        if target_form is None:
            target_form = {"method": "GET", "action": "", "inputs": []}
            self.forms.append(target_form)
        target_form["inputs"].append({
            "type": (attr_map.get("type") or "text").lower(),
            "name": attr_map.get("name") or "",
            "value": attr_map.get("value") or "",
            "placeholder": attr_map.get("placeholder") or "",
            "aria": attr_map.get("aria-label") or "",
        })

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str):
        if tag.lower() == "form" and self._current_form is not None:
            self.forms.append(self._current_form)
            self._current_form = None

    def best_form(self) -> Optional[Dict[str, Any]]:
        scored: List[Tuple[int, Dict[str, Any]]] = []
        for form in self.forms:
            inputs = form.get("inputs") or []
            best_input = self._best_input(inputs)
            if not best_input or not best_input.get("name"):
                continue
            data = {
                item.get("name"): item.get("value") or ""
                for item in inputs
                if item.get("name") and item.get("type") in ("hidden", "submit")
            }
            score = best_input.get("score") or 0
            scored.append((score, {
                "method": form.get("method") or "GET",
                "action": form.get("action") or "",
                "field": best_input.get("name"),
                "data": data,
            }))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    @staticmethod
    def _best_input(inputs: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
        candidates = []
        for item in inputs or []:
            input_type = item.get("type") or "text"
            name = item.get("name") or ""
            if input_type in ("hidden", "submit", "button", "checkbox", "radio", "password"):
                continue
            text = " ".join([
                name,
                item.get("placeholder") or "",
                item.get("aria") or "",
            ]).lower()
            score = 1
            if input_type == "search":
                score += 5
            if any(word in text for word in ("search", "keyword", "query", "wd", "q", "关键词", "搜索")):
                score += 5
            if name in ("q", "s", "wd", "kw", "keyword", "query", "search"):
                score += 6
            candidate = dict(item)
            candidate["score"] = score
            candidates.append(candidate)
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.get("score") or 0, reverse=True)
        return candidates[0]


class _SimpleResponse:
    def __init__(self, text: str, url: str, status_code: int, headers: Optional[Dict[str, str]] = None):
        self.text = text or ""
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400 or self.status_code <= 0:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.text or "")


class _Lixian115Client:
    _headers = {
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://115.com",
        "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120.0 Safari/537.36 115Browser/27.0",
        "Referer": "https://115.com/?cid=0&offset=0&mode=wangpan",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(self, cookie: str, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(self._headers)
        self.session.cookies = self._cookiejar_from_string(cookie)

    def add_task(self, magnet: str, wp_path_id: str = "", savepath: str = "") -> Dict[str, Any]:
        if not self.is_login():
            raise RuntimeError("115 Cookie 登录校验失败")
        sign, sign_time = self.get_sign_and_time()
        uid = self.get_uid()
        data = {
            "savepath": savepath or "",
            "wp_path_id": wp_path_id or "",
            "uid": uid,
            "sign": sign,
            "time": sign_time,
            "url": magnet,
        }
        response = self.session.post(
            "https://115.com/web/lixian/?ct=lixian&ac=add_task_url",
            data=data,
            timeout=self.timeout,
            headers={"Host": "115.com"},
        )
        result = self._json(response)
        if result.get("state") is not True:
            raise RuntimeError(result.get("error_msg") or f"115 返回失败: {result}")
        return result

    def get_uid(self) -> int:
        response = self.session.get(
            "https://my.115.com/?ct=ajax&ac=get_user_aq",
            timeout=self.timeout,
            headers={"Host": "my.115.com"},
        )
        result = self._json(response)
        uid = ((result.get("data") or {}).get("uid"))
        if result.get("state") is not True or not uid:
            raise RuntimeError("获取 115 UID 失败")
        return uid

    def get_sign_and_time(self) -> Tuple[str, int]:
        response = self.session.get(
            "https://115.com/?ct=offline&ac=space",
            timeout=self.timeout,
            headers={"Host": "115.com"},
        )
        result = self._json(response)
        if result.get("state") is not True or not result.get("sign") or not result.get("time"):
            raise RuntimeError("获取 115 离线 sign/time 失败")
        return result["sign"], result["time"]

    def is_login(self) -> bool:
        response = self.session.get(
            "https://my.115.com/?ct=guide&ac=status",
            timeout=self.timeout,
            headers={"Host": "my.115.com"},
        )
        result = self._json(response)
        return result.get("state") is True

    @staticmethod
    def _json(response: requests.Response) -> Dict[str, Any]:
        response.raise_for_status()
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError(f"115 返回非 JSON: {response.text[:200]}") from exc

    @staticmethod
    def _cookiejar_from_string(cookie: str) -> RequestsCookieJar:
        cookie_dict = {}
        for item in (cookie or "").replace("\n", " ").split(";"):
            if "=" not in item:
                continue
            key, value = item.strip().split("=", 1)
            if key and value:
                cookie_dict[key.strip()] = value.strip()
        missing = [key for key in ("UID", "CID", "SEID") if key not in cookie_dict]
        if missing:
            raise RuntimeError(f"115 Cookie 缺少字段: {','.join(missing)}")
        return requests.utils.cookiejar_from_dict(cookie_dict)
