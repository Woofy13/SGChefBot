import time
from cachetools import TTLCache

_TTL = 4 * 60 * 60
_MAX_TURNS = 10
_MAX_CONTENT_CHARS = 800

_store = TTLCache(maxsize=1000, ttl=_TTL)


def get_history(user_id, limit=_MAX_TURNS):
    turns = _store.get(user_id, [])
    return [{"role": t["role"], "content": t["content"]} for t in turns[-limit:]]


def add_turn(user_id, role, content, kind="qa"):
    turns = _store.get(user_id, [])
    turns.append({
        "role": role,
        "content": (content or "")[:_MAX_CONTENT_CHARS],
        "ts": time.time(),
        "kind": kind,
    })
    if len(turns) > _MAX_TURNS * 2:
        turns = turns[-_MAX_TURNS * 2:]
    _store[user_id] = turns


def clear_history(user_id):
    _store.pop(user_id, None)
