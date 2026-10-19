class KairosError(Exception):
    code = "internal_error"

    def __init__(self, code=None):
        if code:
            self.code = code
        super().__init__(self.code)


class AuthError(KairosError):
    code = "google_auth_failed"


class PolicyError(KairosError):
    code = "free_tier_unverified"


class QuotaError(KairosError):
    code = "free_quota_exhausted"

    def __init__(self, code=None, retry_at=None):
        self.retry_at = retry_at
        super().__init__(code)


class VideoError(KairosError):
    code = "video_input_failed"


class GeminiError(KairosError):
    code = "gemini_response_failed"


class DiscoveryError(KairosError):
    code = "youtube_discovery_failed"


class TelegramError(KairosError):
    code = "telegram_send_failed"


class UncertainDelivery(TelegramError):
    code = "telegram_delivery_uncertain"
