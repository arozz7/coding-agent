---
name: security-auditor
description: Scans code for security risks. Use this before every PR or when editing .env files.
---
# Security Auditor
1. **Scan for Secrets:** Run `python ./scripts/scan_secrets.py`.
2. **Review DB Logic:** If the task involves SQL, verify that the agent is using **parameterized queries** and NOT string interpolation.
3. **Reporting:** If threats are found, create a "Security Alert" Artifact and refuse to commit until fixed.