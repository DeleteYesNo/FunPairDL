"""FunPairDL - Paired download manager for video + funscript files."""

import os as _os

# Point OpenSSL's default trust store at certifi's CA bundle for the whole
# process. This MUST run before aiohttp (or anything that builds an ssl
# context) is imported — which it does, since this package __init__ executes
# before any funpairdl.* submodule and aiohttp is only imported from those.
#
# Why: the bundled Python links an old OpenSSL (1.1.1q). With no SSL_CERT_FILE
# it verifies against the Windows system store, which still contains an expired
# legacy root. OpenSSL 1.1.1's path builder anchors to that expired root and
# rejects perfectly valid Let's Encrypt chains with "certificate has expired" —
# which silently killed Pixeldrain/GoFile quota lookups (blank status bar) and
# /probe size lookups (no file size on links), while MEGA (different CA) kept
# working. certifi has the correct, non-expired roots, so verification stays
# ON; setdefault lets an explicit user/env override still win.
try:
    import certifi as _certifi

    _ca = _certifi.where()
    _os.environ.setdefault("SSL_CERT_FILE", _ca)
    _os.environ.setdefault("SSL_CERT_DIR", _os.path.dirname(_ca))
except Exception:  # certifi missing/unreadable — fall back to system default
    pass

__version__ = "0.1.0"
