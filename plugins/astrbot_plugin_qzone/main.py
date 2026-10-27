import base64
import random
import sys

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.api.web import json_response
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.campus_wall import CampusWall
from .core.own_space import OwnSpace
from .core.auto_replies import AutoReplies
from .core.config import PluginConfig
from .core.db import PostDB
from .core.llm_action import LLMAction
from .core.model import Comment, Post
from .core.post_images import prepare_post_images
from .core.public_material import autonomous_request, material_scope
from .core.qzone import QzoneAPI, QzoneParser, QzoneSession
from .core.scheduler import AutoComment, AutoPublish
from .core.sender import Sender
from .core.service import PostService
from .core.utils import get_ats, get_image_urls, parse_range

try:
    from quart import jsonify as _quart_jsonify
    from quart import request as _quart_request
except Exception:
    _quart_jsonify = None
    _quart_request = None


class QzonePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        # 配置
        self.cfg = PluginConfig(config, context)
        # 会话
        self.session = QzoneSession(self.cfg)
        # QQ空间
        self.qzone = QzoneAPI(self.session, self.cfg)
        # 数据库
        self.db = PostDB(self.cfg)
        # LLM模块
        self.llm = LLMAction(self.cfg)
        # 消息发送器
        self.sender = Sender(self.cfg)
        # 操作服务
        self.service = PostService(self.qzone, self.session, self.db, self.llm)
        # 表白墙
        self.campus_wall = CampusWall(self.cfg, self.service, self.db, self.sender)
        # 自动评论模块
        self.auto_comment: AutoComment | None = None
        # 自动发说说模块
        self.auto_publish: AutoPublish | None = None
        self.auto_replies = None
        self.own_space = OwnSpace(self)
        self._page_post_refs: dict[str, Post] = {}
        context.register_web_api('/dafeiyu_qzone/auto_replies', self.auto_replies_status, ['GET'], 'QQ空间自动回评状态')
        context.register_web_api('/dafeiyu_qzone/editorial', self.editorial_status, ['GET'], 'QQ空间文案与配图优化状态')
        context.register_web_api('/dafeiyu_qzone/own_read', self.own_read_status, ['GET'], '自己的空间读取状态')
        # This deployment uses private QQ commands; do not expose page write APIs.

    @staticmethod
    def _owner_private(event) -> bool:
        return (isinstance(event, AiocqhttpMessageEvent)
                and str(event.get_sender_id()) == "7000000001"
                and not event.get_group_id())

    def _owner_publish_chat(self, event) -> bool:
        if (not isinstance(event, AiocqhttpMessageEvent)
                or event.get_platform_id() != "dafeiyu-qq"
                or str(event.get_sender_id()) != "7000000001"):
            return False
        group = str(event.get_group_id() or "")
        if not group:
            return event.is_private_chat()
        settings = self.context.get_config(event.unified_msg_origin)["platform_settings"]
        allowed = {str(value) for value in settings.get("id_whitelist", [])}
        return group in allowed or event.unified_msg_origin in allowed

    @filter.on_platform_loaded()
    async def bind_qzone_platform(self):
        # On a full restart, QQ platforms are loaded after plugins.
        self.cfg.client = None
        self._capture_page_client()

    async def initialize(self):
        """插件加载时触发"""
        await self.db.initialize()
        await self.llm.editorial.initialize()
        self._capture_page_client()

        if not self.auto_comment and self.cfg.trigger.comment_cron:
            self.auto_comment = AutoComment(self.cfg, self.service, self.sender)

        if not self.auto_publish and self.cfg.trigger.publish_cron:
            self.auto_publish = AutoPublish(self.cfg, self.service, self.sender)
            await self.auto_publish.start()

        self.auto_replies = AutoReplies(self.cfg, self.service)
        await self.auto_replies.start()

    async def auto_replies_status(self):
        return json_response(await self.auto_replies.status() if self.auto_replies else {'enabled': False})

    async def editorial_status(self):
        return json_response(await self.llm.editorial.status())

    def _register_page_web_apis(self) -> None:
        routes = (
            ("page/status", self.page_status, ["GET"], "Qzone dashboard status"),
            ("page/feed", self.page_feed, ["GET"], "Qzone dashboard feed"),
            ("page/detail", self.page_detail, ["GET"], "Qzone dashboard detail"),
            ("page/publish", self.page_publish, ["POST"], "Qzone dashboard publish"),
            ("page/like", self.page_like, ["POST"], "Qzone dashboard like"),
            ("page/comment", self.page_comment, ["POST"], "Qzone dashboard comment"),
            ("page/reply", self.page_reply, ["POST"], "Qzone dashboard reply"),
            ("page/delete", self.page_delete, ["POST"], "Qzone dashboard delete"),
            (
                "page/upload-media",
                self.page_upload_media,
                ["POST"],
                "Qzone dashboard upload media",
            ),
        )
        for endpoint, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/astrbot_plugin_qzone/{endpoint}",
                handler,
                methods,
                desc,
            )

    async def _page_response(self, payload: dict, status: int = 200):
        if _quart_jsonify is None:
            return payload
        response = _quart_jsonify(payload)
        response.status_code = status
        return response

    async def _page_json(self, callback):
        try:
            payload = await callback()
            status = 200
        except Exception as exc:
            logger.exception("qzone page api failed: %s", exc)
            message = str(exc) or "请求失败"
            payload = {
                "ok": False,
                # AstrBot 的插件页桥接只识别字符串类型的 message/error，
                # 保留 error 对象的同时补一个顶层 message，避免真实异常被
                # 前端统一显示成 “Request failed with status code 400”。
                "message": message,
                "error": {"message": message},
            }
            status = 400
        return await self._page_response(payload, status)

    async def _page_query_params(self) -> dict:
        if _quart_request is None:
            return {}
        args = getattr(_quart_request, "args", {}) or {}
        try:
            return {str(key): value for key, value in args.items()}
        except Exception:
            return dict(args)

    async def _page_json_body(self) -> dict:
        if _quart_request is None:
            return {}
        try:
            data = await _quart_request.get_json(silent=True)
        except TypeError:
            data = await _quart_request.get_json()
        return data if isinstance(data, dict) else {}

    async def _build_page_status(self) -> dict:
        self._capture_page_client()
        try:
            uin = await self.session.get_uin()
            nickname = await self.session.get_nickname()
            bound = True
        except Exception:
            uin = 0
            nickname = ""
            bound = False
        return {
            "ok": True,
            "data": {
                "daemon": {"state": "ready" if bound else "offline"},
                "login": {
                    "bound": bound,
                    "uin": uin,
                    "nickname": nickname,
                    "avatar": "",
                },
                "limits": {
                    "feed": 10,
                    "images": 9,
                },
            },
        }

    def _remember_page_post(self, post: Post) -> str:
        post_id = f"{post.uin}:{post.tid}"
        self._page_post_refs[post_id] = post
        return post_id

    def _capture_page_client(self):
        if self.cfg.client is not None:
            return self.cfg.client
        try:
            platform = self.context.get_platform("aiocqhttp")
        except Exception:
            platform = None
        if platform is None:
            try:
                for candidate in self.context.platform_manager.platform_insts:
                    meta = candidate.meta()
                    if getattr(meta, "name", "") == "aiocqhttp":
                        platform = candidate
                        break
            except Exception:
                platform = None
        bot = getattr(platform, "bot", None) if platform is not None else None
        if bot is not None:
            self.cfg.client = bot
        return self.cfg.client

    def _page_post_payload(
        self,
        post: Post,
        *,
        include_comments: bool = False,
        self_uin: int = 0,
    ) -> dict:
        images = list(dict.fromkeys([*(post.images or []), *(post.videos or [])]))
        payload = {
            "id": self._remember_page_post(post),
            "author": {
                "uin": post.uin,
                "nickname": post.name or str(post.uin),
                "avatar": post.avatar_url or "",
            },
            "content": post.text or post.rt_con or "",
            "created_at": int(post.create_time or 0),
            "stats": {
                "likes": 0,
                "comments": len(post.comments or []),
            },
            "liked": False,
            "images": images,
            "can_delete": bool(self_uin and int(post.uin) == int(self_uin)),
        }
        if include_comments:
            payload["comments"] = [
                {
                    "id": str(comment.tid or index),
                    "author": {
                        "uin": comment.uin,
                        "nickname": comment.nickname or str(comment.uin),
                    },
                    "content": comment.content,
                    "created_at": int(comment.create_time or 0),
                    "can_reply": bool(comment.tid and comment.uin),
                    "parent_id": str(comment.parent_tid) if comment.parent_tid else None,
                }
                for index, comment in enumerate(post.comments or [], start=1)
            ]
        return payload

    def _require_page_post(self, post_id: str) -> Post:
        post = self._page_post_refs.get(str(post_id or "").strip())
        if not post:
            raise RuntimeError("说说引用已失效，请刷新页面后重试")
        return post

    async def page_status(self):
        return await self._page_json(self._build_page_status)

    async def page_feed(self):
        async def handler():
            self._capture_page_client()
            params = await self._page_query_params()
            scope = str(params.get("scope") or "friends")
            self_uin = await self.session.get_uin()
            target_id = None
            no_self = False
            if scope == "self":
                target_id = str(self_uin)
            elif scope == "profile":
                target_id = str(params.get("hostuin") or "").strip() or None
            else:
                no_self = False
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=0,
                num=min(max(int(params.get("limit") or 10), 1), 10),
                with_detail=False,
                no_self=no_self,
            )
            return {
                "ok": True,
                "data": {
                    "items": [
                        self._page_post_payload(post, self_uin=self_uin)
                        for post in posts
                    ],
                    "cursor": "",
                    "has_more": False,
                },
            }

        return await self._page_json(handler)

    async def page_detail(self):
        async def handler():
            self._capture_page_client()
            params = await self._page_query_params()
            post = self._require_page_post(params.get("id", ""))
            resp = await self.qzone.get_detail(post)
            if not resp.ok or not resp.data:
                raise RuntimeError(resp.message or "获取详情失败")
            parsed = QzoneParser.parse_feeds([resp.data])
            matched = parsed[0] if parsed else post
            self._remember_page_post(matched)
            return {
                "ok": True,
                "data": {
                    "post": self._page_post_payload(
                        matched,
                        include_comments=True,
                        self_uin=await self.session.get_uin(),
                    )
                },
            }

        return await self._page_json(handler)

    async def page_publish(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            media = body.get("media") or []
            images = []
            for item in media:
                if not isinstance(item, dict):
                    continue
                source = str(item.get("source") or "")
                if source.startswith("base64://"):
                    images.append(base64.b64decode(source.removeprefix("base64://")))
            post = await self.service.publish_post(
                text=str(body.get("content") or ""),
                images=images,
            )
            return {
                "ok": True,
                "data": {"post": self._page_post_payload(post)},
                "message": "说说已发布。",
            }

        return await self._page_json(handler)

    async def page_like(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            resp = await self.qzone.like(post)
            if not resp.ok:
                raise RuntimeError(resp.message or "点赞失败")
            return {"ok": True, "data": {"liked": True, "verified": True}}

        return await self._page_json(handler)

    async def page_comment(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            content = str(body.get("content") or "").strip()
            if not content:
                raise RuntimeError("评论内容不能为空")
            resp = await self.qzone.comment(post, content)
            if not resp.ok:
                raise RuntimeError(resp.message or "评论失败")
            comment = {
                "id": f"local-{len(post.comments) + 1}",
                "content": content,
                "author": {
                    "uin": await self.session.get_uin(),
                    "nickname": await self.session.get_nickname(),
                    "avatar": "",
                },
            }
            post.comments.append(
                Comment(
                    uin=comment["author"]["uin"],
                    nickname=comment["author"]["nickname"],
                    content=content,
                    create_time=0,
                    tid=0,
                    parent_tid=None,
                )
            )
            return {"ok": True, "data": {"comment": comment}, "message": "评论已发送。"}

        return await self._page_json(handler)

    async def page_reply(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            content = str(body.get("content") or "").strip()
            comment_id = str(body.get("commentid") or "")
            comment_uin = str(body.get("comment_uin") or body.get("commentuin") or "")
            if not content:
                raise RuntimeError("回复内容不能为空")

            # 楼中楼的 tid 是主评论内的楼层号（都从 1 开始），单靠 tid 会匹配到
            # 第一条同号评论；前端会带 comment_uin，用它一起定位。
            def _matches(item: Comment) -> bool:
                if str(item.tid) != comment_id:
                    return False
                return not comment_uin or str(item.uin) == comment_uin

            target = next((item for item in post.comments if _matches(item)), None)
            if target is None:
                raise RuntimeError("未找到要回复的评论")

            # 楼中楼回复：QQ 空间只有两级评论，回复楼中楼实际是在其主评论下
            # 再发一条 @对方的楼中楼，commentId 必须用主评论的 tid。
            reply_target = target
            reply_uin = None
            reply_nick = None
            target_comment_uin = None
            if target.parent_tid:
                main = next(
                    (
                        item
                        for item in post.comments
                        if not item.parent_tid and str(item.tid) == str(target.parent_tid)
                    ),
                    None,
                )
                if main is None:
                    raise RuntimeError("未找到该楼中楼所属的主评论")
                reply_target = Comment(
                    uin=target.uin,
                    nickname=target.nickname,
                    content=target.content,
                    create_time=target.create_time,
                    tid=main.tid,
                    parent_tid=None,
                )
                reply_uin = target.uin
                reply_nick = target.nickname
                target_comment_uin = main.uin

            resp = await self.qzone.reply(
                post,
                reply_target,
                content,
                reply_uin=reply_uin,
                reply_nick=reply_nick,
                comment_uin=target_comment_uin,
            )
            if not resp.ok:
                raise RuntimeError(resp.message or "回复失败")
            # 不同代理把新评论 ID 放在不同层级（顶层 tid 或 data.tid），
            # 取不到时留空，后续详情刷新会补上。
            inner = resp.data.get("data")
            inner = inner if isinstance(inner, dict) else {}
            reply = {
                "id": str(inner.get("tid") or resp.data.get("tid") or ""),
                "content": content,
                "author": {
                    "uin": await self.session.get_uin(),
                    "nickname": await self.session.get_nickname(),
                    "avatar": "",
                },
            }
            # 服务端是两级结构：回复楼中楼时新评论挂在主评论下（reply_target
            # 已被换成主评论），缓存里也要保持一致，否则下次按 parent_tid
            # 找不到所属主评论。
            cached_parent_tid = reply_target.tid if target.parent_tid else None
            post.comments.append(
                Comment(
                    uin=reply["author"]["uin"],
                    nickname=reply["author"]["nickname"],
                    content=content,
                    create_time=0,
                    tid=int(reply["id"]) if reply["id"].isdigit() else 0,
                    parent_tid=cached_parent_tid,
                )
            )
            return {"ok": True, "data": {"reply": reply}, "message": "回复已发送。"}

        return await self._page_json(handler)

    async def page_delete(self):
        async def handler():
            self._capture_page_client()
            body = await self._page_json_body()
            post = self._require_page_post(body.get("id", ""))
            self_uin = await self.session.get_uin()
            if int(post.uin) != int(self_uin):
                raise RuntimeError("只能删除自己发布的说说")
            resp = await self.qzone.delete(str(post.tid or ""))
            if not resp.ok:
                raise RuntimeError(resp.message or "删除失败")
            return {"ok": True, "data": {}, "message": "说说已删除。"}

        return await self._page_json(handler)

    async def page_upload_media(self):
        async def handler():
            if _quart_request is None:
                raise RuntimeError("当前环境不支持上传")
            files = await _quart_request.files
            upload = files.get("file") or files.get("image") or files.get("media")
            if upload is None:
                raise RuntimeError("没有收到图片文件")
            data = await upload.read()
            return {
                "ok": True,
                "data": {
                    "media": {
                        "kind": "image",
                        "name": upload.filename or "image.jpg",
                        "source": f"base64://{base64.b64encode(data).decode('ascii')}",
                        "size": len(data),
                        "mime_type": getattr(upload, "content_type", "")
                        or "image/jpeg",
                    }
                },
            }

        return await self._page_json(handler)

    async def terminate(self):
        """插件卸载时"""
        if self.auto_replies:
            await self.auto_replies.terminate()
        if self.qzone:
            await self.qzone.close()
        if self.auto_comment:
            await self.auto_comment.terminate()
        if self.auto_publish:
            await self.auto_publish.terminate()

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def prob_read_feed(self, event: AiocqhttpMessageEvent):
        """监听消息"""
        if not self.cfg.client:
            self.cfg.client = event.bot
            logger.debug("QQ空间所需的 CQHttp 客户端已初始化")

        # 按概率触发点赞+评论
        sender_id = event.get_sender_id()
        if (
            not self.cfg.source.is_ignore_user(sender_id)
            and random.random() < self.cfg.trigger.read_prob
        ):
            target_id = event.get_sender_id()
            posts = await self.service.query_feeds(
                target_id=target_id, pos=0, num=1, no_self=True, no_commented=True
            )
            for post in posts:
                try:
                    await self.service.comment_posts(post, event=event)
                    if self.cfg.trigger.like_when_comment:
                        await self.service.like_posts(post)
                    await self.sender.send_post(
                        event,
                        post,
                        message="触发读说说",
                        send_admin=self.cfg.trigger.send_admin,
                    )
                except Exception as e:
                    logger.error(e)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看访客")
    async def view_visitor(self, event: AiocqhttpMessageEvent):
        """查看访客"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        try:
            msg = await self.service.view_visitor()
            await self.sender.send_msg(event, msg)
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)

    async def _get_posts(
        self,
        event: AiocqhttpMessageEvent,
        *,
        target_id: str | None = None,
        with_detail: bool = False,
        no_commented=False,
        no_self=False,
    ) -> list[Post]:
        pos, num = parse_range(event)
        at_ids = get_ats(event)
        if not target_id:
            target_id = at_ids[0] if at_ids else None

        if target_id:
            self.cfg.remove_ignore_users(target_id)
        try:
            logger.debug(
                f"正在查询说说： {target_id, pos, num, with_detail, no_commented, no_self}"
            )
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=pos,
                num=num,
                with_detail=with_detail,
                no_commented=no_commented,
                no_self=no_self,
            )
            if not posts:
                await event.send(event.plain_result("查询结果为空"))
                event.stop_event()
            return posts
        except Exception as e:
            await event.send(event.plain_result(str(e)))
            logger.error(e)
            event.stop_event()
            return []

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("看说说", alias={"查看说说"})
    async def view_feed(self, event: AiocqhttpMessageEvent):
        """
        看说说 <@群友> <序号>
        """
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        posts = await self._get_posts(event, with_detail=True)
        for post in posts:
            await self.sender.send_post(event, post)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("评说说", alias={"评论说说", "读说说"})
    async def comment_feed(self, event: AiocqhttpMessageEvent):
        """评说说 <序号/范围>"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        posts = await self._get_posts(event, no_commented=True, no_self=True)
        for post in posts:
            try:
                await self.service.comment_posts(post, event=event)
                msg = "已评论"
                if self.cfg.trigger.like_when_comment:
                    await self.service.like_posts(post)
                    msg += "并点赞"
                await self.sender.send_post(event, post, message=msg)
            except Exception as e:
                await event.send(event.plain_result(str(e)))
                logger.error(e)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("赞说说")
    async def like_feed(self, event: AiocqhttpMessageEvent):
        """赞说说 <序号/范围>"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        posts = await self._get_posts(event)
        for post in posts:
            try:
                await self.service.like_posts(post)
                await self.sender.send_post(event, post, message="已点赞")
            except Exception as e:
                await event.send(event.plain_result(str(e)))
                logger.error(e)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发说说")
    async def publish_feed(self, event: AiocqhttpMessageEvent):
        """发说说 <内容> <图片>, 由用户指定内容"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        text = event.message_str.partition(" ")[2]
        images = await get_image_urls(event)
        try:
            post = await self.service.publish_post(text=text, images=images)
            await self.sender.send_post(event, post, message="已发布")
            event.stop_event()
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("写说说", alias={"写稿"})
    async def write_feed(self, event: AiocqhttpMessageEvent):
        """写说说 <主题> <图片>, 由AI写完后管理员用‘通过稿件 ID’命令发布"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        group_id = event.get_group_id()
        topic = event.message_str.partition(" ")[2]
        try:
            text = await self.llm.generate_post(
                group_id=group_id, topic=topic, event=event
            )
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)
            return
        images = await get_image_urls(event)
        if not text and not images:
            yield event.plain_result("说说生成失败")
            return
        self_id = event.get_self_id()
        post = Post(
            uin=int(self_id),
            text=text or "",
            images=images,
            status="pending",
        )
        await self.db.save(post)
        await self.sender.send_post(event, post)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删说说")
    async def delete_feed(self, event: AiocqhttpMessageEvent):
        """删说说 <稿件ID>"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        posts = await self._get_posts(event, target_id=event.get_self_id())
        for post in posts:
            try:
                await self.sender.send_post(event, post, message="已删除说说")
                await self.service.delete_post(post)
            except Exception as e:
                await event.send(event.plain_result(str(e)))
                logger.error(e)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("回评", alias={"回复评论"})
    async def reply_comment(
        self, event: AiocqhttpMessageEvent, post_id: int = -1, comment_index: int = -1
    ):
        """回评 <稿件ID> <评论序号>, 默认回复最后一条非己评论"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        post = await self.db.get(post_id)
        if not post:
            yield event.plain_result(f"稿件#{post_id}不存在")
            return
        try:
            await self.service.reply_comment(post, index=comment_index, event=event)
            await self.sender.send_post(event, post, message="已回复评论")
        except Exception as e:
            await event.send(event.plain_result(str(e)))
            logger.error(e)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("投稿")
    async def contribute_post(self, event: AiocqhttpMessageEvent):
        """投稿 <内容> <图片>"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        await self.campus_wall.contribute(event)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("匿名投稿")
    async def anon_contribute_post(self, event: AiocqhttpMessageEvent):
        """匿名投稿 <内容> <图片>"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        await self.campus_wall.contribute(event, anon=True)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("撤稿")
    async def recall_post(self, event: AiocqhttpMessageEvent):
        """删除稿件 <稿件ID>"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        async for msg in self.campus_wall.delete(event):
            yield msg

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("看稿", alias={"查看稿件"})
    async def view_post(self, event: AiocqhttpMessageEvent):
        "查看稿件 <稿件ID>, 默认最新稿件"
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        async for msg in self.campus_wall.view(event):
            yield msg

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("过稿", alias={"通过稿件", "通过投稿"})
    async def approve_post(self, event: AiocqhttpMessageEvent):
        """通过稿件 <稿件ID>"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        async for msg in self.campus_wall.approve(event):
            yield msg

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拒稿", alias={"拒绝稿件", "拒绝投稿"})
    async def reject_post(self, event: AiocqhttpMessageEvent):
        """拒绝稿件 <稿件ID> <原因>"""
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        async for msg in self.campus_wall.reject(event):
            yield msg

    async def own_read_status(self):
        return json_response({'enabled': True, 'fixed_uin': 7000000002, 'read_only': True, 'short_followup_context': True, 'confirmed_social_sources': True, 'last': self.own_space.last})

    @filter.on_decorating_result(priority=sys.maxsize-32)
    async def ground_own_space_recall(self,event):
        await self.own_space.ground_social_reply(event)

    @filter.on_llm_request(priority=350)
    async def own_feed_context(self,event,req):
        await self.own_space.prepare(event,req)

    @filter.llm_tool(name='dafeiyu_qzone_read_own')
    async def read_own_space(self,event,pos:int=0,count:int=3,include_images:bool=False):
        """读取鲸鲸自己7000000002的QQ空间正文、时间和配图，不查询聊天对象的空间，不点赞、评论或发消息。

        Args:
            pos(number): 从第几条开始，0是最新，最大30
            count(number): 最多读几条，1到5；爸爸私信可同时读取最新评论
            include_images(boolean): 问配图、图片画面时必须true，把真实配图交给视觉模型理解；默认false只读正文
        """
        try:return await self.own_space.result(event,pos,count,include_images)
        except ValueError as error:return str(error)
        except Exception:return '自己的空间这次没有读成功，不能据此断言没有发布或编造内容。'

    @filter.llm_tool()
    async def llm_view_feed(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str | None = None,
        pos: int = 0,
        like: bool = False,
        reply: bool = False,
    ):
        """
        查看、点赞、评论某位用户QQ空间的某条说说、动态
        Args:
            user_id(string): 目标用户的QQ账号，必定为一串数字，如(12345678), 默认为当前用户QQ号
            pos(number): 要查询的说说序号, 默认为0表示最新
            like(boolean): 是否点赞
            reply(boolean): 是否评论
        """
        if not self._owner_private(event):
            return
        self.cfg.client = event.bot
        try:
            user_id = user_id or event.get_sender_id()
            logger.debug(f"正在查询用户（{user_id}）的第 {pos} 条说说")

            posts = await self.service.query_feeds(
                target_id=user_id,
                pos=pos,
                num=1,
                with_detail=True,
            )

            if not posts:
                return "查询结果为空"

            post = posts[0]

            # 执行动作
            msg = ""

            if like and reply:
                await self.service.comment_posts(post, event=event)
                await self.service.like_posts(post)
                msg = "已评论并点赞"
            elif reply:
                await self.service.comment_posts(post, event=event)
                msg = "已评论"
            elif like:
                await self.service.like_posts(post)
                msg = "已点赞"

            # 发送展示
            await self.sender.send_post(event, post, message=msg)

            return msg + "\n" + post.text + "\n" + "\n".join(post.images)

        except Exception as e:
            logger.error(e)
            return str(e)

    @filter.llm_tool()
    async def dafeiyu_qzone_draft(
        self,
        event: AiocqhttpMessageEvent,
        text: str = "",
        topic: str = "",
    ):
        """
        仅当爸爸明确要求只写草稿、先看看或暂不发布时使用。普通发空间请求使用 dafeiyu_qzone_publish 直接发布。
        Args:
            text(string): 已写好的原创说说正文；留空时按主题写作
            topic(string): 说说主题，正文留空时使用
        """
        if not self._owner_publish_chat(event):
            return "QQ空间草稿仅限爸爸本人在QQ私信或已开放的群聊中使用。"
        self.cfg.client = event.bot
        try:
            if autonomous_request(event):text='';topic=''
            text = text.strip() or await self.llm.generate_post(topic=topic, event=event)
            if not text:
                return "草稿生成失败，请重试。"
            if len(text) > 1000:
                return "草稿过长，请缩短到1000字以内。"
            post = Post(uin=int(event.get_self_id()), name="小鲸鱼", text=text, status="pending")
            await self.db.save(post)
            return (f"QQ空间草稿#{post.id}已保存，尚未发布。\n\n{post.text}\n\n"
                    f"爸爸确认后，请亲自在QQ私信发送：/过稿 {post.id}。"
                    "不要代替爸爸执行审核命令，也不要声称已经发布。")
        except Exception:
            logger.warning("QQ空间草稿生成失败")
            return "草稿暂时生成失败，请稍后重试。"

    @filter.llm_tool()
    async def dafeiyu_qzone_publish(
        self,
        event: AiocqhttpMessageEvent,
        text: str = "",
        topic: str = "",
        get_image: bool = False,
        with_image: bool = True,
        image_prompt: str = "",
    ):
        """
        爸爸本人要求发QQ空间、朋友圈或说说时，直接发布，无须审稿或再次确认。支持爸爸QQ私信与已开放的群聊。
        Args:
            text(string): 爸爸明确指定的正文；自己挑或随便发时留空，由公开素材流程写作，不从私聊摘内容
            topic(string): 正文留空时的写作主题
            get_image(boolean): 爸爸明确要求附当前消息或引用消息的图片时设为True
            with_image(boolean): 默认True自动配图，优先使用当前或引用消息附图，否则生成一张；明确纯文字或不要配图时设为False
            image_prompt(string): 可选的配图画面要求；配图在发布流程中生成，不要另调画图工具
        """
        if not self._owner_publish_chat(event):
            return "QQ空间发布仅限爸爸本人在QQ私信或已开放的群聊中使用。"
        if event.get_extra("dafeiyu_qzone_publish_attempted", False):
            return "本条消息已尝试发布过，请勿再次调用发布工具。"
        self.cfg.client = event.bot
        try:
            scope = material_scope(event)
            autonomous = scope['autonomous_text']
            if autonomous:text='';topic=''
            text = text.strip() or await self.llm.generate_post(topic=topic, event=event)
            if not text or len(text) > 1000:
                return "正文为空或过长，请使用1000字以内的正文。"
            event.set_extra("dafeiyu_qzone_publish_attempted", True)
            try:
                image_event=event if (scope['use_supplied_images'] or not autonomous) and not scope['reject_supplied_images'] else None
                images = await prepare_post_images(self.context, text, image_event,
                    (with_image or scope['use_supplied_images'] or scope['explicit_scene']) and not scope['text_only'], image_prompt if scope['explicit_scene'] else '',
                    editorial=getattr(getattr(self, 'llm', None), 'editorial', None),require_supplied=scope['use_supplied_images'])
            except Exception:
                logger.warning("QQ空间配图未完成，未提交发布")
                return "这次配图未完成，QQ空间尚未发布，没有自动重试；可以稍后重新发起，或明确要求纯文字发布。"
            post = await self.service.publish_post(text=text, images=images)
            if not post.tid:
                return "空间接口未返回动态编号，结果暂不能确认。请勿自动重试，避免重复发布。"
            image_note = f"，配图{len(images)}张" if images else "，纯文字"
            return f"已发布到QQ空间{image_note}，动态编号：{post.tid}。\n\n{post.text}"
        except Exception:
            logger.warning("QQ空间直接发布未完成")
            return "这次空间发布未完成，结果暂不能确认。请勿自动重试，避免重复发布。"
