"""Read current Windows notifications (best-effort).

Uses the WinRT UserNotificationListener via `winsdk`. This is genuinely finicky:
it needs the user to grant "notification access" to Python under Windows Settings
> Privacy & security > Notifications, and unpackaged apps don't always get it. So
every path degrades to a clear spoken message instead of throwing. Read on request
('any notifications?'), never proactively — that would be spammy and a privacy
hazard.
"""
import logging
import threading

log = logging.getLogger("jarvis.notifications")


def _read_on_thread() -> tuple[str, list[dict]]:
    """WinRT calls on a dedicated thread with its own asyncio loop — the tool that
    calls us already runs inside the main event loop, so we can't asyncio.run here.
    Returns (status, items): status in 'ok' | 'denied' | 'unavailable'."""
    import asyncio

    async def _go() -> tuple[str, list[dict]]:
        from winsdk.windows.ui.notifications.management import (
            UserNotificationListener, UserNotificationListenerAccessStatus)
        from winsdk.windows.ui.notifications import NotificationKinds

        listener = UserNotificationListener.current
        status = await listener.request_access_async()
        if status != UserNotificationListenerAccessStatus.ALLOWED:
            return "denied", []

        notifs = await listener.get_notifications_async(NotificationKinds.TOAST)
        items: list[dict] = []
        for n in notifs:
            app = ""
            try:
                app = n.app_info.display_info.display_name or ""
            except Exception:
                pass
            texts: list[str] = []
            try:
                for binding in n.notification.visual.bindings:
                    for el in binding.get_text_elements():
                        if el.text:
                            texts.append(el.text)
            except Exception:
                pass
            if app or texts:
                items.append({"app": app, "text": " — ".join(texts)})
        return "ok", items

    result: dict = {}

    def worker():
        try:
            result["v"] = asyncio.run(_go())
        except Exception as e:                    # noqa: BLE001
            result["e"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=15)
    if "e" in result:
        log.warning("notification read failed: %s", result["e"])
        return "unavailable", []
    return result.get("v", ("unavailable", []))


def read() -> tuple[str, list[dict]]:
    try:
        import winsdk  # noqa: F401
    except ImportError:
        return "unavailable", []
    return _read_on_thread()
