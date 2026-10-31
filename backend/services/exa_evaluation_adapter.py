"""评测专用EXA HTTP边界；固定端点，禁重定向与自动重试。"""
import httpx
from services.exa_search import EXA_ENDPOINT


async def post_search(payload, key):
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(EXA_ENDPOINT, json=payload, headers={'x-api-key': key})
        response.raise_for_status()
        return response.json()
