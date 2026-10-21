import logging
import traceback
import httpx
from app.config import settings

logger = logging.getLogger("saham_app.notifier")

class GoWANotifier:
    def __init__(
        self,
        base_url: str = None,
        device_id: str = None,
        default_target_phone: str = None,
        basic_user: str = None,
        basic_password: str = None
    ):
        self.base_url = (base_url or settings.GOWA_URL).rstrip("/")
        self.device_id = device_id or settings.GOWA_DEVICE_ID
        self.default_target_phone = default_target_phone or settings.GOWA_TARGET_PHONE
        self.basic_user = basic_user if basic_user is not None else settings.GOWA_BASIC_USER
        self.basic_password = basic_password if basic_password is not None else settings.GOWA_BASIC_PASSWORD

    async def send_message(self, message: str, phone: str = None) -> bool:
        """
        Mengirim pesan teks ke WhatsApp melalui GoWA REST API (/send/message).
        Mendukung HTTP Basic Auth & verbose logging.
        """
        target_phone = phone or self.default_target_phone
        endpoint = f"{self.base_url}/send/message"
        
        headers = {
            "Content-Type": "application/json",
            "X-Device-Id": self.device_id
        }
        
        payload = {
            "phone": target_phone,
            "message": message
        }

        # Setup HTTP Basic Auth jika kredensial diisi
        auth = None
        if self.basic_user and self.basic_password:
            auth = httpx.BasicAuth(username=self.basic_user, password=self.basic_password)

        logger.info(f"[GOWA REQ] Target URL: {endpoint}")
        logger.info(f"[GOWA REQ] Device ID: {self.device_id} | Target Phone: {target_phone} | Auth: {'BasicAuth Active' if auth else 'None'}")
        logger.info(f"[GOWA REQ] Message Snippet: {message[:100]}...")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(endpoint, json=payload, headers=headers, auth=auth)
                
                logger.info(f"[GOWA RESP] HTTP Status Code: {response.status_code}")
                logger.info(f"[GOWA RESP] Raw Body: {response.text}")

                if response.status_code in [200, 201, 202]:
                    logger.info(f"SUCCESS: Pesan berhasil terkirim ke {target_phone} via GoWA.")
                    return True
                else:
                    logger.error(
                        f"FAILED: GoWA merespons dengan HTTP {response.status_code}. Raw Body: {response.text}"
                    )
                    return False

        except httpx.TimeoutException:
            logger.error(f"TIMEOUT: Waktu tunggu habis saat menghubungi GoWA API ({endpoint}).")
            logger.error(traceback.format_exc())
            return False
        except httpx.RequestError as exc:
            logger.error(f"NETWORK ERROR: Gagal menghubungi GoWA API ({endpoint}): {exc}")
            logger.error(traceback.format_exc())
            return False
        except Exception as exc:
            logger.error(f"UNEXPECTED ERROR pada GoWANotifier: {exc}")
            logger.error(traceback.format_exc())
            return False

# Singleton instance
notifier_service = GoWANotifier()
