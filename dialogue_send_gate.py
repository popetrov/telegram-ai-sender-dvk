"""One serialized delivery lane for first contacts and automated dialogues."""

import asyncio
import re
import time

# Keep spelling hyphens within words and identifiers such as 44-ФЗ.
_FINANCE_DASH = re.compile(r'[‒–—―⸺⸻﹘−]|(?<!\w)-|-(?!\w)')
_RANGE = re.compile(r'(?<![\w-])(?P<lower>\d+(?:[.,]\d+)?)[ \t]*[‒–—―⸺⸻﹘-][ \t]*(?P<upper>\d+(?:[.,]\d+)?)(?![\w-])')
_SEPARATOR = re.compile(r'[ \t]*(?:[‒–—―⸺⸻﹘]+|(?<!\w)-+|-(?!\w))[ \t]*')
_NEGATIVE = re.compile(r'(?<![\w\d])[-−](?=\d)')


def finance_client_text(text):
    """Rewrite customer-facing punctuation while preserving word hyphens and values."""
    if not isinstance(text, str):
        return text

    def range_words(match):
        before = match.string[:match.start()].rstrip().casefold()
        prefix = '' if before.endswith((' от', ' между')) or before in ('от', 'между') else 'от '
        join = ' и ' if before == 'между' or before.endswith(' между') else ' до '
        return prefix + match['lower'] + join + match['upper']

    def separator(match):
        before = match.string[:match.start()]
        after = match.string[match.end():]
        if not before or before.endswith(('\n', ':', ',', ';', '!', '?', '.')):
            return ' ' if before and not before.endswith('\n') and after and not after.startswith('\n') else ''
        if not after or after.startswith(('.', ',', ';', ':', '!', '?', '\n')):
            return ''
        return ': '

    text = _RANGE.sub(range_words, text)
    text = _NEGATIVE.sub('минус ', text)
    text = text.replace('−', ' минус ')
    return _SEPARATOR.sub(separator, text)


class DeliveryGate:
    def __init__(self,interval=10,clock=time.monotonic):
        self.interval,self.clock=interval,clock
        self.lock=asyncio.Lock()
        self.finished=None

    async def run(self,operation,guard):
        async with self.lock:
            guard()
            if self.finished is not None:
                remaining=self.interval-(self.clock()-self.finished)
                if remaining>0:await asyncio.sleep(remaining)
            guard()
            try:
                return await operation()
            finally:
                self.finished=self.clock()
