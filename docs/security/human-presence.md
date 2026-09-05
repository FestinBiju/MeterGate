# Proof of Human Presence

MeterGate replaces visual CAPTCHA with a cryptographic, privacy-preserving question: did the registered account holder actively verify this exact sensitive action with a passkey recently?

The browser requests a server-recognized action binding: `new_agent_session` or `renew_agent_session`. Renewal additionally binds the exact agent-session ID and preserves its existing scopes. Redis stores a random `hpc_…` challenge bound to the account, opaque session hash, trusted Origin, action, exact MCP scopes and lifetime, and the credentials allowed when the challenge was issued. Consumption is atomic and leaves a replay tombstone. WebAuthn requires `userVerification=required`; there is no checkbox, image puzzle, or agent fallback. A creation proof cannot renew a connection, and a renewal proof cannot create one or renew a different connection.

Successful verification creates an `hpp_…` PostgreSQL artifact containing only safe references and hashes. It binds the account, session hash, credential reference, action, normalized resource binding, Origin, challenge hash, issue/expiry time, version, and RFC 8785 SHA-256 fingerprint. The proof lasts at most `HUMAN_PRESENCE_TTL_SECONDS` and is consumed once by the exact sensitive action.

MCP exposes no challenge or proof-minting tool. An agent can only pause for the trusted browser flow. Purchase approval already performs required user verification over the exact quote/policy review and therefore satisfies the same human-presence principle without a second prompt. Operator actions retain their recent-passkey gate; expanding PoHP to individual operator mutations is a future, separately reviewed binding migration.

No raw assertion, session cookie, CSRF token, challenge bytes, passkey public key, MCP secret, or capability is stored in the proof or returned by status APIs.
