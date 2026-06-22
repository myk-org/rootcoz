Let me start by reading the graph report and pages manifest, then explore the configuration source files.Now let me read the models, main.py (for settings categories), and ai_client.py to get the complete picture:Now let me look at the encryption, vapid, and CLI config files for additional env vars:Let me also look at the `_merge_settings` function in main.py and the `LOG_LEVEL`/`DEBUG`/`ROOTCOZ_ENCRYPTION_KEY` env var usage:Now let me check the Dockerfile and entrypoint for additional env vars:

## Related Pages

- [Configuring Integrations](configuring-integrations.html)
- [Deployment Recipes](deployment-recipes.html)
- [REST API Reference](api-reference.html)
- [Managing Users and Roles](managing-users.html)
- [Using Multi-AI Peer Analysis](peer-analysis.html)