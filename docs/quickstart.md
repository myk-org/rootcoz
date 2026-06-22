Let me start by reading the knowledge graph and pages manifest, then explore the codebase for quickstart-relevant details.Now let me explore the key files I need for the quickstart guide:Now let me look at the health endpoint and how to verify the setup:So there's no env_prefix, meaning `admin_key` maps to env var `ADMIN_KEY`. Let me check how login works:Now let me check how the CLI installation works:Let me check the require_approval default and how admins approve users:Now let me look at how config.toml works:Now let me check the REQUIRE_APPROVAL and SECURE_COOKIES env vars in the docker-compose file, and look at the raw source analysis flow:

## Related Pages

- [Setting Up the CLI](cli-setup.html)
- [Analyzing Test Failures](analyzing-failures.html)
- [Deployment Recipes](deployment-recipes.html)
- [Environment Variables and Configuration](environment-variables.html)
- [Configuring Integrations](configuring-integrations.html)