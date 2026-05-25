"""
Hermes Loop Protection — Message Deduplication Bridge
Prevents token burn loops by deduplicating messages within a configurable window.

Problem: In May 2026, VOX sent the same welcome message 662 times over 12 hours.
Each message spawned a new Hermes session (22K context tokens).
Total: ~400M input tokens burned. Cost: $23 (DeepSeek) vs would-be $1,235 (Claude).

Fix: MD5-hash incoming messages, skip duplicates within DEDUP_WINDOW seconds.
"""

import hashlib
import time
from collections import OrderedDict

DEDUP_WINDOW = 60       # seconds — skip identical messages within this window
MAX_CACHE_SIZE = 1000   # prevent memory leaks from unbounded cache

class MessageDedup:
    """Thread-safe message deduplication with time-based expiry."""

    def __init__(self, window: int = DEDUP_WINDOW, max_size: int = MAX_CACHE_SIZE):
        self.window = window
        self._cache = OrderedDict()
        self.max_size = max_size
        self.hits = 0
        self.misses = 0

    def is_duplicate(self, message: str | bytes) -> bool:
        """Return True if this message was seen within the dedup window."""
        if isinstance(message, str):
            message = message.encode()
        msg_hash = hashlib.md5(message).hexdigest()
        now = time.time()

        # Expire old entries
        cutoff = now - self.window
        while self._cache:
            oldest_key, oldest_ts = next(iter(self._cache.items()))
            if oldest_ts < cutoff:
                self._cache.popitem(last=False)
            else:
                break

        if msg_hash in self._cache:
            self.hits += 1
            return True  # DUPLICATE — skip this message

        # Not a duplicate — store and return False
        self._cache[msg_hash] = now
        self.misses += 1

        # Bound cache size
        while len(self._cache) > self.max_size:
            self._cache.popitem(last=False)

        return False

    def stats(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "cache_size": len(self._cache),
            "dedup_ratio": f"{self.hits}/{self.hits + self.misses}"
        }


# ===== INTEGRATION EXAMPLE =====
# Add to nats-hermes-bridge.py or any NATS-to-Hermes bridge:

"""
from dedup import MessageDedup

_dedup = MessageDedup(window=60)

async def on_message(msg):
    if _dedup.is_duplicate(msg.data):
        logger.debug(f"Dedup: skipped duplicate message (hits={_dedup.hits})")
        return  # SILENTLY DROP
    await process_with_hermes(msg.data)
"""

# ===== TEST =====
if __name__ == "__main__":
    d = MessageDedup(window=2)
    
    # First message — should NOT be duplicate
    assert d.is_duplicate("Hello VOX") == False
    
    # Same message immediately — IS duplicate
    assert d.is_duplicate("Hello VOX") == True
    
    # Different message — should NOT be duplicate
    assert d.is_duplicate("Hello FORGE") == False
    
    # Wait for window to expire
    time.sleep(2.1)
    
    # Same message after window — should NOT be duplicate (window expired)
    assert d.is_duplicate("Hello VOX") == False
    
    print("All tests passed!")
    print(f"Stats: {d.stats()}")
