import json
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken

logger = logging.getLogger(__name__)
User = get_user_model()


@database_sync_to_async
def _get_user_from_token(token_key: str):
    """Validate a JWT access token and return the corresponding User, or None."""
    try:
        token = AccessToken(token_key)
        return User.objects.get(id=token["user_id"])
    except (InvalidToken, TokenError, User.DoesNotExist):
        return None


class InvoiceConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time invoice processing updates.

    Connection URL: ws://<host>/ws/invoices/?token=<access_token>

    Each authenticated user is added to their own channel group
    (invoices_<user_id>). Celery tasks broadcast updates to this
    group as invoices move through pending → processing → completed/failed.
    """

    async def connect(self):
        query_string = self.scope.get("query_string", b"").decode()
        params = parse_qs(query_string)
        token_keys = params.get("token", [])

        if not token_keys:
            await self.close(code=4001)
            return

        user = await _get_user_from_token(token_keys[0])
        if user is None:
            await self.close(code=4001)
            return

        self.user = user
        self.group_name = f"invoices_{user.id}"

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        logger.debug("WS connected: user=%s group=%s", user.id, self.group_name)

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    # Called by Celery via channel_layer.group_send(type="invoice.update")
    async def invoice_update(self, event):
        await self.send(text_data=json.dumps({"_type": "invoice", **event["data"]}))

    # Called by _push_notification via channel_layer.group_send(type="notification.new")
    async def notification_new(self, event):
        await self.send(text_data=json.dumps({"_type": "notification", **event["data"]}))
