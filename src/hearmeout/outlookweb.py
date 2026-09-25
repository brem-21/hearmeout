"""Outlook on the web inside Home's Mail pane.

It's Microsoft's own web app, signed in the same way as in a browser, so it works with any
work account: no IMAP, no app registration, no admin approval. The sign-in is remembered in
~/.local/share/hearmeout/web/ (readable only by you), like a browser profile.

One web view is kept for the whole session and moved into each new Home page, so Outlook
isn't reloaded every time Home is rebuilt.
"""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from . import config

INBOX = "https://outlook.office.com/mail/"
PROFILE_DIR = config.DATA_DIR / "web"
# Pages that belong inside the pane: Outlook itself and Microsoft's sign-in.
_INSIDE = ("outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft", "login.microsoftonline.com",
           "login.live.com", "login.microsoft.com", "aadcdn.msftauth.net", "aadcdn.msauth.net",
           "device.login.microsoftonline.com", "autologon.microsoftazuread-sso.com", "account.activedirectory.windowsazure.com")


def available() -> bool:
    """Whether Qt's web view is installed (PySide6-Addons)."""
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401
    except ImportError:
        return False
    return True


def _inside(url: QUrl) -> bool:
    host = url.host().lower()
    return url.scheme() in ("https", "about", "data", "blob") and (
        not host or any(host == h or host.endswith("." + h) for h in _INSIDE) or host.endswith(".microsoftonline.com"))


_profile = None


def profile():
    """A browser profile of our own, so the Outlook sign-in is remembered between runs."""
    global _profile
    if _profile is None:
        from PySide6.QtWebEngineCore import QWebEngineProfile
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_DIR.chmod(0o700)
        _profile = QWebEngineProfile("hearmeout-outlook")
        _profile.setPersistentStoragePath(str(PROFILE_DIR / "storage"))
        _profile.setCachePath(str(PROFILE_DIR / "cache"))
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        _profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
    return _profile


def make_view(parent=None):
    """The web view for the Mail pane, already opening your inbox."""
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    class Page(QWebEnginePage):
        """Keeps Outlook and sign-in in the pane; opens anything else (links in emails,
        attachments, "open in new window") in your normal browser."""

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802 (Qt naming)
            if is_main_frame and not _inside(url) and url.scheme() in ("http", "https", "mailto"):
                QDesktopServices.openUrl(url)
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)

        def createWindow(self, _type):  # noqa: N802
            # Outlook wants a new window (pop-out, link): catch its first URL and hand it to the browser.
            catcher = QWebEnginePage(self.profile(), self)
            catcher.urlChanged.connect(lambda u: (QDesktopServices.openUrl(u), catcher.deleteLater())
                                       if u.isValid() and u.toString() != "about:blank" else None)
            return catcher

    view = QWebEngineView(parent)
    page = Page(profile(), view)
    # No camera, mic, location or notifications for the pane.
    if hasattr(page, "permissionRequested"):  # Qt 6.8+
        page.permissionRequested.connect(lambda permission: permission.deny())
    else:
        page.featurePermissionRequested.connect(lambda origin, feature: page.setFeaturePermission(
            origin, feature, QWebEnginePage.PermissionDeniedByUser))
    settings = page.settings()
    settings.setAttribute(QWebEngineSettings.PluginsEnabled, False)
    settings.setAttribute(QWebEngineSettings.FullScreenSupportEnabled, False)
    view.setPage(page)
    view.setZoomFactor(0.9)  # a little smaller, so more of the inbox fits in the pane
    view.load(QUrl(INBOX))
    return view


def sign_out() -> None:
    """Forget the Outlook sign-in (cookies and stored data)."""
    if _profile is not None:
        _profile.cookieStore().deleteAllCookies()
        _profile.clearHttpCache()
    import shutil
    shutil.rmtree(PROFILE_DIR / "storage", ignore_errors=True)
