from __future__ import unicode_literals

import logging as logger

from django.apps import AppConfig
from django.db.utils import OperationalError
from morango.utils.syncing_utils import add_syncing_models

logging = logger.getLogger(__name__)


class MorangoConfig(AppConfig):
    name = 'morango'
    verbose_name = 'Morango'

    def ready(self):
        from morango.models import DatabaseIDModel, InstanceIDModel

        # NOTE: Warning: https://docs.djangoproject.com/en/1.10/ref/applications/#django.apps.AppConfig.ready
        # its recommended not to execute queries in this method, but we are producing the same result after the first call, so its OK

        # call this on app load up to get most recent system config settings
        try:
            if not DatabaseIDModel.objects.all():
                DatabaseIDModel.objects.create()
            InstanceIDModel.get_or_create_current_instance()
        except OperationalError:
            pass

        # add models to be synced by profile
        add_syncing_models()
