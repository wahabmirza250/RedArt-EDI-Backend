"""
WSGI config for redartdigital project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

from redartdigital.ops_999_startup import run_once

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "redartdigital.settings.production")

run_once()
application = get_wsgi_application()
