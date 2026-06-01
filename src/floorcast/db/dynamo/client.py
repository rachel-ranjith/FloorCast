"""DynamoDB resource/table factory.

Honors DYNAMODB_ENDPOINT_URL so the same code targets DynamoDB Local in dev
and real AWS in prod. No credentials or table names are hardcoded.
"""

from __future__ import annotations

import boto3

from config.settings import Settings, get_settings


def get_resource(settings: Settings | None = None):
    settings = settings or get_settings()
    kwargs: dict = {"region_name": settings.aws_region}
    if settings.dynamodb_endpoint_url:
        kwargs["endpoint_url"] = settings.dynamodb_endpoint_url
    return boto3.resource("dynamodb", **kwargs)


def get_table(settings: Settings | None = None):
    settings = settings or get_settings()
    return get_resource(settings).Table(settings.dynamo.table_name)
