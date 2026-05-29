---
name: Telegram upload.GetFile chunk limit
description: The limit parameter for upload.GetFile must be ≤1MB and a multiple of 4096 bytes — passing 2MB causes LIMIT_INVALID errors
---

The `upload.GetFile` Telegram API call enforces a hard maximum `limit` of **1,048,576 bytes (1 MB)**. The value must also be a multiple of 4096.

**Why:** Telegram's server-side validation rejects any `limit` that exceeds 1MB or is not 4KB-aligned, returning `[400 LIMIT_INVALID]`. The original code used `chunk_size = 2 * 1024 * 1024` (2MB) which hit this cap on every streaming request.

**How to apply:** In `FileStream/server/api_routes.py`, keep `chunk_size = 1 * 1024 * 1024`. The offset alignment (`from_bytes - (from_bytes % chunk_size)`) naturally produces 4096-aligned offsets since 1MB = 256 × 4096.
