# -*- encoding: utf-8 -*-

"""
Douyin room id / user id resolution.

Derived from DouyinLiveRecorder (https://github.com/ihmily/DouyinLiveRecorder)
Copyright (c) 2023 by Hmily, All Rights Reserved.
"""
import re

import httpx

from . import utils


class UnsupportedUrlError(Exception):
    pass


HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
    'Cookie': 's_v_web_id=verify_lk07kv74_QZYCUApD_xhiB_405x_Ax51_GYO9bUIyZQVf'
}


# 获取房间ID和用户secID
async def get_sec_user_id(url: str, proxy_addr: str | None = None, headers: dict | None = None) -> tuple | None:
    if not headers or all(k.lower() not in ['user-agent', 'cookie'] for k in headers):
        headers = HEADERS

    try:
        proxy_addr = utils.handle_proxy_addr(proxy_addr)
        async with httpx.AsyncClient(proxy=proxy_addr, timeout=15) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            redirect_url = response.url
            if 'reflow/' in str(redirect_url):
                match = re.search(r'sec_user_id=([\w_\-]+)&', str(redirect_url))
                if match:
                    sec_user_id = match.group(1)
                    room_id = str(redirect_url).split('?')[0].rsplit('/', maxsplit=1)[1]
                    return room_id, sec_user_id
                else:
                    raise RuntimeError("Could not find sec_user_id in the URL.")
            else:
                raise UnsupportedUrlError("The redirect URL does not contain 'reflow/'.")
    except UnsupportedUrlError as e:
        raise e
    except Exception as e:
        raise RuntimeError(f"An error occurred: {e}")


# 获取抖音号
async def get_unique_id(url: str, proxy_addr: str | None = None, headers: dict | None = None) -> str | None:
    if not headers or all(k.lower() not in ['user-agent', 'cookie'] for k in headers):
        headers = HEADERS

    try:
        proxy_addr = utils.handle_proxy_addr(proxy_addr)
        async with httpx.AsyncClient(proxy=proxy_addr, timeout=15) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            redirect_url = str(response.url)
            if 'reflow/' in str(redirect_url):
                raise UnsupportedUrlError("Unsupported URL")
            sec_user_id = redirect_url.split('?')[0].rsplit('/', maxsplit=1)[1]
            headers['Cookie'] = ('ttwid=1%7C4ejCkU2bKY76IySQENJwvGhg1IQZrgGEupSyTKKfuyk%7C1740470403%7Cbc9a'
                                 'd2ee341f1a162f9e27f4641778030d1ae91e31f9df6553a8f2efa3bdb7b4; __ac_nonce=06'
                                 '83e59f3009cc48fbab0; __ac_signature=_02B4Z6wo00f01mG6waQAAIDB9JUCzFb6.TZhmsU'
                                 'AAPBf34; __ac_referer=__ac_blank')
            user_page_response = await client.get(f'https://www.iesdouyin.com/share/user/{sec_user_id}',
                                                headers=headers, follow_redirects=True)
            matches = re.findall(r'unique_id":"(.*?)","verification_type', user_page_response.text)
            if matches:
                unique_id = matches[-1]
                return unique_id
            else:
                raise RuntimeError("Could not find unique_id in the response.")
    except UnsupportedUrlError as e:
        raise e
    except Exception as e:
        raise RuntimeError(f"An error occurred: {e}")
