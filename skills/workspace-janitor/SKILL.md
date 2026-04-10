---
name: workspace-janitor
description: A two-stage cleanup tool. It identifies clutter and removes it upon approval.
---
# Workspace Janitor
1. **Audit:** Run `python3 ./scripts/health_check.py`.
2. **Review:** Present the `suggestions` to the user in a clear list.
3. **Approval:** Ask: "Which of these should I clean up for you?"
4. **Action:** Pass the user's selected items as a JSON list to `python3 ./scripts/cleanup.py '<JSON_LIST>'`.
5. **Verify:** Run the health check one last time to confirm a "healthy" status.