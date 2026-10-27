"""Bounded, non-streaming client for the owner-selected SheApi endpoint."""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import ipaddress
import json

import aiohttp
from PIL import Image
from yarl import URL

ENDPOINT = 'https://cf.sheapi.cc/v1/images/generations'
MODEL = 'gpt-image-2'
MAX_JSON_BYTES = 24 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class DrawingError(Exception):
    """Safe errors without remote response bodies or credentials."""
    def __init__(self,message,*,http_status=None,stage=None):
        super().__init__(message)
        self.http_status=http_status
        self.stage=stage


class PublicResolver(aiohttp.resolver.DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        result = await super().resolve(host, port, family)
        if any(not ipaddress.ip_address(item['host']).is_global for item in result):
            raise OSError('Non-public image endpoint')
        return result


def validate_image(raw: bytes) -> tuple[bytes, str, tuple[int, int]]:
    if not 100 <= len(raw) <= MAX_IMAGE_BYTES:
        raise DrawingError('图片大小异常，这次没有发送。')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[image.format]
            size = image.size
            if not (0 < size[0] <= 8192 and 0 < size[1] <= 8192 and size[0]*size[1] <= 20_000_000):
                raise ValueError('Unexpected image dimensions')
            image.verify()
    except Exception:
        raise DrawingError('接口没有返回有效的图片。') from None
    return raw, suffix, size


async def bounded_body(response, maximum: int) -> bytes:
    if response.content_length and response.content_length > maximum:
        raise DrawingError('图片接口返回内容过大，这次停止接收。')
    body = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > maximum:
            raise DrawingError('图片接口返回内容过大，这次停止接收。')
    return bytes(body)


async def generate(key: str, prompt: str, timeout: int = 180) -> tuple[bytes, str, tuple[int, int]]:
    payload = {'model': MODEL, 'prompt': prompt, 'n': 1, 'size': '1024x1024',
               'response_format': 'b64_json', 'stream': False}
    limits = aiohttp.ClientTimeout(total=max(30, min(int(timeout), 240)), connect=15)
    try:
        async with aiohttp.ClientSession(timeout=limits, trust_env=False, headers={'User-Agent':'Mozilla/5.0'},
                                         connector=aiohttp.TCPConnector(resolver=PublicResolver())) as session:
            async with session.post(ENDPOINT, json=payload, allow_redirects=False,
                                    headers={'Authorization': 'Bearer '+key}) as response:
                if response.status in (401, 403):
                    raise DrawingError('画图密钥或接口访问被拒绝，请管理员检查（HTTP {}）。'.format(response.status),http_status=response.status)
                if response.status == 429:
                    raise DrawingError('画图服务繁忙，稍后再试。',http_status=response.status)
                if response.status != 200:
                    raise DrawingError('画图接口暂时失败（HTTP {}），没有自动重试。'.format(response.status),http_status=response.status)
                raw = await bounded_body(response, MAX_JSON_BYTES)
            try:
                body = json.loads(raw)
                item = body['data'][0]
                encoded = item.get('b64_json')
                image_url = item.get('url')
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                raise DrawingError('画图接口没有返回图片数据。') from None
            if encoded:
                try:
                    if not isinstance(encoded, str):
                        raise ValueError('Invalid base64 type')
                    if encoded.startswith('data:image/'):
                        encoded = encoded.split(',', 1)[1]
                    image = base64.b64decode(encoded, validate=True)
                except (ValueError, IndexError, binascii.Error):
                    raise DrawingError('画图接口返回的图片编码无效。') from None
            elif image_url:
                url = URL(image_url)
                if url.scheme != 'https' or not url.host or url.user or url.password:
                    raise DrawingError('图片下载地址无效。')
                try:
                    if not ipaddress.ip_address(url.host).is_global:
                        raise DrawingError('图片下载地址无效。')
                except ValueError:
                    pass
                try:
                    async with session.get(url, allow_redirects=False) as response:
                        if response.status != 200:
                            raise DrawingError('图片已经生成，但暂时无法下载；没有重新生成。',http_status=response.status,stage='download')
                        image = await bounded_body(response, MAX_IMAGE_BYTES)
                except asyncio.TimeoutError:
                    raise DrawingError('图片已经生成，但下载超时，还没拿到可发送的图片。',stage='download') from None
                except DrawingError as error:
                    error.stage='download'
                    raise
                except (aiohttp.ClientError,OSError):
                    raise DrawingError('图片已经生成，但下载连接中断，还没拿到可发送的图片。',stage='download') from None
            else:
                raise DrawingError('画图接口没有返回图片数据。')
            return validate_image(image)
    except asyncio.TimeoutError:
        raise DrawingError('画图等待超时，没有自动重试；稍后再试。') from None
    except (aiohttp.ClientError, OSError):
        raise DrawingError('暂时连接不上画图服务，没有自动重试。') from None
