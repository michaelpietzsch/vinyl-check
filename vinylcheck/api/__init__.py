"""The routes of the web API, one module per area; `vinylcheck.web.create_app` installs them."""

from . import discogs_account, masters, recorder, recordings, records, system, turntables  # noqa: F401
