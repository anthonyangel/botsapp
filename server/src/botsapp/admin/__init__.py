"""
Network-private admin UI: WhatsApp QR login, and the chat/group allowlist +
metadata management.

Deliberately a separate Starlette app/process from the public MCP server
(botsapp.app/botsapp.main). Has no authentication of its own; it must
never be given a public network listener.
"""
