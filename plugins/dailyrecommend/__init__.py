import datetime
import math
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


class DailyRecommend(_PluginBase):
    plugin_name = "每日推荐"
    plugin_desc = "根据偏好每天推荐一部电影或电视剧，微信回复 /每日要、/每日换、/每日跳。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "0.1.10"
    plugin_author = "heiyingsky"
    author_url = "https://github.com/heiyingsky"
    plugin_config_prefix = "dailyrecommend_"
    plugin_order = 32
    auth_level = 1

    _enabled = False
    _onlyonce = False
    _proxy = False
    _cron = "0 9 * * *"
    _recommend_hour = 9
    _tmdb_token = ""
    _media_type = "mixed"
    _language_pref = "any"
    _language = "zh-CN"
    _genres: List[str] = []
    _year_start = 2000
    _year_end = 0
    _min_vote = 6.5
    _min_vote_count = 200
    _max_pages = 5
    _exclude_recommended = True
    _exclude_subscribed = True
    _exclude_exists = True
    _notification_type = "Subscribe"
    _history_limit = 1000

    _genre_options = [
        {"title": "动作", "value": "action"},
        {"title": "科幻", "value": "sci-fi"},
        {"title": "恐怖", "value": "horror"},
        {"title": "爱情", "value": "romance"},
        {"title": "悬疑", "value": "mystery"},
        {"title": "喜剧", "value": "comedy"},
        {"title": "犯罪", "value": "crime"},
        {"title": "剧情", "value": "drama"},
        {"title": "动画", "value": "animation"},
        {"title": "纪录", "value": "documentary"},
        {"title": "奇幻", "value": "fantasy"},
        {"title": "冒险", "value": "adventure"}
    ]

    _movie_genres = {
        "action": 28,
        "sci-fi": 878,
        "horror": 27,
        "romance": 10749,
        "mystery": 9648,
        "comedy": 35,
        "crime": 80,
        "drama": 18,
        "animation": 16,
        "documentary": 99,
        "fantasy": 14,
        "adventure": 12
    }
    _tv_genres = {
        "action": 10759,
        "sci-fi": 10765,
        "horror": 9648,
        "romance": 18,
        "mystery": 9648,
        "comedy": 35,
        "crime": 80,
        "drama": 18,
        "animation": 16,
        "documentary": 99,
        "fantasy": 10765,
        "adventure": 10759
    }
    _genre_names = {
        28: "动作",
        10759: "动作冒险",
        878: "科幻",
        10765: "科幻奇幻",
        27: "恐怖",
        10749: "爱情",
        9648: "悬疑",
        35: "喜剧",
        80: "犯罪",
        18: "剧情",
        16: "动画",
        99: "纪录",
        14: "奇幻",
        12: "冒险"
    }

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = bool(config.get("enabled"))
            self._onlyonce = bool(config.get("onlyonce"))
            self._proxy = bool(config.get("proxy"))
            self._recommend_hour = self.__parse_recommend_hour(config)
            self._cron = f"0 {self._recommend_hour} * * *"
            self._tmdb_token = (config.get("tmdb_token") or "").strip()
            self._media_type = config.get("media_type") or "mixed"
            self._language_pref = config.get("language_pref") or "any"
            self._language = config.get("language") or "zh-CN"
            self._genres = config.get("genres") or []
            self._year_start = self.__safe_int(config.get("year_start"), 2000)
            self._year_end = self.__safe_int(config.get("year_end"), 0)
            self._min_vote = self.__safe_float(config.get("min_vote"), 6.5)
            self._min_vote_count = self.__safe_int(config.get("min_vote_count"), 200)
            self._max_pages = max(1, min(self.__safe_int(config.get("max_pages"), 5), 20))
            self._exclude_recommended = bool(config.get("exclude_recommended", True))
            self._exclude_subscribed = bool(config.get("exclude_subscribed", True))
            self._exclude_exists = bool(config.get("exclude_exists", True))
            self._notification_type = config.get("notification_type") or "Subscribe"
            self._history_limit = max(100, min(self.__safe_int(config.get("history_limit"), 1000), 5000))

        if self._onlyonce:
            self._onlyonce = False
            self.__save_last_result(True, "正在执行立即推荐...", status="running")
            self.__update_config()
            self.recommend(force=True)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/每日要",
                "event": EventType.PluginAction,
                "desc": "订阅当前每日推荐",
                "category": "每日推荐",
                "data": {"action": "dailyrecommend_subscribe"}
            },
            {
                "cmd": "/每日换",
                "event": EventType.PluginAction,
                "desc": "换一部每日推荐",
                "category": "每日推荐",
                "data": {"action": "dailyrecommend_change"}
            },
            {
                "cmd": "/每日跳",
                "event": EventType.PluginAction,
                "desc": "今日跳过每日推荐",
                "category": "每日推荐",
                "data": {"action": "dailyrecommend_skip"}
            },
            {
                "cmd": "/dailyrecommend_subscribe",
                "event": EventType.PluginAction,
                "desc": "订阅当前每日推荐",
                "category": "每日推荐",
                "data": {"action": "dailyrecommend_subscribe"}
            },
            {
                "cmd": "/dailyrecommend_change",
                "event": EventType.PluginAction,
                "desc": "换一部每日推荐",
                "category": "每日推荐",
                "data": {"action": "dailyrecommend_change"}
            },
            {
                "cmd": "/dailyrecommend_skip",
                "event": EventType.PluginAction,
                "desc": "今日跳过每日推荐",
                "category": "每日推荐",
                "data": {"action": "dailyrecommend_skip"}
            },
            {
                "cmd": "/dailyrecommend_run",
                "event": EventType.PluginAction,
                "desc": "立即生成每日推荐",
                "category": "每日推荐",
                "data": {"action": "dailyrecommend_run"}
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run_once",
                "endpoint": self.run_once,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即推荐一部"
            },
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "清空每日推荐历史"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{
            "id": "DailyRecommend",
            "name": "每日推荐服务",
            "trigger": CronTrigger.from_crontab(self._cron or "0 9 * * *"),
            "func": self.recommend,
            "kwargs": {}
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即推荐一次"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "proxy", "label": "使用代理"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "recommend_hour",
                                        "label": "推荐小时",
                                        "items": [
                                            {"title": f"{hour:02d}:00", "value": hour}
                                            for hour in range(24)
                                        ]
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "tmdb_token",
                                        "label": "TMDb Read Access Token",
                                        "placeholder": "Bearer Token"
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "media_type",
                                        "label": "推荐内容",
                                        "items": [
                                            {"title": "电影", "value": "movie"},
                                            {"title": "电视剧", "value": "tv"},
                                            {"title": "电影和电视剧", "value": "mixed"}
                                        ]
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "language_pref",
                                        "label": "语言偏好",
                                        "items": [
                                            {"title": "不限", "value": "any"},
                                            {"title": "国语/中文", "value": "zh"},
                                            {"title": "外语", "value": "foreign"}
                                        ]
                                    }
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "multiple": True,
                                        "chips": True,
                                        "clearable": True,
                                        "model": "genres",
                                        "label": "类型偏好",
                                        "items": self._genre_options
                                    }
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "min_vote", "label": "最低评分", "placeholder": "6.5"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "min_vote_count", "label": "最低投票数", "placeholder": "200"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "max_pages", "label": "候选页数", "placeholder": "5"}
                                }]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "year_start", "label": "起始年份", "placeholder": "2000"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {"model": "year_end", "label": "结束年份", "placeholder": "0 表示不限"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "exclude_recommended", "label": "排除已推荐"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "exclude_subscribed", "label": "排除已订阅"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "exclude_exists", "label": "排除已入库"}
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "notification_type",
                                        "label": "通知类型",
                                        "items": [
                                            {"title": "订阅", "value": "Subscribe"},
                                            {"title": "插件", "value": "Plugin"},
                                            {"title": "手动处理", "value": "Manual"},
                                            {"title": "其它", "value": "Other"}
                                        ]
                                    }
                                }]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "proxy": False,
            "cron": "0 9 * * *",
            "recommend_hour": 9,
            "tmdb_token": "",
            "media_type": "mixed",
            "language_pref": "any",
            "language": "zh-CN",
            "genres": [],
            "year_start": 2000,
            "year_end": 0,
            "min_vote": 6.5,
            "min_vote_count": 200,
            "max_pages": 5,
            "exclude_recommended": True,
            "exclude_subscribed": True,
            "exclude_exists": True,
            "notification_type": "Subscribe",
            "history_limit": 1000
        }

    def get_page(self) -> List[dict]:
        active = self.get_data("active") or {}
        history = self.get_data("history") or []
        last_result = self.get_data("last_result") or {}
        content = []
        if not self._tmdb_token:
            content.append({
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": "未配置 TMDb Read Access Token，无法生成推荐。填写 Token 后保存配置，再打开“立即推荐一次”。"
                }
            })
        if last_result:
            status = last_result.get("status")
            alert_type = "info"
            if status == "running":
                alert_type = "info"
            elif last_result.get("success"):
                alert_type = "success"
            else:
                alert_type = "error"
            content.append({
                "component": "VAlert",
                "props": {
                    "type": alert_type,
                    "variant": "tonal",
                    "text": f"最近执行：{last_result.get('time') or '-'}，{last_result.get('message') or '-'}"
                }
            })
        if active:
            content.append({
                "component": "VAlert",
                "props": {
                    "type": "success",
                    "variant": "tonal",
                    "text": f"当前推荐：{active.get('title')}，微信回复：/每日要 / /每日换 / /每日跳。"
                }
            })
        content.append({
            "component": "VAlert",
            "props": {
                "type": "info",
                "variant": "tonal",
                "text": f"历史记录 {len(history)} 条。"
            }
        })
        return content

    def run_once(self) -> Dict[str, Any]:
        return self.recommend(force=True)

    def delete_history(self) -> Dict[str, Any]:
        self.del_data("history")
        self.del_data("active")
        self.del_data("skip_date")
        return {"success": True, "message": "每日推荐历史已清空"}

    def recommend(
        self,
        force: bool = False,
        channel: Any = None,
        userid: Any = None,
        exclude_key: Optional[str] = None
    ) -> Dict[str, Any]:
        if not self._tmdb_token:
            message = "每日推荐未配置 TMDb Read Access Token"
            logger.error(message)
            self.__save_last_result(False, message)
            self.__post(title="每日推荐配置缺失", text=message, channel=channel, userid=userid)
            return {"success": False, "message": message}

        logger.info(
            "每日推荐开始执行："
            f"force={force}, media_type={self._media_type}, language_pref={self._language_pref}, "
            f"genres={self._genres}, year={self._year_start}-{self._year_end or '不限'}, "
            f"min_vote={self._min_vote}, min_vote_count={self._min_vote_count}, max_pages={self._max_pages}, "
            f"exclude_recommended={self._exclude_recommended}, exclude_subscribed={self._exclude_subscribed}, "
            f"exclude_exists={self._exclude_exists}"
        )
        self.__save_last_result(True, "正在执行推荐筛选...", status="running")

        today = self.__today()
        if not force:
            active = self.get_data("active") or {}
            if active.get("date") == today:
                logger.info("今日已推荐，跳过重复推送")
                self.__save_last_result(True, "今日已推荐")
                return {"success": True, "message": "今日已推荐"}
            if self.get_data("skip_date") == today:
                logger.info("今日已跳过推荐")
                self.__save_last_result(True, "今日已跳过")
                return {"success": True, "message": "今日已跳过"}

        try:
            candidate = self.__pick_candidate(exclude_key=exclude_key)
        except Exception as err:
            message = f"每日推荐执行失败：{err}"
            logger.error(message)
            self.__save_last_result(False, message)
            self.__post(title="每日推荐执行失败", text=message, channel=channel, userid=userid)
            return {"success": False, "message": message}
        if not candidate:
            message = "没有找到符合条件的新推荐"
            logger.warn(message)
            self.__save_last_result(False, message)
            self.__post(title="每日推荐", text=message, channel=channel, userid=userid)
            return {"success": False, "message": message}

        active = {
            **candidate,
            "date": today,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.save_data("active", active)
        self.__append_history(active, "recommended")
        self.__post_recommendation(active, channel=channel, userid=userid)
        message = f"已生成推荐：{active.get('title')}"
        self.__save_last_result(True, message)
        return {"success": True, "message": message, "data": active}

    @eventmanager.register(EventType.UserMessage)
    def on_user_message(self, event: Event):
        if not self._enabled or not event:
            return
        event_data = event.event_data or {}
        text = self.__extract_event_text(event_data)
        action = self.__action_from_text(text)
        if not action:
            return

        logger.info(f"每日推荐收到用户回复：text={text}, action={action}, event_data={event_data}")
        active = self.get_data("active") or {}
        if not active:
            logger.info("每日推荐收到用户回复，但当前没有 active 推荐，忽略")
            return

        channel = event_data.get("channel")
        userid = event_data.get("userid") or event_data.get("user")

        self.__handle_action(action, active=active, channel=channel, userid=userid)

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        if not self._enabled or not event:
            return
        event_data = event.event_data or {}
        action = event_data.get("action")
        if action not in {
            "dailyrecommend_subscribe",
            "dailyrecommend_change",
            "dailyrecommend_skip",
            "dailyrecommend_run"
        }:
            return

        channel = event_data.get("channel")
        userid = event_data.get("userid") or event_data.get("user")
        logger.info(f"每日推荐收到远程命令：action={action}, event_data={event_data}")

        if action == "dailyrecommend_run":
            self.recommend(force=True, channel=channel, userid=userid)
            return

        active = self.get_data("active") or {}
        if not active:
            logger.info("每日推荐收到远程命令，但当前没有 active 推荐")
            self.__post(title="每日推荐", text="当前没有可操作的推荐，请先执行 /dailyrecommend_run。", channel=channel, userid=userid)
            return

        if action == "dailyrecommend_subscribe":
            self.__handle_action("subscribe", active=active, channel=channel, userid=userid)
        elif action == "dailyrecommend_change":
            self.__handle_action("change", active=active, channel=channel, userid=userid)
        elif action == "dailyrecommend_skip":
            self.__handle_action("skip", active=active, channel=channel, userid=userid)

    @eventmanager.register(EventType.MessageAction)
    def on_message_action(self, event: Event):
        if not self._enabled or not event:
            return
        event_data = event.event_data or {}
        plugin_id = event_data.get("plugin_id")
        if plugin_id and plugin_id != self.__class__.__name__:
            return
        text = self.__extract_event_text(event_data)
        action = self.__action_from_text(text)
        if action not in {"subscribe", "change", "skip"}:
            return

        active = self.get_data("active") or {}
        if not active:
            logger.info("每日推荐收到按钮回调，但当前没有 active 推荐")
            return
        channel = event_data.get("channel")
        userid = event_data.get("userid") or event_data.get("user")
        logger.info(f"每日推荐收到按钮回调：text={text}, action={action}, event_data={event_data}")
        self.__handle_action(action, active=active, channel=channel, userid=userid)

    def __pick_candidate(
        self,
        exclude_key: Optional[str] = None,
        ignore_history: bool = False,
        max_pages: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        media_types = self.__media_types_for_today()
        history_keys = set() if ignore_history else (self.__history_keys() if self._exclude_recommended else set())
        if exclude_key:
            history_keys.add(exclude_key)

        logger.info(f"每日推荐候选类型顺序：{media_types}")
        for media_type in media_types:
            candidates = self.__discover(media_type, max_pages=max_pages)
            logger.info(f"每日推荐 {media_type} 候选数量：{len(candidates)}")
            for item in candidates:
                key = f"{media_type}:{item.get('id')}"
                if key in history_keys:
                    logger.info(f"每日推荐跳过已推荐候选：{item.get('title') or item.get('name')} ({key})")
                    continue
                if self._language_pref == "foreign" and item.get("original_language") == "zh":
                    logger.info(f"每日推荐跳过中文原语种候选：{item.get('title') or item.get('name')}")
                    continue
                candidate = self.__build_candidate(media_type, item)
                if not candidate:
                    logger.info(f"每日推荐跳过无标题候选：{item}")
                    continue
                if self.__should_skip_by_moviepilot(candidate):
                    continue
                logger.info(
                    f"每日推荐命中候选：{candidate.get('title')} "
                    f"({candidate.get('year') or '-'}) tmdb={candidate.get('tmdbid')}"
                )
                self.__enrich_candidate(candidate)
                return candidate
        return None

    def __discover(self, media_type: str, max_pages: Optional[int] = None) -> List[dict]:
        endpoint = "/discover/movie" if media_type == "movie" else "/discover/tv"
        candidates = []
        seen = set()
        page_limit = max(1, min(self.__safe_int(max_pages, self._max_pages), 20))
        for page in range(1, page_limit + 1):
            params = self.__discover_params(media_type, page)
            logger.info(f"每日推荐请求 TMDb：type={media_type}, page={page}")
            data = self.__tmdb_get(endpoint, params)
            results = data.get("results") or []
            logger.info(
                f"每日推荐 TMDb 返回：type={media_type}, page={page}, "
                f"results={len(results)}, total_pages={data.get('total_pages')}"
            )
            for item in results:
                item_id = item.get("id")
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                candidates.append(item)
            if page >= int(data.get("total_pages") or 1):
                break

        today = self.__today()
        rnd = random.Random(f"{today}:{media_type}:{','.join(self._genres)}:{self._language_pref}")
        scored = []
        for item in candidates:
            vote = self.__safe_float(item.get("vote_average"), 0)
            vote_count = self.__safe_int(item.get("vote_count"), 0)
            popularity = self.__safe_float(item.get("popularity"), 0)
            score = vote * 12 + math.log10(vote_count + 1) * 8 + popularity * 0.05 + rnd.random() * 10
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored]

    def __discover_params(self, media_type: str, page: int) -> Dict[str, Any]:
        params = {
            "language": self._language,
            "include_adult": "false",
            "page": page,
            "sort_by": "vote_average.desc",
            "vote_average.gte": self._min_vote,
            "vote_count.gte": self._min_vote_count
        }
        if self._language_pref == "zh":
            params["with_original_language"] = "zh"

        if self._genres:
            ids = []
            mapping = self._movie_genres if media_type == "movie" else self._tv_genres
            for genre in self._genres:
                if genre in mapping:
                    ids.append(str(mapping[genre]))
            if ids:
                params["with_genres"] = "|".join(ids)

        if self._year_start:
            if media_type == "movie":
                params["primary_release_date.gte"] = f"{self._year_start}-01-01"
            else:
                params["first_air_date.gte"] = f"{self._year_start}-01-01"
        if self._year_end:
            if media_type == "movie":
                params["primary_release_date.lte"] = f"{self._year_end}-12-31"
            else:
                params["first_air_date.lte"] = f"{self._year_end}-12-31"
        return params

    def __build_candidate(self, media_type: str, item: dict) -> Optional[Dict[str, Any]]:
        title = item.get("title") if media_type == "movie" else item.get("name")
        original_title = item.get("original_title") if media_type == "movie" else item.get("original_name")
        date_value = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
        year = self.__parse_year(date_value)
        if not title:
            return None

        genre_names = [self._genre_names.get(genre_id) for genre_id in item.get("genre_ids") or []]
        genre_names = [name for name in genre_names if name]
        return {
            "key": f"{media_type}:{item.get('id')}",
            "tmdbid": item.get("id"),
            "media_type": media_type,
            "title": title,
            "original_title": original_title,
            "year": year,
            "date": date_value,
            "overview": item.get("overview") or "",
            "cast": [],
            "vote": item.get("vote_average"),
            "vote_count": item.get("vote_count"),
            "popularity": item.get("popularity"),
            "genres": genre_names,
            "poster": self.__image_url(item.get("poster_path")),
            "original_language": item.get("original_language")
        }

    def __enrich_candidate(self, candidate: Dict[str, Any]):
        tmdbid = candidate.get("tmdbid")
        if not tmdbid:
            return
        path = f"/movie/{tmdbid}" if candidate.get("media_type") == "movie" else f"/tv/{tmdbid}"
        try:
            detail = self.__tmdb_get(path, {
                "language": self._language,
                "append_to_response": "credits"
            })
        except Exception as err:
            logger.warn(f"每日推荐详情补充失败：{candidate.get('title')} - {err}")
            return

        if detail.get("overview"):
            candidate["overview"] = detail.get("overview")

        cast_names = []
        for person in ((detail.get("credits") or {}).get("cast") or []):
            name = person.get("name") or person.get("original_name")
            if name and name not in cast_names:
                cast_names.append(name)
            if len(cast_names) >= 3:
                break
        candidate["cast"] = cast_names

    def __should_skip_by_moviepilot(self, candidate: Dict[str, Any]) -> bool:
        meta = MetaInfo(candidate.get("title"))
        if candidate.get("year"):
            meta.year = candidate.get("year")
        meta.type = MediaType.MOVIE if candidate.get("media_type") == "movie" else MediaType.TV

        try:
            mediainfo: MediaInfo = self.chain.recognize_media(
                meta=meta,
                tmdbid=candidate.get("tmdbid"),
                mtype=meta.type
            )
        except TypeError:
            mediainfo: MediaInfo = self.chain.recognize_media(meta=meta, tmdbid=candidate.get("tmdbid"))

        if not mediainfo:
            logger.warn(f"{candidate.get('title')} 未识别到 MoviePilot 媒体信息，跳过")
            return True

        if self._exclude_subscribed and SubscribeChain().exists(mediainfo=mediainfo, meta=meta):
            logger.info(f"{mediainfo.title_year} 已订阅，跳过推荐")
            return True

        if self._exclude_exists:
            try:
                exists, no_exists = DownloadChain().get_no_exists_info(meta=meta, mediainfo=mediainfo)
                if exists:
                    logger.info(f"{mediainfo.title_year} 已完整入库，跳过推荐")
                    return True
                if self.__has_partial_exists(mediainfo=mediainfo, meta=meta, no_exists=no_exists):
                    logger.info(f"{mediainfo.title_year} 已部分入库，跳过推荐：缺失={no_exists}")
                    return True
            except Exception as err:
                logger.warn(f"{mediainfo.title_year} 入库状态检查失败：{err}")
        logger.info(f"{mediainfo.title_year} 未订阅/未入库，作为可推荐候选")
        return False

    def __subscribe_active(self, active: Dict[str, Any], channel: Any = None, userid: Any = None):
        media_type = MediaType.MOVIE if active.get("media_type") == "movie" else MediaType.TV
        meta = MetaInfo(active.get("title"))
        meta.type = media_type
        if active.get("year"):
            meta.year = active.get("year")

        logger.info(
            f"每日推荐开始订阅：title={active.get('title')}, year={active.get('year')}, "
            f"type={active.get('media_type')}, tmdb={active.get('tmdbid')}, channel={channel or '默认'}, userid={userid or '-'}"
        )
        try:
            mediainfo = self.chain.recognize_media(meta=meta, tmdbid=active.get("tmdbid"), mtype=media_type)
        except TypeError:
            mediainfo = self.chain.recognize_media(meta=meta, tmdbid=active.get("tmdbid"))

        if not mediainfo:
            logger.warn(f"每日推荐订阅失败：{active.get('title')} 未识别到媒体信息")
            self.__post(title="订阅失败", text=f"{active.get('title')} 未识别到媒体信息。", channel=channel, userid=userid)
            return

        if SubscribeChain().exists(mediainfo=mediainfo, meta=meta):
            logger.info(f"每日推荐订阅已存在：{mediainfo.title_year}")
            self.__append_history(active, "already_subscribed")
            self.save_data("active", {})
            self.__post(title="订阅已存在", text=f"{mediainfo.title_year} 已在订阅列表中。", channel=channel, userid=userid)
            return

        sid, message = SubscribeChain().add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=meta.begin_season,
            exist_ok=True,
            username="每日推荐"
        )
        if sid:
            logger.info(f"每日推荐订阅成功：{mediainfo.title_year}, sid={sid}, message={message}")
            self.__append_history(active, "subscribed")
            self.save_data("active", {})
            self.__post(title="已加入订阅", text=f"{mediainfo.title_year}\n结果：{message}", channel=channel, userid=userid)
        else:
            logger.warn(f"每日推荐订阅失败：{mediainfo.title_year}, message={message}")
            self.__post(title="订阅失败", text=f"{mediainfo.title_year}\n原因：{message}", channel=channel, userid=userid)

    def __post_recommendation(self, item: Dict[str, Any], channel: Any = None, userid: Any = None):
        mtype = "电影" if item.get("media_type") == "movie" else "电视剧"
        title = f"今日推荐：{item.get('title')}"

        lines = [
            f"类型：{mtype}",
            f"年份：{item.get('year') or '-'}",
            f"评分：{item.get('vote') or '-'} / 投票：{item.get('vote_count') or '-'}",
            f"主演：{self.__cast_text(item.get('cast'))}",
            f"简介：{self.__core_overview(item.get('overview'))}",
            "",
            "/每日要：订阅",
            "/每日换：换一部",
            "/每日跳：跳过"
        ]
        buttons = [
            [
                {"text": "订阅", "callback_data": f"[PLUGIN]{self.__class__.__name__}|subscribe"},
                {"text": "换一部", "callback_data": f"[PLUGIN]{self.__class__.__name__}|change"},
                {"text": "跳过", "callback_data": f"[PLUGIN]{self.__class__.__name__}|skip"}
            ]
        ]
        self.__post(
            title=title,
            text="\n".join(lines),
            image=item.get("poster"),
            channel=channel,
            userid=userid,
            buttons=buttons
        )

    def __handle_action(self, action: str, active: Dict[str, Any], channel: Any = None, userid: Any = None):
        logger.info(
            f"每日推荐处理动作：action={action}, title={active.get('title')}, "
            f"channel={channel or '默认'}, userid={userid or '-'}"
        )
        if action == "subscribe":
            self.__subscribe_active(active, channel=channel, userid=userid)
        elif action == "change":
            self.__change_active(active, channel=channel, userid=userid)
        elif action == "skip":
            self.__append_history(active, "skipped")
            self.save_data("active", {})
            self.save_data("skip_date", self.__today())
            self.__post(title="今日推荐已跳过", text="明天会继续按你的偏好推荐。", channel=channel, userid=userid)

    def __change_active(self, active: Dict[str, Any], channel: Any = None, userid: Any = None):
        self.__append_history(active, "changed")
        old_key = active.get("key")
        today = self.__today()
        attempts = [
            {"ignore_history": False, "max_pages": self._max_pages},
            {"ignore_history": True, "max_pages": self._max_pages},
            {"ignore_history": True, "max_pages": max(self._max_pages, 10)},
            {"ignore_history": True, "max_pages": 20}
        ]

        for attempt in attempts:
            try:
                candidate = self.__pick_candidate(
                    exclude_key=old_key,
                    ignore_history=attempt["ignore_history"],
                    max_pages=attempt["max_pages"]
                )
            except Exception as err:
                logger.error(f"每日推荐换一部执行失败：{err}")
                self.__save_last_result(False, f"换一部执行失败：{err}")
                self.save_data("active", active)
                return
            if not candidate:
                continue

            new_active = {
                **candidate,
                "date": today,
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            self.save_data("active", new_active)
            self.__append_history(new_active, "recommended")
            self.__post_recommendation(new_active, channel=channel, userid=userid)
            self.__save_last_result(True, f"已换一部推荐：{new_active.get('title')}")
            return

        logger.warn("每日推荐换一部未找到可替换候选，保留当前推荐")
        self.save_data("active", active)
        self.__save_last_result(True, "换一部未找到可替换候选，已保留当前推荐")

    def __post(
        self,
        title: str,
        text: str = "",
        image: Optional[str] = None,
        channel: Any = None,
        userid: Any = None,
        buttons: Optional[List[Any]] = None
    ):
        mtype = self.__notification_type()
        kwargs = {
            "mtype": mtype,
            "title": title,
            "text": text or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        if image:
            kwargs["image"] = image
        if channel:
            kwargs["channel"] = channel
        if userid:
            kwargs["userid"] = userid
        if buttons:
            kwargs["buttons"] = buttons
        try:
            logger.info(
                f"每日推荐发送通知：type={getattr(mtype, 'value', mtype)}, "
                f"channel={channel or '默认'}, userid={userid or '-'}, title={title}"
            )
            self.post_message(**kwargs)
        except Exception as err:
            logger.error(f"每日推荐通知发送失败：{err}")
            self.__save_last_result(False, f"通知发送失败：{err}")

    def __has_partial_exists(self, mediainfo: MediaInfo, meta: MetaInfo, no_exists: Dict[Any, Any]) -> bool:
        if not no_exists or mediainfo.type != MediaType.TV:
            return False

        expected_seasons = self.__expected_seasons(mediainfo=mediainfo, meta=meta)
        missing_full_seasons = set()
        has_partial_missing = False

        for season_map in no_exists.values():
            if not isinstance(season_map, dict):
                continue
            for season, info in season_map.items():
                season_num = self.__safe_int(season, -1)
                if season_num < 0:
                    continue
                episodes = self.__missing_episodes(info)
                if episodes:
                    has_partial_missing = True
                else:
                    missing_full_seasons.add(season_num)

        if has_partial_missing:
            return True
        if expected_seasons and missing_full_seasons >= expected_seasons:
            return False
        return bool(missing_full_seasons)

    def __expected_seasons(self, mediainfo: MediaInfo, meta: MetaInfo) -> set:
        seasons = set()
        season_filter = set(getattr(meta, "season_list", None) or [])
        for season, episodes in (getattr(mediainfo, "seasons", None) or {}).items():
            season_num = self.__safe_int(season, -1)
            if season_num < 0 or not episodes:
                continue
            if getattr(meta, "sea", None) and season_filter and season_num not in season_filter:
                continue
            seasons.add(season_num)
        return seasons

    @staticmethod
    def __missing_episodes(info: Any) -> List[Any]:
        if isinstance(info, dict):
            episodes = info.get("episodes")
        else:
            episodes = getattr(info, "episodes", None)
        return list(episodes or [])

    def __notification_type(self):
        value = self._notification_type or "Subscribe"
        if isinstance(value, NotificationType):
            return value
        if hasattr(NotificationType, str(value)):
            return getattr(NotificationType, str(value))
        for item in NotificationType:
            if item.name == value or item.value == value:
                return item
        return NotificationType.Subscribe

    def __tmdb_get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        token = self._tmdb_token
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        proxies = settings.PROXY if self._proxy else None
        res = RequestUtils(headers=headers, proxies=proxies).get_res(
            url=f"https://api.themoviedb.org/3{path}",
            params=params
        )
        if not res:
            raise RuntimeError("请求无响应")
        if res.status_code != 200:
            raise RuntimeError(f"HTTP {res.status_code}: {res.text[:200]}")
        return res.json()

    def __media_types_for_today(self) -> List[str]:
        if self._media_type in {"movie", "tv"}:
            return [self._media_type]
        today = self.__today()
        media_types = ["movie", "tv"]
        random.Random(today).shuffle(media_types)
        return media_types

    def __history_keys(self) -> set:
        return {item.get("key") for item in (self.get_data("history") or []) if item.get("key")}

    def __append_history(self, item: Dict[str, Any], result: str):
        history = self.get_data("history") or []
        history.append({
            "key": item.get("key"),
            "title": item.get("title"),
            "media_type": item.get("media_type"),
            "tmdbid": item.get("tmdbid"),
            "year": item.get("year"),
            "result": result,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        if len(history) > self._history_limit:
            history = history[-self._history_limit:]
        self.save_data("history", history)

    def __save_last_result(self, success: bool, message: str, status: Optional[str] = None):
        self.save_data("last_result", {
            "success": bool(success),
            "message": message,
            "status": status or ("success" if success else "error"),
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "proxy": self._proxy,
            "cron": self._cron,
            "recommend_hour": self._recommend_hour,
            "tmdb_token": self._tmdb_token,
            "media_type": self._media_type,
            "language_pref": self._language_pref,
            "language": self._language,
            "genres": self._genres,
            "year_start": self._year_start,
            "year_end": self._year_end,
            "min_vote": self._min_vote,
            "min_vote_count": self._min_vote_count,
            "max_pages": self._max_pages,
            "exclude_recommended": self._exclude_recommended,
            "exclude_subscribed": self._exclude_subscribed,
            "exclude_exists": self._exclude_exists,
            "notification_type": self._notification_type,
            "history_limit": self._history_limit
        })

    @staticmethod
    def __safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def __safe_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def __parse_year(value: str) -> Optional[int]:
        try:
            return int(str(value or "")[:4])
        except Exception:
            return None

    def __parse_recommend_hour(self, config: dict) -> int:
        if config.get("recommend_hour") not in (None, ""):
            return max(0, min(self.__safe_int(config.get("recommend_hour"), 9), 23))
        cron = str(config.get("cron") or "").strip()
        parts = cron.split()
        if len(parts) >= 2:
            return max(0, min(self.__safe_int(parts[1], 9), 23))
        return 9

    @staticmethod
    def __today() -> str:
        return datetime.date.today().isoformat()

    @staticmethod
    def __image_url(path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        return f"https://image.tmdb.org/t/p/w500{path}"

    @classmethod
    def __extract_event_text(cls, event_data: Any) -> str:
        if not isinstance(event_data, dict):
            return str(event_data or "").strip()
        for key in ("text", "content", "message", "msg", "body", "value", "command"):
            value = event_data.get(key)
            if isinstance(value, dict):
                nested = cls.__extract_event_text(value)
                if nested:
                    return nested
            elif value not in (None, ""):
                return str(value).strip()
        return ""

    @staticmethod
    def __action_from_text(text: Optional[str]) -> Optional[str]:
        value = str(text or "").strip()
        if not value:
            return None
        value = value.translate(str.maketrans({"１": "1", "２": "2", "３": "3"}))
        lower = value.lower().strip()
        if lower.startswith("/每日要") or lower.startswith("/dailyrecommend_subscribe") or lower in {"1", "要", "要了", "收", "收下", "订", "订阅", "加入订阅", "subscribe"}:
            return "subscribe"
        if lower.startswith("/每日换") or lower.startswith("/dailyrecommend_change") or lower in {"2", "换", "换一部", "换一个", "换部", "下一部", "再来", "change"}:
            return "change"
        if lower.startswith("/每日跳") or lower.startswith("/dailyrecommend_skip") or lower in {"3", "跳", "今日跳过", "跳过", "skip"}:
            return "skip"

        match = re.match(r"^(?:回复\s*)?([123])(?:\s|$|[：:，,。.！!])", value)
        if not match:
            return None
        return {"1": "subscribe", "2": "change", "3": "skip"}.get(match.group(1))

    @staticmethod
    def __cast_text(value: Any) -> str:
        if not value:
            return "-"
        if isinstance(value, str):
            return value
        names = [str(item).strip() for item in value if str(item).strip()]
        return " / ".join(names[:3]) if names else "-"

    @classmethod
    def __core_overview(cls, value: Optional[str], limit: int = 70) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            return "暂无简介。"

        production_terms = ("执笔", "导演", "编剧", "制片", "制作", "出品", "主演", "预订成剧", "Netflix", "HBO", "ABC")
        plot_terms = ("讲述", "讲的是", "故事", "围绕", "聚焦", "跟随", "描绘", "记录", "发生在", "主角", "主人公")
        for term in plot_terms:
            index = text.find(term)
            if index > 0 and (index > 18 or any(word in text[:index] for word in production_terms)):
                text = text[index:]
                break

        parts = [part.strip() for part in re.split(r"(?<=[。！？!?])", text) if part.strip()]
        if len(parts) > 1 and any(word in parts[0] for word in production_terms) and not any(word in parts[0] for word in plot_terms):
            text = "".join(parts[1:]) or text

        return cls.__short_overview(text, limit=limit)

    @staticmethod
    def __short_overview(value: Optional[str], limit: int = 90) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            return "暂无简介。"
        if len(text) <= limit:
            return text
        return text[:limit].rstrip("，。,. ") + "..."

    def stop_service(self):
        pass
