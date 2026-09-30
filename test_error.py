import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import RequestFactory
from routing.views import route_plan

factory = RequestFactory()
request = factory.get(
    '/api/route/',
    {'start': '35.4676,-97.5164', 'finish': '41.8781,-87.6298'},
)
response = route_plan(request)
print(response.status_code)
print(response.content)
