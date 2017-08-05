# -*- coding: utf-8 -*-
# Generated by Django 1.9.13 on 2017-08-04 18:40
from __future__ import unicode_literals

from django.db import migrations, models
import django.utils.timezone
import morango.utils.uuids


class Migration(migrations.Migration):

    dependencies = [
        ('morango', '0011_certificate_salt'),
    ]

    operations = [
        migrations.CreateModel(
            name='Nonce',
            fields=[
                ('id', morango.utils.uuids.UUIDField(editable=False, primary_key=True, serialize=False)),
                ('timestamp', models.DateTimeField(default=django.utils.timezone.now)),
                ('ip', models.CharField(blank=True, max_length=100)),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
