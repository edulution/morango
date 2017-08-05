# -*- coding: utf-8 -*-
# Generated by Django 1.9.13 on 2017-08-05 04:46
from __future__ import unicode_literals

from django.db import migrations
import morango.utils.uuids


class Migration(migrations.Migration):

    dependencies = [
        ('morango', '0012_nonce'),
    ]

    operations = [
        migrations.AlterField(
            model_name='syncsession',
            name='id',
            field=morango.utils.uuids.UUIDField(primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name='transfersession',
            name='id',
            field=morango.utils.uuids.UUIDField(primary_key=True, serialize=False),
        ),
    ]
