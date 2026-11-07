# Mattermost live-update diagnosis

On 7 November 2026, the browser opened Mattermost at
`http://127.0.0.1:57898`. Its actual WebSocket requests used the correct
`ws://127.0.0.1:57898/api/v4/websocket` endpoint but received HTTP 403. The
server's configured Site URL used `localhost` instead of `127.0.0.1`.

The read-only native probe used the same enrolled nonadmin observer token and
the same endpoint, varying only the Origin header:

| Origin | Handshake | First event |
| --- | --- | --- |
| `http://127.0.0.1:57898` | 403 | None |
| `http://localhost:57898` | 101 | `hello` |
| No Origin header | 101 | `hello` |

The actual captured results are in `showcase-websocket-diagnostic.json`. No
messages, roles, passwords, or server settings were changed by these probes.

This confirms an origin mismatch, rather than a wrong WebSocket port. Aligning
the configured Site URL with the actual browser origin preserves the existing
origin check. This diagnosis did not apply that configuration change. After
applying it, verify a 101 handshake using the browser's origin and a new post
arriving in the actual browser without refreshing.

Mattermost's [official knowledge-base article](https://support.mattermost.com/hc/en-us/articles/39370643896468-Mattermost-Real-Time-Features-Not-Working-e-g-Posts-Aren-t-Showing-Up-When-Connecting-From-Multiple-Origins)
documents this exact symptom and its Site URL/origin relationship. Its
[configuration documentation](https://docs.mattermost.com/administration-guide/configure/environment-configuration-settings#site-url)
defines Site URL as the address users access, including a nonstandard port.

The current official [WebSocket client source](https://github.com/mattermost/mattermost/blob/master/webapp/channels/src/actions/websocket_actions.ts#L207-L236)
uses an explicit WebSocket URL when configured. Otherwise it derives a URL
from the window origin and only appends the configured WebSocket port when the
host has no explicit port. The [window URL helper](https://github.com/mattermost/mattermost/blob/master/webapp/channels/src/utils/url.tsx#L36-L59)
explains why a browser on `127.0.0.1` chooses that hostname. The server's
[upgrade handler](https://github.com/mattermost/mattermost/blob/master/server/channels/api4/websocket.go#L53-L65)
invokes its origin checker before upgrading. These source links describe the
current upstream implementation; the actual local probe establishes the
installed server's behavior independently.
