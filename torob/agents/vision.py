"""A shopper's photo → what product it shows, as a short Persian search query (Claude reads the image).

One small call per photo. The model only names what is visible (type, brand, model if legible on the product or
box); when it can't tell, the answer is None and the chat asks for the name instead of guessing.
"""

import re

from core.llm import LLMClient

PROMPT = ("این عکسِ یک کالا در مغازه است. فقط اسم کالا را برای جست‌وجو بنویس: نوع کالا، برند، و مدل اگر روی کالا یا "
          "جعبه خوانا است. حداکثر ۶ کلمه، بدون جمله و توضیح. اگر کالا معلوم نیست فقط بنویس: نامعلوم")
DATA_URL = re.compile(r"data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}")
MAX_QUERY = 60


def is_image(value: str | None, max_len: int) -> bool:
    return bool(value) and len(value) <= max_len and DATA_URL.fullmatch(value) is not None


def identify(llm: LLMClient, image: str, session_id: str) -> str | None:
    """The product in the photo as a search query, or None (unclear photo, LLM off, any error)."""
    msg = {"role": "user", "content": [{"type": "text", "text": PROMPT}, {"type": "image_url", "image_url": {"url": image}}]}
    try:
        res = llm.complete("vision", [msg], session_id=session_id, max_tokens=40)
    except Exception:
        return None
    q = " ".join(res.text.strip().strip("«»\"'.،").split())
    return None if not q or "نامعلوم" in q or len(q) > MAX_QUERY else q
