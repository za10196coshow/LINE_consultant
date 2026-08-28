import logging

from linebot.v3.messaging import ApiClient, Configuration, MessagingApi, PushMessageRequest, ReplyMessageRequest, TextMessage

logger = logging.getLogger(__name__)


class LineClient:
    def __init__(self, access_token: str):
        self.configuration = Configuration(access_token=access_token)

    def _api(self):
        return ApiClient(self.configuration)

    def display_name(self, user_id: str, source_type: str, conversation_id: str) -> str:
        try:
            with self._api() as api_client:
                api = MessagingApi(api_client)
                if source_type == "group":
                    return api.get_group_member_profile(conversation_id, user_id).display_name
                if source_type == "room":
                    return api.get_room_member_profile(conversation_id, user_id).display_name
                return api.get_profile(user_id).display_name
        except Exception as exc:
            logger.warning(
                "LINE profile lookup failed user_suffix=%s category=%s",
                user_id[-6:] if user_id else "unknown",
                type(exc).__name__,
            )
            return "メンバー"

    def reply(self, reply_token: str, text: str) -> bool:
        try:
            with self._api() as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text[:5000])])
                )
            logger.info("LINE reply success")
            return True
        except Exception as exc:
            logger.error("LINE reply failed category=%s", type(exc).__name__)
            return False

    def push(self, conversation_id: str, text: str) -> bool:
        try:
            with self._api() as api_client:
                MessagingApi(api_client).push_message(PushMessageRequest(to=conversation_id, messages=[TextMessage(text=text[:5000])]))
            logger.info("LINE push success target_suffix=%s", conversation_id[-6:])
            return True
        except Exception as exc:
            logger.error("LINE push failed target_suffix=%s category=%s", conversation_id[-6:], type(exc).__name__)
            return False
