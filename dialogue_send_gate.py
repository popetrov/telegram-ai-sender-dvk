"""One serialized delivery lane for first contacts and automated dialogues."""
import asyncio
import time


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
