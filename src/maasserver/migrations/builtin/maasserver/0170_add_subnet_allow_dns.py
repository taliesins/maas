# -*- coding: utf-8 -*-
# Generated by Django 1.11.11 on 2018-08-09 22:31
from __future__ import unicode_literals

from django.db import (
    migrations,
    models,
)


class Migration(migrations.Migration):

    dependencies = [
        ('maasserver', '0169_find_pod_host'),
    ]

    operations = [
        migrations.AddField(
            model_name='subnet',
            name='allow_dns',
            field=models.BooleanField(default=True),
        ),
    ]