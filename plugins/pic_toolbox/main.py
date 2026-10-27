"""图片处理工具箱：反色、调速、@用户反色（参照 pic_mirror 的指令解析模式）"""

import asyncio
import os
import re
import time
import uuid
from PIL import Image

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .meme import gif_speed, invert, flip, petpet, mirror, shoot, do, lash, behead, rotate

QQ_AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
TMP_DIR = Path(get_astrbot_data_path()) / "plugin_data" / "pic_toolbox"
os.makedirs(TMP_DIR, exist_ok=True)

# ── 工具函数 ────────────────────────────────────

def _download_sync(url: str, path: str) -> bool:
    from .limits import download_image
    try:
        download_image(url, path)
        return True
    except Exception as error:
        logger.warning("[pic_toolbox] 图片获取未通过: %s", type(error).__name__)
        return False


def _is_gif(path: str) -> bool:
    try:
        with Image.open(path) as im:
            return getattr(im, "is_animated", False)
    except Exception:
        return path.lower().endswith(".gif")


# ── 插件主体 ─────────────────────────────────────

class PicToolboxPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        if config is None:
            config = {}
        self._processing = asyncio.Lock()
        self._enable_at_avatar = config.get("enable_at_avatar", True)
        self._match_mode = config.get("match_mode", False)
        self._gif_speed_allow_frame_drop = config.get("gif_speed_allow_frame_drop", False)
        # 启动时清理旧临时文件（进程崩溃残留）
        self._cleanup_stale_tempfiles()

    # ── 参照 pic_mirror 的指令解析 ─────────────
    @staticmethod
    def _command_text(event):
        # Plain components omit QQ's injected @ display text. Targets stay in At components.
        text = " ".join(part.text for part in event.get_messages() if isinstance(part, Comp.Plain)).strip()
        text = re.sub(r"^(?:哦鲸鲸|鲸鲸|小鲸鱼|大肥鱼)[，,:：\s]*", "", text).strip()
        if text.lstrip("/") in ("摸摸头", "摸摸我", "摸头我"):
            return "/摸头"
        return text

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_all_commands(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        settings = self.context.get_config(event.unified_msg_origin)['platform_settings']
        allowed = {str(item) for item in settings.get('id_whitelist', [])}
        if settings.get('enable_id_white_list'):
            if event.is_private_chat():
                if str(event.get_sender_id()) not in allowed and event.unified_msg_origin not in allowed:
                    return
            elif str(event.get_group_id()) not in allowed:
                return
        text = self._command_text(event).lstrip('/').strip()
        commands = {"反色", "左右翻转", "上下翻转", "顺时针", "逆时针", "左对称", "右对称", "上对称", "下对称", "摸头", "发射", "操你", "抽你", "杀"}
        if text not in commands and not re.fullmatch(r"调速\s+[\d.]+", text):
            return
        if self._processing.locked():
            event.stop_event()
            yield event.plain_result("上一张还在做，稍等一下再发我～")
            return
        async with self._processing:
            async for result in self._handle_commands(event):
                yield result

    async def _handle_commands(self, event: AstrMessageEvent):
        """手动解析 @用户 指令 或 指令 @用户，避免 / 前缀和 @ 标记冲突。"""
        msg = self._command_text(event)

        # 解析指令和 @ 目标（参照 pic_mirror 两阶段解析）
        actual_cmd = msg
        at_qq = self._extract_at_qq(event)

        if " @" in msg:
            parts = msg.split("@", 1)
            actual_cmd = parts[0].strip()
        elif msg.startswith("@"):
            parts = msg.split(None, 2)
            if len(parts) >= 2:
                actual_cmd = parts[1].strip()

        # 去掉可能的前导 /
        cmd_text = actual_cmd.lstrip("/").strip()

        # ── 反色 ────────────────────────────
        if cmd_text == "反色":
            image_url = None

            # 头像：@用户
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)

            # 引用/直接图片
            if not image_url:
                image_url = self._extract_image_url(event)

            if not image_url:
                return

            event.stop_event()
            async for r in self._download_and_process(event, image_url, invert.invert_image, "反色"):
                yield r
            return

        # ── 左右翻转 ─────────────────────────
        if cmd_text == "左右翻转":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return

            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                return
            event.stop_event()
            async for r in self._download_and_process(event, image_url, flip.flip_horizontal, "左右翻转"):
                yield r
            return

        # ── 上下翻转 ─────────────────────────
        if cmd_text == "上下翻转":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return

            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                return
            event.stop_event()
            async for r in self._download_and_process(event, image_url, flip.flip_vertical, "上下翻转"):
                yield r
            return

        # ── 顺时针 ─────────────────────────
        if cmd_text == "顺时针":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return

            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                return
            event.stop_event()
            async for r in self._download_and_process(event, image_url, rotate.rotate_clockwise, "顺时针"):
                yield r
            return

        # ── 逆时针 ─────────────────────────
        if cmd_text == "逆时针":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return

            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                return
            event.stop_event()
            async for r in self._download_and_process(event, image_url, rotate.rotate_counterclockwise, "逆时针"):
                yield r
            return

        # ── 左对称 ───────────────────────────
        if cmd_text == "左对称":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return
            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                return
            event.stop_event()
            async for r in self._download_and_process(event, image_url, mirror.mirror_left, "左对称"):
                yield r
            return

        # ── 右对称 ───────────────────────────
        if cmd_text == "右对称":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return
            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                return
            event.stop_event()
            async for r in self._download_and_process(event, image_url, mirror.mirror_right, "右对称"):
                yield r
            return

        # ── 上对称 ───────────────────────────
        if cmd_text == "上对称":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return
            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                return
            event.stop_event()
            async for r in self._download_and_process(event, image_url, mirror.mirror_top, "上对称"):
                yield r
            return

        # ── 下对称 ───────────────────────────
        if cmd_text == "下对称":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return
            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                return
            event.stop_event()
            async for r in self._download_and_process(event, image_url, mirror.mirror_bottom, "下对称"):
                yield r
            return

        # ── 摸头 ────────────────────────────
        if cmd_text == "摸头":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return

            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                image_url = QQ_AVATAR_URL.format(qq=event.get_sender_id())
            event.stop_event()
            async for r in self._download_and_process(event, image_url, petpet.generate_petpet, "摸头"):
                yield r
            return

        # ── 发射 ────────────────────────────
        if cmd_text == "发射":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return
            image_url = None
            if at_qq and self._enable_at_avatar:
                image_url = QQ_AVATAR_URL.format(qq=at_qq)
            if not image_url:
                image_url = self._extract_image_url(event)
            if not image_url:
                return
            event.stop_event()
            async for r in self._download_and_process(event, image_url, shoot.generate_shoot, "发射"):
                yield r
            return

        # ── 操你（双头像）─────────────────────
        if cmd_text == "操你":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return
            if not at_qq:
                yield event.plain_result("请 @ 一个目标！")
                return
            event.stop_event()
            async for r in self._dual_avatar(event, at_qq, do.generate_do):
                yield r
            return

        # ── 抽你（双头像）─────────────────────
        if cmd_text == "抽你":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return
            if not at_qq:
                yield event.plain_result("请 @ 一个目标！")
                return
            event.stop_event()
            async for r in self._dual_avatar(event, at_qq, lash.generate_lash):
                yield r
            return

        # ── 杀（单头像）─────────────────────
        if cmd_text == "杀":
            if not self._match_mode and not actual_cmd.startswith("/"):
                return
            if not at_qq or not self._enable_at_avatar:
                return
            event.stop_event()
            image_url = QQ_AVATAR_URL.format(qq=at_qq)
            async for r in self._download_and_process(event, image_url, behead.generate_behead, "杀"):
                yield r
            return

        # ── 调速（需精准匹配模式 或 显式 /）──
        speed_match = re.fullmatch(r"调速\s+([\d.]+)", cmd_text)
        if speed_match:
            if not self._match_mode and not actual_cmd.startswith("/"):
                return
            image_url = self._extract_image_url(event)
            if not image_url:
                return
            try:
                speed = gif_speed.parse_speed(speed_match.group(1))
            except ValueError as e:
                yield event.plain_result(str(e))
                return
            event.stop_event()
            allow_drop = self._gif_speed_allow_frame_drop
            speed_result = {"warning": None}
            def proc(inp, out):
                _, actual_speed, warning = gif_speed.adjust_gif_speed(
                    inp, out, speed, allow_frame_drop=allow_drop
                )
                speed_result["warning"] = warning
            async for r in self._download_and_process(
                event, image_url, proc, "调速",
                get_prefix=lambda: speed_result["warning"],
            ):
                yield r

    # ── 从组件提取 ────────────────────────────
    @staticmethod
    def _extract_at_qq(event: AstrMessageEvent) -> str | None:
        for comp in event.get_messages():
            if isinstance(comp, Comp.At):
                for attr in ("qq", "target", "user_id", "id"):
                    qq = getattr(comp, attr, None)
                    if qq and str(qq).isdigit() and str(qq) != str(event.get_self_id()):
                        return str(qq)
        return None

    @staticmethod
    def _extract_image_url(event: AstrMessageEvent) -> str | None:
        for comp in event.get_messages():
            if isinstance(comp, Comp.Reply):
                chain = getattr(comp, "chain", None) or []
                for rc in chain:
                    if isinstance(rc, Comp.Image):
                        url = getattr(rc, "url", None) or getattr(rc, "file", None)
                        if url:
                            return url
        for comp in event.get_messages():
            if isinstance(comp, Comp.Image):
                url = getattr(comp, "url", None) or getattr(comp, "file", None)
                if url:
                    return url
        return None

    async def _download_and_process(self, event: AstrMessageEvent,
                                     image_url: str, processor, label: str,
                                     get_prefix=None):
        uid = uuid.uuid4().hex[:8]  # 每次请求唯一标识，避免并发冲突
        input_path = os.path.join(TMP_DIR, f"pt_in_{os.getpid()}_{uid}.tmp")
        output_path = None
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, _download_sync, image_url, input_path)
        if not ok:
            try:
                os.remove(input_path)
            except OSError:
                pass
            yield event.plain_result("图片获取失败或过大。请用不超过8MB、200万像素的图片；动图最多120帧、累计800万像素。")
            return

        ext = ".gif" if label in ("摸头", "发射", "杀", "调速") or _is_gif(input_path) else ".png"
        output_path = os.path.join(TMP_DIR, f"pt_out_{os.getpid()}_{uid}{ext}")

        try:
            await loop.run_in_executor(None, processor, input_path, output_path)
        except Exception as e:
            logger.error(f"[pic_toolbox] {label} 失败: {e}")
            try:
                os.remove(output_path)
            except OSError:
                pass
            yield event.plain_result(f"处理失败: {e}")
            return
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass

        if get_prefix:
            prefix_text = get_prefix()
        else:
            prefix_text = None
        if prefix_text:
            yield event.chain_result([
                Comp.Plain(prefix_text + "\n"),
                Comp.Image(file=str(output_path)),
            ])
        else:
            yield event.chain_result([Comp.Image(file=str(output_path))])
        # 延迟 10s 后清理输出文件（给 QQ 足够时间上传）
        out = output_path
        async def _cleanup():
            await asyncio.sleep(10)
            try:
                os.remove(out)
                logger.debug(f"[pic_toolbox] 清理临时文件: {os.path.basename(out)}")
            except OSError:
                pass
        asyncio.ensure_future(_cleanup())

    async def _dual_avatar(self, event: AstrMessageEvent, at_qq: str, processor):
        """双头像处理：指令者 + 被 @ 者。"""
        sender_qq = event.get_sender_id()
        loop = asyncio.get_event_loop()
        uid = uuid.uuid4().hex[:8]
        c_path = os.path.join(TMP_DIR, f"pt_da_c_{os.getpid()}_{uid}.png")
        t_path = os.path.join(TMP_DIR, f"pt_da_t_{os.getpid()}_{uid}.png")
        o_path = os.path.join(TMP_DIR, f"pt_da_o_{os.getpid()}_{uid}.gif")

        def _clean_inputs():
            for p in (c_path, t_path):
                try:
                    os.remove(p)
                except OSError:
                    pass

        ok1 = await loop.run_in_executor(None, _download_sync, QQ_AVATAR_URL.format(qq=sender_qq), c_path)
        ok2 = await loop.run_in_executor(None, _download_sync, QQ_AVATAR_URL.format(qq=at_qq), t_path)
        if ok1 and ok2:
            try:
                await loop.run_in_executor(None, processor, c_path, t_path, o_path)
                yield event.chain_result([Comp.Image(file=str(o_path))])
            except Exception as e:
                yield event.plain_result(f"失败: {e}")
                try:
                    os.remove(o_path)
                except OSError:
                    pass
            _clean_inputs()
            out = o_path
            async def _cleanup():
                await asyncio.sleep(10)
                try:
                    os.remove(out)
                    logger.debug(f"[pic_toolbox] 清理临时文件: {os.path.basename(out)}")
                except OSError:
                    pass
            asyncio.ensure_future(_cleanup())
        else:
            _clean_inputs()
            yield event.plain_result("下载失败")

    @staticmethod
    def _cleanup_stale_tempfiles():
        """清除进程崩溃后残留的旧临时文件（超过 1 小时的）。"""
        import time
        now = time.time()
        cutoff = now - 3600
        try:
            for fname in os.listdir(TMP_DIR):
                if fname.startswith("pt_") and not fname.endswith((".py", ".pyc")):
                    fpath = os.path.join(TMP_DIR, fname)
                    try:
                        if os.path.getmtime(fpath) < cutoff:
                            os.remove(fpath)
                            logger.info(f"[pic_toolbox] 清理残留: {fname}")
                    except OSError:
                        pass
        except OSError:
            pass

    async def terminate(self):
        pass
