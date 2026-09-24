import os
import psycogreen.gevent
from gevent import monkey

if os.environ.get("GEVENT_PATCH_ALL") == "1":
    monkey.patch_all()
    psycogreen.gevent.patch_psycopg()


from core.celery import app as celery_app

__all__ = ("celery_app",)
