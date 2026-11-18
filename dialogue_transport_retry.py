"""Temporary transport outages never transfer a client's dialogue to a manager."""
import logging
import time
import traceback
from openai import APIConnectionError
from telegram.error import NetworkError
from telethon.errors import ServerError

TRANSIENT_ERRORS = (ConnectionError, TimeoutError, APIConnectionError, NetworkError, ServerError)


def record_retry(worker, exc):
    # Metadata only: no messages, exception text, tokens or paths.
    now=time.monotonic()
    if now-getattr(worker,'transport_retry_logged_at',-3600)<300:return
    worker.transport_retry_logged_at=now
    frames=[{'file':f.filename.replace('\\','/').rsplit('/',1)[-1],
             'function':f.name,'line':f.lineno}
            for f in traceback.extract_tb(exc.__traceback__)[-8:]]
    logging.getLogger(__name__).warning('Dialogue transport retry (%s), peer=%s',
                                      type(exc).__name__,worker.settings['peer_id'])
    try:
        from dialogue_seller import event
        with worker.store.connection() as db:
            t=worker.store._thread(db,worker.thread_id)
            event(db,worker.thread_id,'transport_retry',t['revision'],worker.store.clock(),
                  error_type=type(exc).__name__,frames=frames,retry_seconds=60)
    except Exception:
        logging.getLogger(__name__).warning('Transport retry metadata unavailable')
