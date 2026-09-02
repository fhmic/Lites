# Google OAuth setup for LITE

This project uses a Desktop OAuth client for Gmail and Calendar access.

## Required setup in Google Cloud Console

1. Go to https://console.cloud.google.com/
2. Select your project.
3. Enable these APIs:
   - Gmail API
   - Google Calendar API
4. Go to APIs & Services -> OAuth consent screen
5. Choose External
6. Fill in the branding fields with a real public web domain, for example:
   - App name: LITE
   - Homepage: https://example.com
   - Privacy policy: https://example.com/privacy
   - Terms: https://example.com/terms
   - Support email: your email
   - Developer contact: your email
7. Add your own Gmail address as a Test user.
8. Go to APIs & Services -> Credentials
9. Create Credential -> OAuth 2.0 Client ID
10. Choose Application type: Desktop app
11. Save the downloaded JSON as:
   - config/google_client_secret.json
12. Delete any stale token if needed:
   - config/google_token.json

## Important distinction

- The app redirect URI for the OAuth desktop flow is localhost-based.
- The branding / consent screen domain must be a real public domain, not localhost.

For this project, the actual desktop flow is configured to use:

- http://localhost:8080/

and the Google client JSON should include that exact redirect URI.

## Verification

The app code in core/google_workspace.py expects the Desktop OAuth flow and will open the browser consent screen once, then store the refresh token in config/google_token.json
