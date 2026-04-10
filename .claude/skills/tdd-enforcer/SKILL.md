---
name: tdd-enforcer
description: Use this for all feature implementations to ensure code reliability.
---
# TDD Protocol
1. **Draft Test:** Create a failing test file in `/tests` that covers the requested feature.
2. **Execute & Verify:** Run the test script and confirm it fails.
3. **Implement:** Write the minimum code required to pass the test.
4. **Self-Correction:** If the test fails after implementation, analyze logs and patch until green.