import httpx
import asyncio

async def test():
    try:
        c = httpx.AsyncClient(timeout=30.0)
        r = await c.post('http://127.0.0.1:1234/v1/chat/completions', json={
            'model': 'qwen3.5-35b-a3b',
            'messages': [{'role': 'user', 'content': 'hi'}]
        })
        print('Status:', r.status_code)
        print('Text:', r.text[:300])
    except Exception as e:
        print('Error:', e)

asyncio.run(test())